"""End-to-end fault injection across the action reconcile path."""

from __future__ import annotations

import errno
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
