"""Detect undeclared third-party imports in a Python workspace.

Demonstrates cross-integration composition: ``workspace_dependency_check``
composes ``python_source`` (import scanning) with ``installed_packages``
(site-packages discovery) to surface imports that are installed in the
environment but missing from the declared dependency list.

The finding comes from the ``.dist-info`` directories site-packages carries, so
the example needs pyinc installed as a distribution: a source tree reached only
through ``PYTHONPATH`` cannot produce it. Rather than report that nothing was
found and exit 0, the example fails and says why.

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
EXPECTED_DISTRIBUTION = "pyinc"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "app.py").write_text(SOURCE, encoding="utf-8")

        db = Database(mode="strict")
        analysis = workspace_dependency_check(db, str(root), DECLARED_DEPS)

        print(f"Declared:    {DECLARED_DEPS or '(none)'}")
        print(f"Statuses:    {len(analysis.statuses)}")
        print(f"Diagnostics: {len(analysis.diagnostics)}")
        print()
        print("Undeclared imports (installed but not declared):")
        for item in analysis.undeclared_imports:
            print(f"  - {item.import_name:<20s} -> distribution: {item.distribution_name}")

        found = {item.distribution_name for item in analysis.undeclared_imports}
        if EXPECTED_DISTRIBUTION not in found:
            raise SystemExit(
                f"undeclared_imports.py found no undeclared import of "
                f"{EXPECTED_DISTRIBUTION!r} (found {sorted(found)}). The example "
                f"needs {EXPECTED_DISTRIBUTION} installed as a distribution, so "
                f"that its .dist-info is visible in site-packages; a source tree "
                f"reached only through PYTHONPATH cannot produce the finding."
            )


if __name__ == "__main__":
    main()
