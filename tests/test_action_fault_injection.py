"""End-to-end fault injection across the action reconcile path."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from _action_fault_injection import (
    RAW_OR_TYPED,
    assert_mutation_fault_invariants,
    assert_refusal_replays_after_checkpoint,
    assert_tree_and_ledger_unchanged,
    desired_spec,
    inject_fault,
    input_driven_action,
    manifest_gate,
)
from _action_witness import manifest_bytes, tree_witness

from pyinc import Database, InMemoryArtifactStore
from pyinc.action import _manifest_path
from pyinc.errors import ActionManifestError


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
