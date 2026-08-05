"""pyinc correctness demo — exercising its tracked consistency contract.

This demo walks through pyinc's core differentiators in sequence:

1. Incremental recomputation with dependency tracking
2. Exact raw inputs plus semantic backdating — comment-only edits skip downstream work
3. Selective recomputation — only affected queries re-execute
4. Untracked read enforcement — raw open() raises inside queries
5. Mutation protection — frozen values reject writes in strict mode
6. Provenance inspection — structured decision trees via explain()
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
from collections.abc import Mapping
from pathlib import Path

from pyinc import Database, FileResource, UntrackedReadError, query

# ---------------------------------------------------------------------------
# Setup: a tiny incremental pipeline
# ---------------------------------------------------------------------------

_FILES = FileResource()


@query
def read_source(db: Database, path: str) -> str:
    """Read exact Python source text through the resource API."""
    return _FILES.read(db, path)


@query
def source_structure(db: Database, path: str) -> tuple[str, str, int, int]:
    """Return the complete AST-derived payload consumed by this pipeline.

    Comment-only edits leave this payload equal, while ``read_source`` still
    publishes the exact changed text.
    """
    source = read_source(db, path)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ("source", source, 0, 0)
    functions = sum(
        1 for node in ast.iter_child_nodes(tree) if isinstance(node, ast.FunctionDef)
    )
    imports = sum(
        1 for node in ast.iter_child_nodes(tree) if isinstance(node, ast.Import | ast.ImportFrom)
    )
    return ("ast", ast.dump(tree), functions, imports)


@query
def count_functions(db: Database, path: str) -> int:
    """Count top-level function definitions in a source file."""
    return source_structure(db, path)[2]


@query
def count_imports(db: Database, path: str) -> int:
    """Count import statements in a source file."""
    return source_structure(db, path)[3]


@query
def summary(db: Database, path: str) -> str:
    """Produce a summary string combining function and import counts."""
    funcs = count_functions(db, path)
    imports = count_imports(db, path)
    return f"{funcs} functions, {imports} imports"


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------


def _banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sample = Path(tmpdir) / "sample.py"

        # --- Phase 1: Initial computation ---
        _banner("Phase 1: Initial computation")

        sample.write_text(
            textwrap.dedent("""\
                import os
                import sys

                def greet(name):
                    return f"Hello, {name}"
            """),
            encoding="utf-8",
        )

        db = Database(mode="strict")
        result = db.get(summary, str(sample))
        print(f"Result: {result}")
        print("\nProvenance tree:")
        print(db.explain(summary, str(sample)))

        # --- Phase 2: Exact raw edit → parsed backdating (early cutoff) ---
        _banner("Phase 2: Exact raw edit → parsed backdating (early cutoff)")

        print("Adding a comment to the source file...")
        sample.write_text(
            textwrap.dedent("""\
                import os
                import sys

                # This is a new comment — it changes the raw text
                # but the AST structure is identical.

                def greet(name):
                    return f"Hello, {name}"
            """),
            encoding="utf-8",
        )

        result2 = db.get(summary, str(sample))
        print(f"Result: {result2}")

        node = db.inspect(summary, str(sample))
        print(f"\nsummary decision: {node.last_decision}")
        print(f"  raw source recompute: {db.inspect(read_source, str(sample)).last_recompute}")
        print(
            "  parsed payload recompute: "
            f"{db.inspect(source_structure, str(sample)).last_recompute}"
        )
        print("  (the exact raw node changed; the complete AST payload backdated,")
        print("   so count queries and the summary were reused)")
        print("\nRecorded dependency explanation:")
        print(db.explain(summary, str(sample)))

        # --- Phase 3: Structural edit → selective recomputation ---
        _banner("Phase 3: Structural edit → selective recomputation")

        print("Adding a new function and a new import...")
        sample.write_text(
            textwrap.dedent("""\
                import os
                import sys
                import json

                # This is a new comment — it changes the raw text
                # but the AST structure is identical.

                def greet(name):
                    return f"Hello, {name}"

                def farewell(name):
                    return f"Goodbye, {name}"
            """),
            encoding="utf-8",
        )

        result3 = db.get(summary, str(sample))
        print(f"Result: {result3}")
        print("\nProvenance (structural change forces recomputation):")
        print(db.explain(summary, str(sample)))

        # --- Phase 4: Untracked read enforcement ---
        _banner("Phase 4: Untracked read enforcement")

        @query
        def unsafe_query(db: Database, path: str) -> str:
            # This raw open() bypasses the resource API — pyinc catches it.
            with open(path) as f:  # noqa: SIM115
                return f.read()

        print("Attempting raw open() inside a query...")
        try:
            db.get(unsafe_query, str(sample))
            print("ERROR: should have raised!")
        except UntrackedReadError as exc:
            print(f"Caught UntrackedReadError: {exc}")
            print("\n  pyinc patches builtins.open during query execution.")
            print("  Tracked file-content dependencies go through a Resource to maintain")
            print("  the from-scratch consistency guarantee.")

        # --- Phase 5: Mutation protection (strict mode) ---
        _banner("Phase 5: Mutation protection (strict mode)")

        @query
        def get_config(db: Database) -> Mapping[str, int]:
            return {"a": 1, "b": 2}

        frozen_result: Mapping[str, int] = db.get(get_config)
        print(f"Query returned: {frozen_result} (type: {type(frozen_result).__name__})")
        print(f"  frozen_result['a'] = {frozen_result['a']}")
        print("\nAttempting to mutate the frozen result...")
        try:
            frozen_result["c"] = 3  # type: ignore[index]
            print("ERROR: should have raised!")
        except TypeError as exc:
            print(f"Caught TypeError: {exc}")
            print("\n  In strict mode, query results are frozen at the boundary.")
            print("  Lists become FrozenList, dicts become FrozenDict, etc.")
            print("  This prevents external aliases from corrupting cached state.")

        # --- Phase 6: Provenance summary ---
        _banner("Phase 6: Final provenance inspection")

        node = db.inspect(summary, str(sample))
        print(f"Query: {node.label}")
        print(f"  Kind: {node.kind}")
        print(f"  Last decision: {node.last_decision}")
        print(f"  Changed at revision: {node.changed_at}")
        print(f"  Verified at revision: {node.verified_at}")
        print(f"  Dependencies: {len(node.dependencies)}")
        for dep in node.dependencies:
            print(f"    - {dep.label}: {dep.last_decision}")

        print("\n--- Demo complete ---")


if __name__ == "__main__":
    main()
