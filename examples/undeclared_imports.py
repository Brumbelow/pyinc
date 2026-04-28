"""Detect undeclared third-party imports in a Python workspace.

Demonstrates cross-integration composition: ``workspace_dependency_check``
composes ``python_source`` (import scanning) with ``installed_packages``
(site-packages discovery) to surface imports that are installed in the
environment but missing from the declared dependency list.

Run: ``python examples/undeclared_imports.py``
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations import workspace_dependency_check

SOURCE = '''\
"""A tiny app that imports pyinc but forgets to declare it."""
import json  # stdlib — not flagged
import os    # stdlib — not flagged

import pyinc  # installed third-party — NOT in DECLARED_DEPS below


def summary() -> str:
    return f"using {pyinc.__name__}"

print(os.getcwd(), json.dumps({"ok": True}))
'''

DECLARED_DEPS: tuple[str, ...] = ()  # user forgot to declare pyinc


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "app.py").write_text(SOURCE, encoding="utf-8")

        db = Database(mode="strict")
        analysis = workspace_dependency_check(db, str(root), DECLARED_DEPS)

        print(f"Workspace:   {root}")
        print(f"Declared:    {DECLARED_DEPS or '(none)'}")
        print(f"Statuses:    {len(analysis.statuses)}")
        print(f"Diagnostics: {len(analysis.diagnostics)}")
        print()
        print("Undeclared imports (installed but not declared):")
        if not analysis.undeclared_imports:
            print("  (none — all imports accounted for)")
        for item in analysis.undeclared_imports:
            print(
                f"  - {item.import_name:<20s} -> distribution: {item.distribution_name}"
            )


if __name__ == "__main__":
    main()
