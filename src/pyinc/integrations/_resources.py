from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pyinc.resources import _read_file, _reads_as_missing

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

    Decoding is left to ``Path.read_text`` so a load keeps the newline handling
    it has always had; ``file_read_snapshot`` decodes the bytes it hashed. The
    two are not interchangeable -- a text read translates CRLF and a lone CR
    into a newline where decoding the bytes keeps them -- so the kind check runs
    as a separate read in front, on the same terms the byte read uses, and a
    pipe, a socket or a device answers absent here too instead of never
    returning. An ordinary file is therefore read twice, which is what keeping
    the newline handling unchanged costs; the second read is skipped only when
    the kind check found nothing readable and the path is no file either, since
    a path that became one between the two reads has a text answer to give.
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
