"""Evaluate PEP 508 markers in a requirements.txt against the active interpreter.

Demonstrates the ``requirement_evaluation`` integration. The analysis reports
which requirements apply to the current Python environment and which ones
markers like ``; python_version < "3.9"`` exclude. For each applicable
requirement, it also reports whether the installed version satisfies the
declared specifier, or ``missing`` when the distribution is not installed.

Run: ``python examples/applicable_requirements.py``
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations import applicable_requirements

REQUIREMENTS = """\
# Evaluated against the active interpreter.
requests>=2.0; python_version >= "3.8"
backports.zoneinfo>=0.2.1; python_version < "3.9"
tomli>=2.0; python_version < "3.11"
packaging>=23.0
"""


def main() -> None:
    print(f"Active interpreter: {sys.version.split()[0]}")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "requirements.txt"
        path.write_text(REQUIREMENTS, encoding="utf-8")

        db = Database(mode="strict")
        analysis = applicable_requirements(db, path)

        env = analysis.environment
        print("Interpreter snapshot used for marker evaluation:")
        print(f"  python_version = {env.python_version}")
        print(f"  sys_platform   = {env.sys_platform}")
        print(f"  implementation = {env.implementation_name}")
        print()

        print(f"{'Name':<22s} {'Applicable':<12s} {'Status':<16s} Detail")
        print("-" * 90)
        for req in analysis.requirements:
            print(f"{req.name:<22s} {str(req.applicable):<12s} {req.status:<16s} {req.detail}")

        if analysis.diagnostics:
            print()
            print("Diagnostics:")
            for code, detail in analysis.diagnostics:
                print(f"  [{code}] {detail}")


if __name__ == "__main__":
    main()
