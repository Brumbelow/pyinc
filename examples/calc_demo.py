"""End-to-end ``calc`` demo: includes, incremental evaluation, and reconciliation.

Builds a tiny ``.calc`` workspace, reconciles the emitted results to disk via the
``@action`` layer, and shows the incremental properties: an edit to an
unreferenced file does no work and no writes; a comment-only edit backdates the
parse; removing an ``emit`` deletes only that owned output.

Run: ``python examples/calc_demo.py``
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make the sibling ``calc`` package importable both as ``python
# examples/calc_demo.py`` and via ``runpy.run_path`` (which does not add the
# script's directory to sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from calc.engine import calc_emit, calc_source, evaluate_name  # noqa: E402

from pyinc import Database  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        constants = base / "constants.calc"
        root = base / "m.calc"
        unrelated = base / "other.calc"
        out = base / "out"

        constants.write_text("let base = 40\n", encoding="utf-8")
        root.write_text(
            'include "constants.calc"\nlet alpha = base + 2\nemit alpha\nemit base\n',
            encoding="utf-8",
        )
        unrelated.write_text("let z = 1\n", encoding="utf-8")

        db = Database(mode="strict")
        calc_emit.reconcile(db, str(root), root=out)
        print(f"alpha={evaluate_name(db, str(root), 'alpha')[1]}")  # 42

        # Editing a file that nothing includes does no query work and no writes.
        db.reset_statistics()
        unrelated.write_text("let z = 2\n", encoding="utf-8")
        unrelated_run = calc_emit.reconcile(db, str(root), root=out)
        print(f"unrelated_edit_writes={unrelated_run.written}")
        print(f"unrelated_edit_executions={db.statistics().query_executions}")

        # A comment-only edit reparses but backdates the semantic parse.
        root.write_text(
            '# header\ninclude "constants.calc"\nlet alpha = base + 2\nemit alpha\nemit base\n',
            encoding="utf-8",
        )
        calc_emit.reconcile(db, str(root), root=out)
        backdated = db.inspect(calc_source, str(root)).last_recompute == "backdated"
        print(f"comment_edit_backdated={backdated}")

        # Removing an emit deletes only that owned output.
        root.write_text(
            'include "constants.calc"\nlet alpha = base + 2\nemit alpha\n',
            encoding="utf-8",
        )
        removed = calc_emit.reconcile(db, str(root), root=out)
        print(f"removed_emit_deleted={removed.deleted}")


if __name__ == "__main__":
    main()
