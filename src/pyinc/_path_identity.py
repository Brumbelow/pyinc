from __future__ import annotations

from pathlib import PurePath


def is_stdlib_path(value: object) -> bool:
    """Return whether ``value`` is one of pathlib's own immutable path types."""

    return isinstance(value, PurePath) and type(value).__module__ in {
        "pathlib",
        "pathlib._local",
    }


__all__ = ["is_stdlib_path"]
