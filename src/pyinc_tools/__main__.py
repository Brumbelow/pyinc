"""Allow ``python -m pyinc_tools`` as an alternative to the console script."""

from __future__ import annotations

from pyinc_tools.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
