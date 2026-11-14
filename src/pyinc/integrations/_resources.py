from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TypeAlias

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


TextPayload: TypeAlias = tuple[str, str | None]


def text_payload(path: str, encoding: str) -> TextPayload:
    """Read a text resource as ``(text, decode_error)``.

    The text comes from ``file_text``, so its newline handling is unchanged. A
    file whose bytes do not decode answers empty text and a message made of the
    path and the codec's error, as ``python_source`` reports its source. The
    query that owns the file turns the message into its own diagnostic, so an
    undecodable file never raises out of an entrypoint. An absent file answers
    empty text and no error.
    """

    try:
        text = file_text(path, encoding)
    except UnicodeDecodeError as exc:
        return "", f"{path}: {exc}"
    return (text if text is not None else ""), None


def text_payload_snapshot(path: str, encoding: str) -> tuple[FileProbe, TextPayload]:
    """Read a text resource once and derive its probe from those exact bytes.

    The answer has the shape ``text_payload`` gives. The probe hashes the raw
    bytes whether or not they decode, so an edit to an undecodable file is
    still seen.
    """

    raw = file_bytes(path)
    if raw is None:
        return ("missing",), ("", None)
    probe: FileProbe = ("present", hashlib.sha256(raw).hexdigest())
    try:
        return probe, (raw.decode(encoding), None)
    except UnicodeDecodeError as exc:
        return probe, ("", f"{path}: {exc}")


__all__ = [
    "FileProbe",
    "TextPayload",
    "file_bytes",
    "file_probe",
    "file_read_snapshot",
    "file_text",
    "text_payload",
    "text_payload_snapshot",
]
