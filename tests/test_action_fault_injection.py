"""End-to-end fault injection across the action reconcile path."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest
from _action_fault_injection import (
    FAULT_FAMILIES,
    RAW_OR_TYPED,
    assert_mutation_fault_invariants,
    assert_refusal_replays_after_checkpoint,
    assert_tree_and_ledger_unchanged,
    desired_spec,
    inject_fault,
    inject_lock_acquire_fault,
    inject_path_method_fault,
    input_driven_action,
    manifest_gate,
    named_gate,
)
from _action_witness import manifest_bytes, tree_witness

from pyinc import Database, InMemoryArtifactStore
from pyinc.action import _manifest_path
from pyinc.errors import ActionManifestError, ActionPathError


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unreadable_ledger_refuses_before_mutation_warm_and_reloaded(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    emit, source = input_driven_action("fault-ledger-unreadable")
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    spec = desired_spec({"out.txt": "fresh"})
    db.set(source, spec)
    emit.reconcile(db, root=root)

    ledger_before = manifest_bytes(root, "fault-ledger-unreadable")
    before = tree_witness(root)
    manifest = _manifest_path(root, "fault-ledger-unreadable")
    manifest.chmod(0o000)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionManifestError, match="Cannot read action manifest"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    try:
        assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
        with pytest.raises(ActionManifestError, match="Cannot read action manifest"):
            emit.plan(db, root=root)
    finally:
        manifest.chmod(0o644)

    # Witnesses with the mode restored: an unreadable ledger file would
    # raise out of the witness itself.
    assert_tree_and_ledger_unchanged(
        root, root, "fault-ledger-unreadable", before, ledger_before
    )


def test_a_ledger_write_fault_leaves_outputs_published_and_the_next_run_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    emit, source = input_driven_action("fault-ledger-write")
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "fresh"}))
    disarm = inject_fault(monkeypatch, "atomic_write", errno.ENOSPC, gate=manifest_gate)

    with pytest.raises(RAW_OR_TYPED):
        emit.reconcile(db, root=root)

    # The output landed before the ledger fault; the set is deliberately
    # not transactional, so the published file stays published.
    assert (root / "out.txt").read_bytes() == b"fresh"
    assert_mutation_fault_invariants(root, root, "fault-ledger-write", None)

    disarm()
    result = emit.reconcile(db, root=root)
    # The repair run finds the bytes already correct and the ledger absent:
    # it classifies the output unchanged, not created, and publishes the
    # ledger it could not write before.
    assert result.created == ()
    assert result.unchanged == ("out.txt",)
    assert manifest_bytes(root, "fault-ledger-write") is not None


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_root_resolution_fault_is_typed_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "fault-root"
    emit, source = input_driven_action(f"fault-root-resolve-{code}")
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    spec = desired_spec({"out.txt": "fresh"})
    db.set(source, spec)
    disarm = inject_path_method_fault(
        monkeypatch, "resolve", code, gate=named_gate("fault-root")
    )

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError, match="Action root or state directory is invalid"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    refuse(db)
    disarm()
    # Entry faults precede everything: the root was never even created.
    assert not root.exists()
    result = emit.reconcile(db, root=root)
    assert result.created == ("out.txt",)


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_root_inspection_fault_is_typed_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "fault-root"
    root.mkdir()
    emit, source = input_driven_action(f"fault-root-lstat-{code}")
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    spec = desired_spec({"out.txt": "fresh"})
    db.set(source, spec)
    disarm = inject_path_method_fault(
        monkeypatch, "lstat", code, gate=named_gate("fault-root")
    )

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError, match="Cannot safely inspect owned output path"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    refuse(db)
    disarm()
    # The inspection refused before any write, so the root is still empty.
    # Read it back only with the hook disarmed: the patch is class-wide.
    assert list(root.iterdir()) == []
    result = emit.reconcile(db, root=root)
    assert result.created == ("out.txt",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unwritable_lock_directory_base_fails_before_any_root_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    base = tmp_path / "temp-base"
    base.mkdir()
    emit, source = input_driven_action("fault-lock-base")
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "fresh"}))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))

    base.chmod(0o555)
    try:
        # The lock directory is prepared before the reconcile's typed-wrap
        # region, so today this escapes raw; the union survives a future
        # retyping of the escape.
        with pytest.raises(RAW_OR_TYPED):
            emit.reconcile(db, root=root)
    finally:
        base.chmod(0o755)

    assert not root.exists()
    result = emit.reconcile(db, root=root)
    assert result.created == ("out.txt",)


def locks_gate(path: Path) -> bool:
    """Match the per-user action lock directory at a shared seam."""
    return path.name.startswith("pyinc-action-locks")


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_lock_directory_creation_fault_fails_before_any_root_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    base = tmp_path / "temp-base"
    base.mkdir()
    emit, source = input_driven_action(f"fault-lock-mkdir-{code}")
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "fresh"}))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    disarm = inject_path_method_fault(monkeypatch, "mkdir", code, gate=locks_gate)

    # The injected error fires ahead of the real mkdir, so the create's
    # FileExistsError suppression never absorbs it, whichever family it is.
    with pytest.raises(RAW_OR_TYPED):
        emit.reconcile(db, root=root)

    assert not root.exists()
    disarm()
    result = emit.reconcile(db, root=root)
    assert result.created == ("out.txt",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_a_lock_directory_mode_repair_fault_fails_before_any_root_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("lock directory mode repair does not bite as root")
    root = tmp_path / "root"
    base = tmp_path / "temp-base"
    base.mkdir()
    emit, source = input_driven_action("fault-lock-chmod")
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "fresh"}))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    lock_directory = base / f"pyinc-action-locks-{os.getuid()}"
    lock_directory.mkdir(mode=0o755)
    lock_directory.chmod(0o755)  # explicit: group/other bits trip the repair
    disarm = inject_path_method_fault(monkeypatch, "chmod", errno.EPERM, gate=locks_gate)

    # The mode repair also sits outside the reconcile's typed-wrap region,
    # so this escapes raw today; the union survives a future retyping.
    with pytest.raises(RAW_OR_TYPED):
        emit.reconcile(db, root=root)

    assert not root.exists()
    disarm()
    result = emit.reconcile(db, root=root)
    assert result.created == ("out.txt",)
    # The next run finishes the repair the fault interrupted.
    assert stat.S_IMODE(lock_directory.lstat().st_mode) == 0o700


# An injected EINTR at this seam behaves as any other OSError; the
# interpreter retries a real EINTR below it, where it is unobservable.
@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_lock_acquisition_fault_is_typed_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    emit, source = input_driven_action(f"fault-lock-acquire-{code}")
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    spec = desired_spec({"out.txt": "fresh"})
    db.set(source, spec)
    before = tree_witness(root)
    disarm = inject_lock_acquire_fault(monkeypatch, code)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError, match="Cannot safely acquire the reconciliation lock"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    refuse(db)
    disarm()
    assert_tree_and_ledger_unchanged(
        root, root, f"fault-lock-acquire-{code}", before, None
    )
    result = emit.reconcile(db, root=root)
    assert result.created == ("out.txt",)


# An injected EINTR behaves as any other OSError at these preflight seams;
# the interpreter retries a real EINTR below them, where it is unobservable.
@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_ledger_read_fault_refuses_typed_with_the_tree_and_ledger_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    tool = f"fault-ledger-read-{code}"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    spec = desired_spec({"out.txt": "fresh"})
    db.set(source, spec)
    emit.reconcile(db, root=root)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    disarm = inject_fault(monkeypatch, "read_regular_file", code, gate=manifest_gate)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionManifestError, match="Cannot read action manifest"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    refuse(db)
    with pytest.raises(ActionManifestError, match="Cannot read action manifest"):
        emit.plan(db, root=root)
    disarm()
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.unchanged == ("out.txt",)


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_root_identity_fault_is_tolerated_and_the_run_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "fault-root"
    tool = f"fault-root-identity-{code}"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "fresh"}))
    emit.reconcile(db, root=root)
    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    disarm = inject_path_method_fault(
        monkeypatch, "stat", code, gate=named_gate("fault-root")
    )

    # An unanswerable root identity is tolerated by design: the recorded
    # claims stay in force (voiding needs BOTH incarnations answerable),
    # and a no-op reconcile stays a byte-identical no-op.
    result = emit.reconcile(db, root=root)
    disarm()
    assert result.unchanged == ("out.txt",)
    assert result.deleted == ()
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unsearchable_orphan_parent_refuses_during_preflight(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    tool = "fault-orphan-parent-search"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"outer/d/f.txt": "recorded", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)
    spec = desired_spec({"keep.txt": "kept"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    # lstat needs search permission on the ancestor; the recorded path is
    # two levels below it, so the probe meets EACCES rather than a benign
    # missing answer.
    (root / "outer").chmod(0o600)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError,
            match="Cannot safely inspect owned output path 'outer/d/f\\.txt'",
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    try:
        refuse(db)
        with pytest.raises(
            ActionPathError,
            match="Cannot safely inspect owned output path 'outer/d/f\\.txt'",
        ):
            emit.plan(db, root=root)
    finally:
        (root / "outer").chmod(0o700)

    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.deleted == ("outer/d/f.txt",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unlistable_released_directory_refuses_during_preflight(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    tool = "fault-released-dir-listing"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"pkg": "was a file", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)

    # A run that stopped before publishing its ledger: the file recorded at
    # 'pkg' is gone and a directory holding the newly nested output stands
    # in its place.
    (root / "pkg").unlink()
    (root / "pkg").mkdir()
    (root / "pkg" / "inner.txt").write_text("nested", encoding="utf-8")
    spec = desired_spec({"pkg/inner.txt": "nested", "keep.txt": "kept"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    # Deciding whether the directory holds nothing but desired outputs needs
    # a listing, and scandir needs read permission.
    (root / "pkg").chmod(0o300)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError,
            match="Cannot safely inspect directory 'pkg' left by the previous layout",
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    try:
        refuse(db)
        with pytest.raises(
            ActionPathError,
            match="Cannot safely inspect directory 'pkg' left by the previous layout",
        ):
            emit.plan(db, root=root)
    finally:
        (root / "pkg").chmod(0o700)

    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    # With the listing answerable the orphan reads as already released:
    # nothing is deleted, both files already carry the desired bytes, and
    # the fresh ledger records exactly the new layout.
    assert result.deleted == ()
    assert result.unchanged == ("keep.txt", "pkg/inner.txt")
    ledger_after = manifest_bytes(root, tool)
    assert ledger_after is not None
    assert set(json.loads(ledger_after)["outputs"]) == {"keep.txt", "pkg/inner.txt"}


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_target_inspection_fault_refuses_typed_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    tool = f"fault-target-lstat-{code}"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    spec = desired_spec({"sub/out.txt": "fresh"})
    db.set(source, spec)
    disarm = inject_path_method_fault(monkeypatch, "lstat", code, gate=named_gate("sub"))

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError,
            match="Cannot safely inspect owned output path 'sub/out\\.txt'",
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    refuse(db)
    with pytest.raises(
        ActionPathError, match="Cannot safely inspect owned output path 'sub/out\\.txt'"
    ):
        emit.plan(db, root=root)
    disarm()
    # An unanswerable component is fatal for every family but a plain
    # missing answer, so nothing was written and no ledger was published.
    # Read the tree back only with the class-wide hook disarmed.
    assert list(root.iterdir()) == []
    assert manifest_bytes(root, tool) is None
    result = emit.reconcile(db, root=root)
    assert result.created == ("sub/out.txt",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unreadable_orphan_refuses_typed_during_preflight(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    tool = "fault-orphan-unreadable"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"orphan.txt": "recorded", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)
    spec = desired_spec({"keep.txt": "kept"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    # Ownership is decided by the orphan's current bytes, so the run must
    # read it. Witness the tree first: the witness reads those bytes too.
    orphan = root / "orphan.txt"
    orphan.chmod(0o000)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError, match="Cannot safely open regular file"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    try:
        refuse(db)
        with pytest.raises(ActionPathError, match="Cannot safely open regular file"):
            emit.plan(db, root=root)
    finally:
        orphan.chmod(0o644)

    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.deleted == ("orphan.txt",)


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_an_orphan_ownership_read_fault_refuses_with_nothing_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    tool = f"fault-orphan-read-{code}"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"orphan.txt": "recorded", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)
    spec = desired_spec({"keep.txt": "kept"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    disarm = inject_fault(
        monkeypatch, "read_regular_file", code, gate=named_gate("orphan.txt")
    )

    # Only an unsafe-path error is caught at the ownership read, so every
    # injected family escapes raw today; the union survives a retyping.
    def refuse(active: Database) -> str:
        with pytest.raises(RAW_OR_TYPED) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    refuse(db)
    with pytest.raises(RAW_OR_TYPED):
        emit.plan(db, root=root)
    disarm()
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.deleted == ("orphan.txt",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unreadable_output_refuses_typed_during_preflight(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    tool = "fault-output-unreadable"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"out.txt": "old"}))
    emit.reconcile(db, root=root)
    spec = desired_spec({"out.txt": "new"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    # Classifying the target needs its current bytes, so an unreadable
    # output refuses before the replacement is written.
    output = root / "out.txt"
    output.chmod(0o000)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError, match="Cannot safely open regular file"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    try:
        refuse(db)
        with pytest.raises(ActionPathError, match="Cannot safely open regular file"):
            emit.plan(db, root=root)
    finally:
        output.chmod(0o644)

    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.updated == ("out.txt",)
    assert output.read_bytes() == b"new"


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_desired_state_read_fault_refuses_with_nothing_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    tool = f"fault-output-read-{code}"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"out.txt": "old"}))
    emit.reconcile(db, root=root)
    spec = desired_spec({"out.txt": "new"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    disarm = inject_fault(
        monkeypatch, "read_regular_file", code, gate=named_gate("out.txt")
    )

    # The desired-state read catches only an unsafe-path error, so every
    # injected family escapes raw today; the union survives a retyping.
    def refuse(active: Database) -> str:
        with pytest.raises(RAW_OR_TYPED) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    refuse(db)
    with pytest.raises(RAW_OR_TYPED):
        emit.plan(db, root=root)
    disarm()
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.updated == ("out.txt",)
