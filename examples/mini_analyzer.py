"""Analyze a small workspace the example builds for itself.

Writes a fixed two-module workspace into a ``TemporaryDirectory`` and prints a
few named fields of the file-level and workspace-level analyses: the entry
point's imports and definitions, and for every module in the workspace how many
imports it has, what it defines, and which kinds of resolution its imports took.
Nothing is written outside the temporary directory, and every printed value is
derived from the workspace the example wrote, so what it prints does not depend
on what is installed or where it is run from.

Run: ``python examples/mini_analyzer.py``
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations import file_analysis, workspace_analysis

APP = '''\
"""Entry point for the toy workspace."""
import os

from helpers import greet


def main() -> str:
    return greet(os.sep)
'''

HELPERS = '''\
"""One helper the entry point imports."""


def greet(sep: str) -> str:
    return f"hello{sep}world"


CONSTANT = 3
'''


def build_workspace(root: Path) -> Path:
    """Write the fixed two-module workspace and return the entry point."""
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "helpers.py").write_text(HELPERS, encoding="utf-8")
    return root / "app.py"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        app = build_workspace(root)

        db = Database(mode="strict")

        one = file_analysis(db, app)
        print(f"file:          {Path(one.path).name}")
        print(f"  imports:     {tuple(i.module for i in one.imports)}")
        print(f"  definitions: {tuple(d.name for d in one.definitions)}")
        print(f"  diagnostics: {len(one.diagnostics)}")

        ws = workspace_analysis(db, root)
        print(f"workspace:     {len(ws.modules)} modules")
        for module in sorted(ws.modules, key=lambda m: m.module):
            resolutions = tuple(sorted({imp.resolution for imp in module.resolved_imports}))
            print(
                f"  {module.module:<8s} "
                f"imports={len(module.imports)} "
                f"definitions={tuple(d.name for d in module.definitions)} "
                f"resolutions={resolutions}"
            )


if __name__ == "__main__":
    main()
