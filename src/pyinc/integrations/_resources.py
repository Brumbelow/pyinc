from __future__ import annotations

import hashlib
import io

from pyinc.resources import _read_file

FileProbe = tuple[str, str] | tuple[str]


def file_bytes(path: str) -> bytes | None:
    """Read a file resource's bytes, reporting an unreadable kind as absent.

    A path that is a directory, or that has a file somewhere in its parent
    chain, names no readable regular file and never will by being read again,
    so it answers the way an absent path does -- which is what keeps the probe
    built on it total. Any other OSError propagates. Shares the kernel file
    resources' read so the two classify a failed read the same way, which the
    platforms make less obvious than it sounds.
    """

    return _read_file(path)


def file_probe(path: str) -> FileProbe:
    """Probe a file resource from its exact bytes, without decoding them."""

    raw = file_bytes(path)
    if raw is None:
        return ("missing",)
    return ("present", hashlib.sha256(raw).hexdigest())


def file_text(path: str, encoding: str) -> str | None:
    """Read a text resource, reporting an unreadable kind as absent.

    The shared byte reader performs the nonblocking-open and descriptor-kind
    check. ``TextIOWrapper`` preserves ``Path.read_text``'s universal-newline
    behavior for direct ``load`` calls; ``file_read_snapshot`` decodes the
    exact bytes it hashed, as before.
    """

    raw = file_bytes(path)
    if raw is None:
        return None
    with io.TextIOWrapper(io.BytesIO(raw), encoding=encoding) as handle:
        return handle.read()


def file_read_snapshot(path: str, encoding: str) -> tuple[FileProbe, str | None]:
    """Read a text resource once and derive its probe from those exact bytes."""

    raw = file_bytes(path)
    if raw is None:
        return ("missing",), None
    return ("present", hashlib.sha256(raw).hexdigest()), raw.decode(encoding)


__all__ = ["FileProbe", "file_bytes", "file_probe", "file_read_snapshot", "file_text"]
