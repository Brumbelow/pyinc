"""Filesystem effect witnesses shared by the action reconcile suites.

An assertion that reads only the returned ``ReconcileResult`` can pass while
the tree or the ledger silently changed underneath it. These helpers record
what is actually on disk -- kind, identity (st_dev/st_ino), and exact bytes
-- so a refusal test can prove nothing moved and a deletion test can prove
the report matches the filesystem.

A directory that cannot be listed hides its children from the walk: restore
its permissions before taking a witness, or the witness manufactures a
phantom diff.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pyinc.action import ReconcileResult, _manifest_path

TreeWitness = dict[str, tuple[object, ...]]


def tree_witness(root: Path) -> TreeWitness:
    """Record kind, identity, and content for every path under ``root``."""
    witness: TreeWitness = {}
    for current, directories, files in os.walk(root):
        for name in sorted(directories) + sorted(files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                witness[relative] = ("dir", metadata.st_dev, metadata.st_ino)
            elif stat.S_ISREG(metadata.st_mode):
                witness[relative] = (
                    "file",
                    metadata.st_dev,
                    metadata.st_ino,
                    path.read_bytes(),
                )
            else:
                witness[relative] = ("other", metadata.st_dev, metadata.st_ino)
    return witness


def manifest_bytes(state_dir: Path, tool: str) -> bytes | None:
    """The ledger's exact bytes, or None while no ledger exists."""
    try:
        return _manifest_path(state_dir, tool).read_bytes()
    except FileNotFoundError:
        return None


def assert_deleted_equals_removed(
    result: ReconcileResult, before: TreeWitness, after: TreeWitness
) -> None:
    """A completed reconcile's ``deleted`` must equal the files the tree lost."""
    assert not result.dry_run, "a dry run predicts; assert the prediction directly"
    removed = {
        path
        for path, entry in before.items()
        if entry[0] == "file" and after.get(path, ("absent",))[0] != "file"
    }
    assert set(result.deleted) == removed, (
        f"deleted={sorted(result.deleted)} removed={sorted(removed)}"
    )
