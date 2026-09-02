"""Reconcile pure desired artifacts to the filesystem with the @action layer.

Queries derive *desired* outputs (pure, tracked); a separate @action reconciles
them with the filesystem: it writes only what changed, repairs out-of-band
edits via content hashing, deletes outputs it previously owned but no longer
declares, and supports a dry-run plan. Side effects never enter a query.

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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src.txt"
        src.write_text("hi", encoding="utf-8")
        out = root / "out"

        db = Database(mode="strict")
        db.set(NAMES, ("alpha", "beta"))

        first = emit.reconcile(db, str(src), root=out)
        print(f"first_created={first.created}")

        rerun = emit.reconcile(db, str(src), root=out)
        print(f"rerun_updated={rerun.updated}")

        # Out-of-band edit to a generated file is detected via hash mismatch.
        (out / "beta.txt").write_text("TAMPERED", encoding="utf-8")
        repaired = emit.reconcile(db, str(src), root=out)
        print(f"tamper_repaired={repaired.repaired}")

        # Removing a declaration deletes only that owned output — and only
        # while that file still holds the bytes the ledger recorded for it, and
        # is still the same file the check read. A drifted orphan is released
        # instead: the ledger stops claiming it, and the file stays where it is.
        db.set(NAMES, ("alpha",))
        removed = emit.reconcile(db, str(src), root=out)
        print(f"orphan_deleted={removed.deleted}")

        # Dry-run plan writes nothing.
        plan_root = root / "planned"
        plan = emit.plan(db, str(src), root=plan_root)
        print(f"plan_created={plan.created}")
        print(f"plan_only_no_files={not plan_root.exists()}")


if __name__ == "__main__":
    main()
