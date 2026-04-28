"""Follow a cross-module re-export chain to find a symbol's defining location.

Demonstrates the ``symbol_resolution`` integration: given a name exported
from a facade module, the analysis follows ``from X import Y`` chains
(with cycle detection and a bounded follow depth) back to where the symbol
was originally defined.

Run: ``python examples/symbol_lookup.py``
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations import resolve_symbol

ORIGIN = '''\
"""Defines the actual function."""


def process(items):
    return [x.strip() for x in items if x.strip()]
'''

MIDDLE = '''\
"""Re-exports from origin under a renamed alias."""
from origin import process as do_process
'''

FACADE = '''\
"""Public facade — users import from here."""
from middle import do_process as process
'''


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "origin.py").write_text(ORIGIN, encoding="utf-8")
        (root / "middle.py").write_text(MIDDLE, encoding="utf-8")
        facade_path = root / "facade.py"
        facade_path.write_text(FACADE, encoding="utf-8")

        db = Database(mode="strict")
        resolved = resolve_symbol(db, str(root), str(facade_path), "process")

        print("Starting module:  facade")
        print("Looking up:       process")
        print()
        print(f"Resolution kind:  {resolved.resolution}")
        print(f"Defining module:  {resolved.defining_module}")
        print(f"Defining path:    {resolved.defining_path}")
        print(f"Defining line:    {resolved.defining_lineno}")
        print(f"Follow depth:     {resolved.follow_depth}")
        print(f"Trail:            {' -> '.join(resolved.trail)}")


if __name__ == "__main__":
    main()
