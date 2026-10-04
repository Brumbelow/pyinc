from __future__ import annotations

import ntpath
from pathlib import PurePath
from types import ModuleType


def is_stdlib_path(value: object) -> bool:
    """Return whether ``value`` is one of pathlib's own immutable path types."""

    return isinstance(value, PurePath) and type(value).__module__ in {
        "pathlib",
        "pathlib._local",
    }


def is_fully_qualified(path: str | bytes, path_module: ModuleType) -> bool:
    r"""Return whether ``path`` names one place whatever the working directory.

    On POSIX that is an absolute path. On Windows the answer follows Windows'
    own path types, because ``ntpath.isabs`` and ``ntpath.splitdrive`` changed
    their answers between versions. These qualify: a drive with a root
    (``C:\x``), a UNC or device path (``\\server\share``, ``\\?\...``,
    ``\\.\...``), and the null device, which ``realpath`` answers before it
    anchors anything. A drive-relative ``C:x`` depends on that drive's working
    directory, and a rooted ``\x`` on the working directory's drive, so neither
    qualifies.
    """

    normalized = path_module.normpath(path)
    if path_module is not ntpath:
        return bool(path_module.isabs(normalized))
    is_bytes = isinstance(normalized, bytes)
    sep = b"\\" if is_bytes else "\\"
    # Windows 3.11 and 3.12 normalise in C, which keeps a leading "/" before a
    # ":" (normpath("/:/x") is "/:\\x"). Windows reads both separators alike.
    normalized = normalized.replace(b"/" if is_bytes else "/", sep)
    if normalized.startswith(sep * 2):
        return True
    if path_module.normcase(normalized) == (b"nul" if is_bytes else "nul"):
        return True
    return bool(normalized[:1] != sep and normalized[1:3] == (b":\\" if is_bytes else ":\\"))


__all__ = ["is_fully_qualified", "is_stdlib_path"]
