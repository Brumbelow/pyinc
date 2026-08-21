"""End-to-end fault injection across the action reconcile path."""

from __future__ import annotations

import ast
import errno
import importlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from _action_fault_injection import (
    FAULT_FAMILIES,
    RAW_OR_TYPED,
    assert_mutation_fault_invariants,
    assert_no_tmp_residue,
    assert_refusal_replays_after_checkpoint,
    assert_tree_and_ledger_unchanged,
    desired_spec,
    fault,
    inject_fault,
    inject_lock_acquire_fault,
    inject_path_method_fault,
    input_driven_action,
    make_nonregular_node,
    manifest_gate,
    named_gate,
)
from _action_witness import assert_deleted_equals_removed, manifest_bytes, tree_witness

from pyinc import Database, InMemoryArtifactStore, Input
from pyinc.action import Action, _manifest_path
from pyinc.errors import ActionManifestError, ActionPathError

# ``pyinc`` re-exports the action decorator under the submodule's own name,
# so the module object is fetched by path rather than by attribute.
action_module = importlib.import_module("pyinc.action")


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

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
    with pytest.raises(ActionManifestError, match="Cannot read action manifest"):
        emit.plan(db, root=root)
    disarm()
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.unchanged == ("out.txt",)


def inject_root_incarnation_stat_fault(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    *,
    gate: Callable[[Path], bool],
) -> Callable[[], None]:
    """Arm a ``Path.stat`` fault confined to the ``_root_incarnation`` probe.

    ``Path.lstat`` delegates to ``Path.stat`` through CPython 3.13, and
    ``Path.resolve`` ends in a ``stat`` on older ones, so a class-wide
    ``stat`` patch would also fault the reconcile entry's inspection of the
    root -- a different seam, with a typed refusal of its own. Swapping
    ``Path.stat`` in only for the dynamic extent of ``_root_incarnation``
    keeps the fault at the identity probe and leaves every other seam
    reading real pathlib. The module attribute is the seam ``_read_manifest``
    and ``_write_manifest`` both call the probe through; the original
    function runs underneath, so its own tolerating arm is what answers.
    """
    original_incarnation: Callable[[Path], list[int] | None] = (
        action_module._root_incarnation
    )
    original_stat: Callable[..., object] = Path.stat
    armed = [True]

    def hook(self: Path, *args: object, **kwargs: object) -> object:
        if gate(self):
            raise fault(code, self)
        return original_stat(self, *args, **kwargs)

    def scoped(root: Path) -> list[int] | None:
        if not armed[0] or not gate(root):
            return original_incarnation(root)
        Path.stat = hook  # type: ignore[method-assign, assignment]
        try:
            return original_incarnation(root)
        finally:
            Path.stat = original_stat  # type: ignore[method-assign, assignment]

    monkeypatch.setattr(action_module, "_root_incarnation", scoped)

    def disarm() -> None:
        armed[0] = False

    return disarm


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
    # Scoped to the incarnation probe rather than armed class-wide: the
    # helper's docstring records why the root's stat is the only one that
    # may fault here.
    disarm = inject_root_incarnation_stat_fault(
        monkeypatch, code, gate=named_gate("fault-root")
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
        assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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
        assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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
        assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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
        assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
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

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
    with pytest.raises(RAW_OR_TYPED):
        emit.plan(db, root=root)
    disarm()
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    result = emit.reconcile(db, root=root)
    assert result.updated == ("out.txt",)


def test_an_orphan_that_vanishes_in_the_deletion_window_is_not_reported_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    tool = "fault-window-vanish"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"orphan.txt": "recorded", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)
    db.set(source, desired_spec({"keep.txt": "kept"}))
    target = root / "orphan.txt"

    original_read = action_module.read_regular_file_with_identity

    def vanish_inside_the_window(path: Path, **kwargs: object) -> object:
        # The preflight read classified the orphan deletable; it vanishes
        # before the last-moment verification, which then reads None.
        if path.name == "orphan.txt" and target.exists():
            target.unlink()
        return original_read(path, **kwargs)

    monkeypatch.setattr(
        action_module, "read_regular_file_with_identity", vanish_inside_the_window
    )
    result = emit.reconcile(db, root=root)

    # The action reports only its own removals: the entry vanished under
    # someone else's hand, so deleted stays empty even though the path is
    # gone -- and the claim is still released by the fresh ledger.
    assert result.deleted == ()
    assert not target.exists()
    ledger = manifest_bytes(root, tool)
    assert ledger is not None and b"orphan.txt" not in ledger


# An injected EINTR behaves as any other OSError at these mutation seams;
# the interpreter retries a real EINTR below them, where it is unobservable.
@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_deletion_verification_fault_stops_the_run_before_the_orphan_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    tool = f"fault-window-read-{code}"
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
        monkeypatch,
        "read_regular_file_with_identity",
        code,
        gate=named_gate("orphan.txt"),
    )

    # Only an unsafe-path error is caught around the deletion, so every
    # injected family escapes raw today; the union survives a retyping.
    def refuse(active: Database) -> str:
        with pytest.raises(RAW_OR_TYPED) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)

    # The verification is the first mutation step, so this single-orphan
    # fixture is byte-identical too: nothing was deleted, pruned, or written.
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    assert_no_tmp_residue(root)

    disarm()
    result = emit.reconcile(db, root=root)
    assert result.deleted == ("orphan.txt",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_undeletable_orphan_stops_the_run_and_the_next_run_deletes_it(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    tool = "fault-unlink-parent"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"sub/orphan.txt": "recorded", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)
    spec = desired_spec({"keep.txt": "kept"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)
    # Removing an entry needs write permission on its parent directory,
    # while the last-moment verification read above it still succeeds.
    (root / "sub").chmod(0o555)

    # The unlink sits outside the typed wrap, so this escapes raw today
    # carrying the parent-relative name; the union survives a future
    # retyping and no message is pinned here.
    def refuse(active: Database) -> str:
        with pytest.raises(RAW_OR_TYPED) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    try:
        assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
    finally:
        (root / "sub").chmod(0o755)

    # The fault landed on the only deletion, so nothing moved at all.
    assert (root / "sub" / "orphan.txt").read_bytes() == b"recorded"
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    assert_no_tmp_residue(root)

    result = emit.reconcile(db, root=root)
    assert result.deleted == ("sub/orphan.txt",)


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_an_unlink_fault_stops_the_run_with_the_orphan_and_ledger_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    tool = f"fault-unlink-{code}"
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
        monkeypatch, "unlink_regular_file", code, gate=named_gate("orphan.txt")
    )

    # The removal sits outside the typed wrap, so every injected family
    # escapes raw today; the union survives a retyping.
    def refuse(active: Database) -> str:
        with pytest.raises(RAW_OR_TYPED) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)

    # The verification read completed but the removal never ran, so the
    # single-orphan fixture is still byte-identical.
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)
    assert_no_tmp_residue(root)

    disarm()
    result = emit.reconcile(db, root=root)
    assert result.deleted == ("orphan.txt",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_a_mid_set_deletion_fault_preserves_the_performed_order_across_runs(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    tool = "fault-deletion-order"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(
        source,
        desired_spec({
            "one/a.txt": "first",
            "three/c.txt": "second",
            "two/b.txt": "third",
            "keep.txt": "kept",
        }),
    )
    emit.reconcile(db, root=root)
    db.set(source, desired_spec({"keep.txt": "kept"}))

    # Deletions run in sorted order: one/a.txt, three/c.txt, two/b.txt.
    # The second parent is read-only, so the first deletion lands and the
    # second faults.
    (root / "three").chmod(0o555)
    try:
        with pytest.raises(RAW_OR_TYPED):
            emit.reconcile(db, root=root)
    finally:
        (root / "three").chmod(0o755)

    assert not (root / "one" / "a.txt").exists()
    assert (root / "three" / "c.txt").read_bytes() == b"second"
    assert (root / "two" / "b.txt").read_bytes() == b"third"
    ledger = manifest_bytes(root, tool)
    assert ledger is not None and b"one/a.txt" in ledger  # ledger still old

    before = tree_witness(root)
    result = emit.reconcile(db, root=root)
    after = tree_witness(root)
    # The repair run deletes the remainder in the same sorted order --
    # the report's tuple order is part of the matrix.
    assert result.deleted == ("three/c.txt", "two/b.txt")
    assert_deleted_equals_removed(result, before, after)


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_prune_fault_after_deletions_is_typed_and_the_next_run_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    tool = f"fault-prune-{code}"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"pkg/inner.txt": "nested"}))
    emit.reconcile(db, root=root)
    ledger_before = manifest_bytes(root, tool)
    db.set(source, desired_spec({"pkg": "now a file"}))
    disarm = inject_fault(
        monkeypatch, "remove_empty_directory", code, gate=named_gate("pkg")
    )

    with pytest.raises(
        ActionPathError,
        match="Cannot prune directory 'pkg' left by the previous layout: \\[Errno",
    ):
        emit.reconcile(db, root=root)

    # Phase-keyed: the orphan deletion already ran and STAYS run -- each
    # step is atomic, the set deliberately is not -- while the ledger is
    # still the old one and nothing torn is left behind.
    assert not (root / "pkg" / "inner.txt").exists()
    assert (root / "pkg").is_dir()
    assert_mutation_fault_invariants(root, root, tool, ledger_before)

    disarm()
    result = emit.reconcile(db, root=root)
    assert result.created == ("pkg",)
    assert result.deleted == ()
    assert (root / "pkg").read_bytes() == b"now a file"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unwritable_root_stops_publication_with_the_stale_bytes_intact(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    root.mkdir()
    tool = "fault-publish-parent"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "fresh"}))
    # Bytes standing at the target that no ledger ever claimed.
    output = root / "out.txt"
    output.write_bytes(b"stale")

    root.chmod(0o555)
    try:
        with pytest.raises(RAW_OR_TYPED):
            emit.reconcile(db, root=root)
    finally:
        root.chmod(0o755)

    # Publication opens its temporary beside the target, so an unwritable
    # parent stops the very first step: no temporary was ever created and
    # the bytes that were there are the bytes still there.
    assert output.read_bytes() == b"stale"
    assert manifest_bytes(root, tool) is None
    assert_no_tmp_residue(root)

    result = emit.reconcile(db, root=root)
    # Unowned pre-existing bytes classify as an update, not a creation: the
    # target exists with different content and the previous claims are empty.
    assert result.updated == ("out.txt",)
    assert output.read_bytes() == b"fresh"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_an_unwritable_root_stops_parent_creation_with_nothing_created(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    root.mkdir()
    tool = "fault-publish-mkdir"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"sub/out.txt": "fresh"}))

    root.chmod(0o555)
    try:
        # Creating the missing parent suppresses only a lost race with
        # another creator, so an unwritable root escapes raw today.
        with pytest.raises(RAW_OR_TYPED):
            emit.reconcile(db, root=root)
    finally:
        root.chmod(0o755)

    assert not (root / "sub").exists()
    assert manifest_bytes(root, tool) is None
    assert_no_tmp_residue(root)

    result = emit.reconcile(db, root=root)
    assert result.created == ("sub/out.txt",)


@pytest.mark.parametrize("code", FAULT_FAMILIES)
def test_a_publication_fault_leaves_no_temporary_and_the_next_run_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = tmp_path / "root"
    tool = f"fault-publish-{code}"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "old"}))
    emit.reconcile(db, root=root)
    ledger_before = manifest_bytes(root, tool)
    db.set(source, desired_spec({"out.txt": "new"}))
    disarm = inject_fault(monkeypatch, "atomic_write", code, gate=named_gate("out.txt"))

    with pytest.raises(RAW_OR_TYPED):
        emit.reconcile(db, root=root)

    # The replacement never reached the target: the published bytes are the
    # old ones, the ledger is the old one, and no temporary survives.
    assert (root / "out.txt").read_bytes() == b"old"
    assert_mutation_fault_invariants(root, root, tool, ledger_before)

    disarm()
    result = emit.reconcile(db, root=root)
    assert result.updated == ("out.txt",)
    assert (root / "out.txt").read_bytes() == b"new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_a_read_only_state_directory_leaves_outputs_published_and_the_ledger_old(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES does not bite as root")
    root = tmp_path / "root"
    state = tmp_path / "state"
    tool = "fault-state-dir"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "one"}))
    emit.reconcile(db, root=root, state_dir=state)
    ledger_before = manifest_bytes(state, tool)
    db.set(source, desired_spec({"out.txt": "two"}))

    state.chmod(0o555)
    try:
        with pytest.raises(RAW_OR_TYPED):
            emit.reconcile(db, root=root, state_dir=state)
    finally:
        state.chmod(0o755)

    # The output published before the ledger write faulted; the ledger is
    # the old one and the repair run classifies the already-correct bytes
    # unchanged.
    assert (root / "out.txt").read_bytes() == b"two"
    assert_mutation_fault_invariants(root, state, tool, ledger_before)
    result = emit.reconcile(db, root=root, state_dir=state)
    assert result.unchanged == ("out.txt",)
    assert manifest_bytes(state, tool) != ledger_before


def test_a_ledger_write_fault_during_a_voiding_run_leaves_the_void_unpersisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    tool = "fault-void-write"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "generated"}))
    emit.reconcile(db, root=root, state_dir=state)
    stash = tmp_path / "stash"
    os.rename(root, stash)
    root.mkdir()
    db.set(source, desired_spec({}))
    ledger_before = manifest_bytes(state, tool)
    disarm = inject_fault(
        monkeypatch, "atomic_write", errno.ENOSPC, gate=manifest_gate
    )

    with pytest.raises(RAW_OR_TYPED):
        emit.reconcile(db, root=root, state_dir=state)

    # The voiding decision could not be persisted: the ledger still holds
    # the dead claims and the dead incarnation, and nothing was deleted.
    assert manifest_bytes(state, tool) == ledger_before
    assert list(root.iterdir()) == []

    disarm()
    # The next run voids again -- and this time persists: fresh adoption,
    # live incarnation, no claims, and the renamed-aside bytes stay safe.
    emit.reconcile(db, root=root, state_dir=state)
    persisted = json.loads(manifest_bytes(state, tool) or b"{}")
    assert persisted["outputs"] == {}
    live = root.stat()
    assert persisted["root_incarnation"] == [live.st_dev, live.st_ino]
    assert (stash / "out.txt").read_bytes() == b"generated"


def _window_replacement_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    replacement: bytes,
) -> tuple[Action, Path, Database, Input[str], str, InMemoryArtifactStore, Callable[[], None]]:
    """Directory-to-file migration with a replacement landing in the window.

    Returns the action, the root, the warm database (its desired set
    already switched to the migrating layout), the input/spec/store the
    replay wrapper needs, and a disarm callable that restores the real
    unlink for the staged follow-up runs.
    """
    root = tmp_path / "root"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"pkg/inner.txt": "nested"}))
    emit.reconcile(db, root=root)
    target = root / "pkg" / "inner.txt"
    # The new layout turns the directory into a file: pkg enters the
    # prune map, and the orphan beneath it is deleted first.
    spec = desired_spec({"pkg": "now a file"})
    db.set(source, spec)

    original_unlink = action_module.unlink_regular_file

    def replace_inside_the_window(path: Path, **kwargs: object) -> bool:
        # Fires between the ownership read and the unlink -- the window.
        if path.name == "inner.txt":
            foreign = tmp_path / "replacement"
            foreign.write_bytes(replacement)
            os.replace(foreign, target)
        return bool(original_unlink(path, **kwargs))

    monkeypatch.setattr(action_module, "unlink_regular_file", replace_inside_the_window)

    def disarm() -> None:
        monkeypatch.setattr(action_module, "unlink_regular_file", original_unlink)

    return emit, root, db, source, spec, store, disarm


def test_a_window_replacement_inside_a_pruned_directory_aborts_then_refuses_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = "prune-window-drift"
    emit, root, db, source, spec, store, disarm = _window_replacement_run(
        tmp_path, monkeypatch, tool, b"BRAND NEW FILE FROM ANOTHER PROCESS"
    )
    target = root / "pkg" / "inner.txt"
    verified = target.stat()
    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)

    # Stage 1: the unlink declines the replaced entry, the directory is
    # not empty at the prune, and the run aborts typed mid-mutation.
    # The match stops at the bracket opening the OS error: this refusal
    # wraps a real OSError, which spells itself [Errno N] on POSIX and
    # [WinError N] on Windows.
    with pytest.raises(
        ActionPathError,
        match="Cannot prune directory 'pkg' left by the previous layout: \\[",
    ):
        emit.reconcile(db, root=root)

    after = tree_witness(root)
    installed = target.stat()
    # The only difference against the pre-call tree is the replacement
    # itself; the desired file was never written (deletions and prunes
    # precede writes), and the ledger is byte-unchanged.
    assert {k: v for k, v in after.items() if k != "pkg/inner.txt"} == {
        k: v for k, v in before.items() if k != "pkg/inner.txt"
    }
    assert target.read_bytes() == b"BRAND NEW FILE FROM ANOTHER PROCESS"
    assert (installed.st_dev, installed.st_ino) != (verified.st_dev, verified.st_ino)
    assert manifest_bytes(root, tool) == ledger_before
    assert (root / "pkg").is_dir()

    # Stage 2: with no injection, the next run refuses during preflight --
    # the survivor's bytes drifted from the recorded digest, so it is not
    # deletable and the prune preflight names it; plan() and reconcile()
    # agree, pre-mutation.
    disarm()
    ledger_stage2 = manifest_bytes(root, tool)
    stage2 = tree_witness(root)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError, match="it still holds 'pkg/inner\\.txt'"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    with pytest.raises(ActionPathError, match="it still holds 'pkg/inner\\.txt'"):
        emit.plan(db, root=root)
    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
    assert tree_witness(root) == stage2
    assert manifest_bytes(root, tool) == ledger_stage2


def test_a_byte_identical_window_survivor_inside_a_pruned_directory_is_deleted_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = "prune-window-identical"
    emit, root, db, source, spec, store, disarm = _window_replacement_run(
        tmp_path, monkeypatch, tool, b"nested"
    )
    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)

    # The abort is the same as the drifted replacement's: the unlink
    # declines the replaced entry and the directory is not empty at the
    # prune, and the match stops at the OS error's opening bracket for the
    # same reason.
    with pytest.raises(
        ActionPathError,
        match="Cannot prune directory 'pkg' left by the previous layout: \\[",
    ):
        emit.reconcile(db, root=root)

    after = tree_witness(root)
    assert {k: v for k, v in after.items() if k != "pkg/inner.txt"} == {
        k: v for k, v in before.items() if k != "pkg/inner.txt"
    }
    # Identity is compared DELIBERATELY: the replacement carries the same
    # bytes, so a bytes-only witness cannot see it at all.
    assert after["pkg/inner.txt"][3] == before["pkg/inner.txt"][3] == b"nested"
    assert after["pkg/inner.txt"][1:3] != before["pkg/inner.txt"][1:3]
    assert manifest_bytes(root, tool) == ledger_before
    assert (root / "pkg").is_dir()

    disarm()
    result = emit.reconcile(db, root=root)
    # Across runs the recorded digest decides ownership; the intra-run
    # identity protection ended with the aborted run, whose claim was
    # never released. The byte-identical survivor is therefore deleted
    # and the migration completes.
    assert result.deleted == ("pkg/inner.txt",)
    assert result.created == ("pkg",)
    assert (root / "pkg").read_bytes() == b"now a file"
    ledger = json.loads(manifest_bytes(root, tool) or b"{}")
    assert set(ledger["outputs"]) == {"pkg"}


def test_a_byte_identical_window_survivor_in_a_plain_deletion_is_durably_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    tool = "plain-window-identical"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"gen.txt": "generated", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)
    target = root / "gen.txt"
    verified = target.stat()
    db.set(source, desired_spec({"keep.txt": "kept"}))

    original_unlink = action_module.unlink_regular_file

    def replace_inside_the_window(path: Path, **kwargs: object) -> bool:
        if path.name == "gen.txt":
            foreign = tmp_path / "replacement"
            foreign.write_bytes(b"generated")
            os.replace(foreign, target)
        return bool(original_unlink(path, **kwargs))

    monkeypatch.setattr(action_module, "unlink_regular_file", replace_inside_the_window)
    first = emit.reconcile(db, root=root)
    monkeypatch.setattr(action_module, "unlink_regular_file", original_unlink)

    # No prune is involved, so the run COMPLETES: the ledger write
    # releases the claim, and the survivor is nobody's to delete --
    # durably, unlike the same survivor inside a pruned directory, where
    # the aborted run keeps the claim and the next run deletes it.
    assert first.deleted == ()
    survivor = target.stat()
    assert (survivor.st_dev, survivor.st_ino) != (verified.st_dev, verified.st_ino)
    ledger = manifest_bytes(root, tool)
    assert ledger is not None and b"gen.txt" not in ledger

    second = emit.reconcile(db, root=root)
    assert second.deleted == ()
    assert second.unchanged == ("keep.txt",)
    assert target.read_bytes() == b"generated"
    final = target.stat()
    assert (final.st_dev, final.st_ino) == (survivor.st_dev, survivor.st_ino)


@pytest.mark.skipif(os.name == "nt", reason="POSIX node types")
@pytest.mark.parametrize("kind", ("fifo", "socket", "char-device", "block-device"))
def test_a_non_regular_node_at_an_orphan_path_refuses_and_survives(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "root"
    tool = f"fault-node-orphan-{kind}"
    emit, source = input_driven_action(tool)
    store = InMemoryArtifactStore()
    db = Database("strict", store=store)
    db.set(source, desired_spec({"sub/gone.txt": "recorded", "keep.txt": "kept"}))
    emit.reconcile(db, root=root)
    target = root / "sub" / "gone.txt"
    target.unlink()
    make_nonregular_node(target, kind)  # may skip: capability-gated
    spec = desired_spec({"keep.txt": "kept"})
    db.set(source, spec)

    ledger_before = manifest_bytes(root, tool)
    before = tree_witness(root)

    def refuse(active: Database) -> str:
        with pytest.raises(
            ActionPathError, match="Owned output target is not a regular file"
        ) as caught:
            emit.reconcile(active, root=root)
        return str(caught.value)

    assert_refusal_replays_after_checkpoint(source, spec, store, db, refuse)
    with pytest.raises(
        ActionPathError, match="Owned output target is not a regular file"
    ):
        emit.plan(db, root=root)
    # The node survives, whatever it is, and nothing else moved.
    assert not stat.S_ISREG(target.lstat().st_mode)
    assert_tree_and_ledger_unchanged(root, root, tool, before, ledger_before)


@pytest.mark.skipif(os.name == "nt", reason="POSIX node types")
@pytest.mark.parametrize("kind", ("fifo", "socket", "char-device", "block-device"))
def test_a_non_regular_lock_path_refuses_with_a_typed_error(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    tool = f"fault-node-lock-{kind}"
    emit, source = input_driven_action(tool)
    db = Database("strict", store=InMemoryArtifactStore())
    db.set(source, desired_spec({"out.txt": "fresh"}))
    lock_path = action_module._lock_path(root.resolve(), tool)
    make_nonregular_node(lock_path, kind)  # may skip: capability-gated
    try:
        with pytest.raises(ActionPathError, match="reconciliation lock"):
            emit.reconcile(db, root=root)
        assert list(root.iterdir()) == []
    finally:
        lock_path.unlink()  # the lock directory is shared; leave it clean


_FS_CALLEES = frozenset({
    "read_regular_file", "read_regular_file_with_identity",
    "unlink_regular_file", "remove_empty_directory", "atomic_write",
    "_atomic_write", "_write_manifest", "_read_manifest", "_safe_target",
    "_orphan_cannot_exist", "_holds_only_desired_outputs",
    "_unprunable_entry", "_root_incarnation", "_action_lock_directory",
    "_lock_path", "FileLock", "acquire",
    "lstat", "stat", "resolve", "mkdir", "chmod", "scandir",
})

#: Non-filesystem os/os.path calls the action module is allowed to make.
#: os.scandir is the only filesystem toucher and is covered by the pairs.
OS_CALLS = frozenset({
    "os.fsencode", "os.fspath", "os.path.commonpath",
    "os.path.normcase", "os.scandir",
})

#: Every attribute-call name in the module, frozen. A new method call of
#: ANY name fails here first: if it touches the filesystem, register a
#: fault cell and add the callee to _FS_CALLEES; either way, extend this
#: inventory deliberately.
METHOD_CALL_NAMES = frozenset({
    "S_IMODE", "S_ISDIR", "S_ISLNK", "S_ISREG", "_desired_map",
    "_reconcile_locked", "acquire", "append", "as_posix", "casefold",
    "chmod", "commonpath", "count", "dumps", "encode", "fn", "fsencode",
    "fspath", "get", "gettempdir", "hexdigest", "home", "is_absolute",
    "is_dir", "is_file", "items", "join", "joinpath", "loads", "lstat",
    "mkdir", "monotonic", "normalize", "normcase", "outputs", "pop",
    "reconcile", "relative_to", "release", "resolve", "scandir",
    "setdefault", "sha256", "split", "startswith", "stat", "suppress",
    "values",
})


def _action_ast() -> ast.Module:
    # Fetching the module by path types it as a plain module object, whose
    # ``__file__`` is optional; the action layer is always file-backed.
    source = action_module.__file__
    assert source is not None
    return ast.parse(Path(source).read_text(encoding="utf-8"))


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _pair_counts() -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope = ["<module>"]

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_Call(self, node: ast.Call) -> None:
            name = _callee_name(node)
            if name in _FS_CALLEES:
                pair = (self.scope[-1], name)
                counts[pair] = counts.get(pair, 0) + 1
            self.generic_visit(node)

    Visitor().visit(_action_ast())
    return counts


def _os_calls() -> frozenset[str]:
    found: set[str] = set()
    for node in ast.walk(_action_ast()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            if isinstance(value, ast.Name) and value.id == "os":
                found.add(f"os.{node.func.attr}")
            elif (
                isinstance(value, ast.Attribute)
                and value.attr == "path"
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
            ):
                found.add(f"os.path.{node.func.attr}")
    return frozenset(found)


def _method_call_names() -> frozenset[str]:
    return frozenset(
        node.func.attr
        for node in ast.walk(_action_ast())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )


def _safe_fs_imports() -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(_action_ast()):
        if isinstance(node, ast.ImportFrom) and node.module == "_safe_fs":
            names.update(alias.name for alias in node.names)
    return frozenset(names) - {"UnsafeFilesystemPathError"}


def _resolve_cell(reference: str) -> object:
    module_name, separator, test_name = reference.partition("::")
    if not separator:
        return globals()[reference]
    module = importlib.import_module(module_name.removesuffix(".py"))
    return getattr(module, test_name)


#: Every filesystem call site in pyinc.action, keyed
#: (enclosing function, callee) -> (call count, fault cells).
#: Cells named bare live in this module; "file.py::name" cells live in the
#: named suite. A new fs call site -- a new pair OR a changed count --
#: fails the inventory test until a cell is registered here.
FAULT_REGISTRY: dict[tuple[str, str], tuple[int, tuple[str, ...]]] = {
    ("reconcile", "resolve"): (2, (
        "test_a_root_resolution_fault_is_typed_and_touches_nothing",
        "test_action.py::test_action_wraps_invalid_root_paths_as_typed_errors",
    )),
    ("reconcile", "lstat"): (1, (
        "test_a_root_inspection_fault_is_typed_and_touches_nothing",
        "test_action.py::test_action_wraps_non_directory_root_as_typed_path_error",
        "test_action.py::test_action_wraps_root_inspection_failure_as_typed_path_error",
    )),
    ("reconcile", "_lock_path"): (2, (
        "test_an_unwritable_lock_directory_base_fails_before_any_root_work",
        "test_a_lock_directory_creation_fault_fails_before_any_root_work",
    )),
    ("reconcile", "FileLock"): (1, (
        "test_a_lock_acquisition_fault_is_typed_and_touches_nothing",
        "test_action.py::test_action_lock_timeout_is_typed",
    )),
    ("reconcile", "acquire"): (1, (
        "test_a_lock_acquisition_fault_is_typed_and_touches_nothing",
        "test_a_non_regular_lock_path_refuses_with_a_typed_error",
        "test_action.py::test_action_rejects_nonregular_lock_path_with_typed_error",
    )),
    ("_lock_path", "_action_lock_directory"): (1, (
        "test_an_unwritable_lock_directory_base_fails_before_any_root_work",
        "test_a_lock_directory_creation_fault_fails_before_any_root_work",
        "test_a_lock_directory_mode_repair_fault_fails_before_any_root_work",
    )),
    ("_action_lock_directory", "resolve"): (1, (
        "test_an_unwritable_lock_directory_base_fails_before_any_root_work",
        "test_action_store_branches.py::test_action_lock_directory_resolves_a_symlinked_temporary_base",
    )),
    ("_action_lock_directory", "mkdir"): (1, (
        "test_an_unwritable_lock_directory_base_fails_before_any_root_work",
        "test_a_lock_directory_creation_fault_fails_before_any_root_work",
    )),
    ("_action_lock_directory", "lstat"): (1, (
        "test_action_store_branches.py::test_action_lock_directory_rejects_a_non_directory",
        "test_action_store_branches.py::test_action_lock_directory_rejects_a_symlinked_private_directory",
        "test_action_store_branches.py::test_action_lock_directory_rejects_foreign_owner",
    )),
    ("_action_lock_directory", "chmod"): (1, (
        "test_a_lock_directory_mode_repair_fault_fails_before_any_root_work",
        "test_action_store_branches.py::test_action_lock_directory_repairs_permissive_mode",
    )),
    ("_read_manifest", "read_regular_file"): (1, (
        "test_an_unreadable_ledger_refuses_before_mutation_warm_and_reloaded",
        "test_a_ledger_read_fault_refuses_typed_with_the_tree_and_ledger_intact",
        "test_action.py::test_manifest_schema_is_strict_and_failure_is_premutation",
    )),
    ("_read_manifest", "_root_incarnation"): (1, (
        "test_a_root_identity_fault_is_tolerated_and_the_run_converges",
        "test_action.py::test_a_voiding_reconcile_persists_the_adoption",
    )),
    ("_root_incarnation", "stat"): (1, (
        "test_a_root_identity_fault_is_tolerated_and_the_run_converges",
    )),
    ("_write_manifest", "_root_incarnation"): (1, (
        "test_a_ledger_write_fault_during_a_voiding_run_leaves_the_void_unpersisted",
        "test_action.py::test_a_voiding_reconcile_persists_the_adoption",
    )),
    ("_write_manifest", "_atomic_write"): (1, (
        "test_a_ledger_write_fault_leaves_outputs_published_and_the_next_run_converges",
        "test_a_read_only_state_directory_leaves_outputs_published_and_the_ledger_old",
        "test_a_ledger_write_fault_during_a_voiding_run_leaves_the_void_unpersisted",
    )),
    ("_reconcile_locked", "_read_manifest"): (1, (
        "test_an_unreadable_ledger_refuses_before_mutation_warm_and_reloaded",
        "test_a_ledger_read_fault_refuses_typed_with_the_tree_and_ledger_intact",
    )),
    ("_reconcile_locked", "_orphan_cannot_exist"): (1, (
        "test_an_unsearchable_orphan_parent_refuses_during_preflight",
        "test_action_store_branches.py::test_preflight_probes_answer_missing_and_refuse_unanswerable",
    )),
    ("_orphan_cannot_exist", "lstat"): (1, (
        "test_an_unsearchable_orphan_parent_refuses_during_preflight",
        "test_action_store_branches.py::test_preflight_probes_answer_missing_and_refuse_unanswerable",
    )),
    ("_reconcile_locked", "_holds_only_desired_outputs"): (1, (
        "test_an_unlistable_released_directory_refuses_during_preflight",
        "test_action_store_branches.py::test_preflight_probes_answer_missing_and_refuse_unanswerable",
    )),
    ("_holds_only_desired_outputs", "scandir"): (1, (
        "test_an_unlistable_released_directory_refuses_during_preflight",
        "test_action_store_branches.py::test_preflight_probes_answer_missing_and_refuse_unanswerable",
    )),
    ("_reconcile_locked", "_safe_target"): (6, (
        "test_a_target_inspection_fault_refuses_typed_before_any_write",
        "test_a_non_regular_node_at_an_orphan_path_refuses_and_survives",
        "test_action_store_branches.py::test_action_rechecks_target_type_before_writing",
    )),
    ("_safe_target", "lstat"): (1, (
        "test_a_target_inspection_fault_refuses_typed_before_any_write",
    )),
    ("_safe_target", "resolve"): (1, (
        "test_action_store_branches.py::test_safe_target_rejects_resolved_parent_escape",
    )),
    ("_reconcile_locked", "read_regular_file"): (2, (
        # Two sites share this pair: the orphan-ownership read and the
        # desired-state read.
        "test_an_unreadable_orphan_refuses_typed_during_preflight",
        "test_an_orphan_ownership_read_fault_refuses_with_nothing_mutated",
        "test_an_unreadable_output_refuses_typed_during_preflight",
        "test_a_desired_state_read_fault_refuses_with_nothing_mutated",
    )),
    ("_reconcile_locked", "_unprunable_entry"): (1, (
        "test_a_window_replacement_inside_a_pruned_directory_aborts_then_refuses_preflight",
        "test_action.py::test_reconcile_refuses_an_unlistable_migration_directory_before_deleting",
        "test_action.py::test_plan_refuses_an_unlistable_migration_directory",
    )),
    ("_unprunable_entry", "scandir"): (1, (
        "test_action.py::test_reconcile_refuses_an_unlistable_migration_directory_before_deleting",
        "test_action_store_branches.py::test_preflight_probes_answer_missing_and_refuse_unanswerable",
    )),
    ("_reconcile_locked", "read_regular_file_with_identity"): (1, (
        "test_an_orphan_that_vanishes_in_the_deletion_window_is_not_reported_deleted",
        "test_a_deletion_verification_fault_stops_the_run_before_the_orphan_is_touched",
        "test_action.py::test_a_replacement_landing_in_the_deletion_window_survives",
    )),
    ("_reconcile_locked", "unlink_regular_file"): (1, (
        "test_an_undeletable_orphan_stops_the_run_and_the_next_run_deletes_it",
        "test_an_unlink_fault_stops_the_run_with_the_orphan_and_ledger_intact",
        "test_a_mid_set_deletion_fault_preserves_the_performed_order_across_runs",
        "test_a_byte_identical_window_survivor_inside_a_pruned_directory_is_deleted_next_run",
        "test_a_byte_identical_window_survivor_in_a_plain_deletion_is_durably_safe",
        "test_action.py::test_a_byte_identical_replacement_in_the_deletion_window_survives",
    )),
    ("_reconcile_locked", "remove_empty_directory"): (1, (
        "test_a_prune_fault_after_deletions_is_typed_and_the_next_run_converges",
        "test_a_window_replacement_inside_a_pruned_directory_aborts_then_refuses_preflight",
        "test_a_byte_identical_window_survivor_inside_a_pruned_directory_is_deleted_next_run",
    )),
    ("_reconcile_locked", "_atomic_write"): (1, (
        "test_an_unwritable_root_stops_publication_with_the_stale_bytes_intact",
        "test_an_unwritable_root_stops_parent_creation_with_nothing_created",
        "test_a_publication_fault_leaves_no_temporary_and_the_next_run_converges",
    )),
    ("_atomic_write", "atomic_write"): (1, (
        "test_a_publication_fault_leaves_no_temporary_and_the_next_run_converges",
        "test_a_ledger_write_fault_leaves_outputs_published_and_the_next_run_converges",
    )),
    ("_reconcile_locked", "_write_manifest"): (1, (
        "test_a_ledger_write_fault_leaves_outputs_published_and_the_next_run_converges",
        "test_a_read_only_state_directory_leaves_outputs_published_and_the_ledger_old",
        "test_a_ledger_write_fault_during_a_voiding_run_leaves_the_void_unpersisted",
    )),
}

#: The public _safe_fs seams the action layer reaches: the five
#: functions action.py imports plus open_lock_file, which it reaches
#: through the lock machinery. ensure_directory is store-only and stays
#: with the store suite. _safe_fs INTERNAL windows stay with the existing
#: unit suite.
SAFE_FS_SEAMS: dict[str, tuple[str, ...]] = {
    "read_regular_file": (
        "test_a_ledger_read_fault_refuses_typed_with_the_tree_and_ledger_intact",
        "test_an_orphan_ownership_read_fault_refuses_with_nothing_mutated",
        "test_a_desired_state_read_fault_refuses_with_nothing_mutated",
        "test_safe_fs_locking_branches.py::test_posix_safe_fs_handles_missing_and_nonregular_targets",
    ),
    "read_regular_file_with_identity": (
        "test_an_orphan_that_vanishes_in_the_deletion_window_is_not_reported_deleted",
        "test_a_deletion_verification_fault_stops_the_run_before_the_orphan_is_touched",
        "test_safe_fs_locking_branches.py::test_identity_read_and_identity_checked_unlink",
    ),
    "unlink_regular_file": (
        "test_an_undeletable_orphan_stops_the_run_and_the_next_run_deletes_it",
        "test_an_unlink_fault_stops_the_run_with_the_orphan_and_ledger_intact",
        "test_a_byte_identical_window_survivor_in_a_plain_deletion_is_durably_safe",
        "test_safe_fs_locking_branches.py::test_identity_read_and_identity_checked_unlink",
    ),
    "remove_empty_directory": (
        "test_a_prune_fault_after_deletions_is_typed_and_the_next_run_converges",
        "test_safe_fs_locking_branches.py::test_remove_empty_directory_branches_on_missing_nondirectory_and_populated",
    ),
    "atomic_write": (
        "test_a_publication_fault_leaves_no_temporary_and_the_next_run_converges",
        "test_a_ledger_write_fault_leaves_outputs_published_and_the_next_run_converges",
        "test_safe_fs_locking_branches.py::test_posix_atomic_write_reports_exhausted_temporary_names",
    ),
    "open_lock_file": (
        "test_a_lock_acquisition_fault_is_typed_and_touches_nothing",
        "test_a_non_regular_lock_path_refuses_with_a_typed_error",
        "test_action.py::test_action_rejects_nonregular_lock_path_with_typed_error",
    ),
}


def test_every_filesystem_call_site_in_the_action_layer_is_registered() -> None:
    counted = _pair_counts()
    registered = {pair: count for pair, (count, _cells) in FAULT_REGISTRY.items()}
    assert counted == registered, (
        "the action layer's filesystem call sites changed; register a fault "
        f"cell for the difference: {set(counted.items()) ^ set(registered.items())}"
    )


def test_no_unlisted_os_call_enters_the_action_layer() -> None:
    assert _os_calls() == OS_CALLS


def test_no_unlisted_method_call_enters_the_action_layer() -> None:
    assert _method_call_names() == METHOD_CALL_NAMES


def test_every_public_safe_fs_seam_reached_by_actions_is_registered() -> None:
    # open_lock_file is reached through the lock machinery rather than an
    # action.py import; a new _safe_fs import lands here first.
    assert set(SAFE_FS_SEAMS) == _safe_fs_imports() | {"open_lock_file"}


def test_every_registered_fault_cell_exists() -> None:
    for _count, cells in FAULT_REGISTRY.values():
        for reference in cells:
            assert callable(_resolve_cell(reference)), reference
    for seam_cells in SAFE_FS_SEAMS.values():
        for reference in seam_cells:
            assert callable(_resolve_cell(reference)), reference


def test_every_registered_site_has_at_least_one_fault_cell() -> None:
    assert all(cells for _count, cells in FAULT_REGISTRY.values())
    assert all(SAFE_FS_SEAMS.values())
