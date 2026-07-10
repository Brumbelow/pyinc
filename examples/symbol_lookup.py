"""Resolve a source position through a cross-module re-export chain.

Demonstrates the ``symbol_resolution`` integration: given a position on a name
exported from a facade module, the analysis follows ``from X import Y`` chains
(with cycle detection and a bounded follow depth) back to where the symbol
was originally defined.

Run: ``python examples/symbol_lookup.py``
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations import SourcePosition, symbol_at

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
        symbol = symbol_at(
            db,
            str(root),
            str(facade_path),
            SourcePosition(1, 34),
        )
        assert symbol is not None

        print("Starting module:  facade")
        print("Looking up:       process")
        print()
        print(f"Defining path:    {symbol.path}")
        print(f"Defining position: {symbol.declaration.start}")


if __name__ == "__main__":
    main()
