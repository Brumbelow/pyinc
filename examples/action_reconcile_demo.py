"""Reconcile pure desired artifacts to the filesystem with the @action layer.

Queries derive the *desired* outputs and stay pure and tracked. A separate
@action reconciles them with the filesystem. It writes only what changed,
repairs out-of-band edits by content hash, deletes owned outputs it stops
declaring, and supports a dry-run plan. All side effects stay in the action.

Run: ``python examples/action_reconcile_demo.py``
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyinc import Database, FileResource, Input, Output, action, query

_FILES = FileResource()
NAMES = Input[tuple[str, ...]]("emit_names")


@query
def _body(db: Database, src: str) -> str:
    return _FILES.read(db, src)


@action(tool="reconcile-demo/1")
def emit(db: Database, src: str) -> list[Output]:
    body = _body(db, src)
    return [Output.text(f"{name}.txt", f"{name}:{body}") for name in NAMES.read(db)]


def main(mode: str = "strict") -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src.txt"
        src.write_text("hi", encoding="utf-8")
        out = root / "out"

        db = Database(mode=mode)
        db.set(NAMES, ("alpha", "beta"))

        first = emit.reconcile(db, str(src), root=out)
        print(f"first_created={first.created}")

        rerun = emit.reconcile(db, str(src), root=out)
        print(f"rerun_updated={rerun.updated}")

        # The action detects an out-of-band edit to a generated file by hash
        # mismatch.
        (out / "beta.txt").write_text("TAMPERED", encoding="utf-8")
        repaired = emit.reconcile(db, str(src), root=out)
        print(f"tamper_repaired={repaired.repaired}")

        # Removing a declaration deletes only that owned output. The delete
        # happens only while the file holds the bytes the ledger recorded and is
        # the same file the check read. A drifted orphan is released instead:
        # the ledger drops its claim and the file stays in place.
        db.set(NAMES, ("alpha",))
        removed = emit.reconcile(db, str(src), root=out)
        print(f"orphan_deleted={removed.deleted}")

        # A dry-run plan leaves the filesystem unchanged.
        plan_root = root / "planned"
        plan = emit.plan(db, str(src), root=plan_root)
        print(f"plan_created={plan.created}")
        print(f"plan_only_no_files={not plan_root.exists()}")


if __name__ == "__main__":
    main()
