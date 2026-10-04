from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pyinc.resources import _read_file, _reads_as_missing

FileProbe = tuple[str, str] | tuple[str]


def file_bytes(path: str) -> bytes | None:
    """Read a file resource's bytes, reporting an unreadable kind as absent.

    A directory, or a path with a file somewhere in its parent chain, names no
    readable regular file, and reading it again will give the same result. It
    answers as an absent path does, which keeps the probe built on it total. A
    pipe, a socket and a device answer the same way. CPython has no OSError
    subclass for the socket's errno, so that case is matched by errno. Any
    other OSError propagates. The read is shared with the kernel file resources
    so both classify a failed read the same way. Platform differences make
    that harder than it looks.
    """

    return _read_file(path)


def file_probe(path: str) -> FileProbe:
    """Probe a file resource from a hash of its raw, undecoded bytes."""

    raw = file_bytes(path)
    if raw is None:
        return ("missing",)
    return ("present", hashlib.sha256(raw).hexdigest())


def file_text(path: str, encoding: str) -> str | None:
    """Read a text resource, reporting an unreadable kind as absent.

    ``Path.read_text`` does the decoding, so a load keeps its existing newline
    handling. ``file_read_snapshot`` decodes the bytes it hashed, and the two
    give different text: a text read turns CRLF and a lone CR into a newline,
    while decoding the bytes keeps them. So the kind check runs first as a
    separate read, on the same terms as the byte read. A pipe, a socket or a
    device then answers absent here too, where a text read could block
    forever. An ordinary file is read twice, which is the cost of keeping the
    newline handling. The second read is skipped only when the kind check found
    nothing readable and the path is also not a file. The file check is there
    because a path that became a file between the two reads has a text answer
    to give.
    """

    if file_bytes(path) is None and not os.path.isfile(path):
        return None
    try:
        return Path(path).read_text(encoding=encoding)
    except OSError as exc:
        if _reads_as_missing(path, exc):
            return None
        raise


def file_read_snapshot(path: str, encoding: str) -> tuple[FileProbe, str | None]:
    """Read a text resource once and derive its probe from those exact bytes."""

    raw = file_bytes(path)
    if raw is None:
        return ("missing",), None
    return ("present", hashlib.sha256(raw).hexdigest()), raw.decode(encoding)


__all__ = ["FileProbe", "file_bytes", "file_probe", "file_read_snapshot", "file_text"]
