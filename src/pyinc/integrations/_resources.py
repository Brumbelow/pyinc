from __future__ import annotations

import hashlib
from pathlib import Path


def file_read_snapshot(path: str, encoding: str) -> tuple[tuple[str, str] | tuple[str], str | None]:
    """Read a text resource once and derive its probe from those exact bytes."""

    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return ("missing",), None
    return ("present", hashlib.sha256(raw).hexdigest()), raw.decode(encoding)


__all__ = ["file_read_snapshot"]
