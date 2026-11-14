from __future__ import annotations

import json
from typing import Any, BinaryIO, cast


class ParseError(ValueError):
    """A framed message failed to decode as JSON."""


class BodyParseError(ParseError):
    """A whole frame was read, but its body could not be taken.

    The stream is left at the start of the next frame, so a server can reply
    with a parse error and keep reading. A plain ParseError means the headers
    are broken and the next frame cannot be found.
    """


class InvalidRequest(ValueError):
    """A decoded JSON value is an invalid JSON-RPC request object."""


_MAX_HEADER_BYTES = 64 * 1024
_MAX_CONTENT_LENGTH = 16 * 1024 * 1024
# A longer Content-Length is read as a broken header. A body over the size
# limit but within this many digits is read and dropped instead.
_MAX_CONTENT_LENGTH_DIGITS = 10
_DISCARD_CHUNK_BYTES = 64 * 1024


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _discard_body(stream: BinaryIO, length: int) -> None:
    remaining = length
    while remaining:
        chunk = stream.read(min(remaining, _DISCARD_CHUNK_BYTES))
        if not chunk:
            raise ParseError("truncated JSON-RPC body")
        remaining -= len(chunk)


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one Content-Length-framed JSON-RPC message from ``stream``."""

    headers: dict[str, str] = {}
    saw_header = False
    header_bytes = 0
    while True:
        line = stream.readline(_MAX_HEADER_BYTES - header_bytes + 1)
        if not line:
            if not saw_header:
                return None
            raise ParseError("truncated JSON-RPC headers")
        saw_header = True
        header_bytes += len(line)
        if header_bytes > _MAX_HEADER_BYTES:
            raise ParseError("JSON-RPC headers exceed the size limit")
        try:
            decoded = line.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ParseError("JSON-RPC headers must be ASCII") from exc
        if not decoded:
            break
        key, separator, value = decoded.partition(":")
        if not separator or not key.strip():
            raise ParseError("malformed JSON-RPC header")
        normalized_key = key.strip().lower()
        if normalized_key in headers:
            raise ParseError(f"duplicate JSON-RPC header: {normalized_key}")
        headers[normalized_key] = value.strip()

    content_length = headers.get("content-length")
    if content_length is None:
        raise ParseError("missing Content-Length header")
    if not content_length.isdigit():
        raise ParseError("Content-Length must be a non-negative integer")
    significant_length = content_length.lstrip("0") or "0"
    if len(significant_length) > _MAX_CONTENT_LENGTH_DIGITS:
        raise ParseError(
            f"Content-Length exceeds the {_MAX_CONTENT_LENGTH_DIGITS}-digit JSON-RPC limit"
        )
    try:
        length = int(content_length)
    except (ValueError, OverflowError) as exc:
        raise ParseError("Content-Length must be a non-negative integer") from exc
    if length > _MAX_CONTENT_LENGTH:
        # The body is read and dropped, so the stream stays on a frame
        # boundary and the next message can still be read.
        _discard_body(stream, length)
        raise BodyParseError(
            f"Content-Length exceeds the {_MAX_CONTENT_LENGTH}-byte JSON-RPC limit"
        )
    body = stream.read(length)
    if len(body) != length:
        raise ParseError("truncated JSON-RPC body")
    try:
        decoded_body = body.decode("utf-8")
        payload = json.loads(decoded_body, parse_constant=_reject_json_constant)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
        OverflowError,
    ) as exc:
        raise BodyParseError("invalid UTF-8 JSON-RPC body") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("JSON-RPC request must be an object")
    return cast(dict[str, Any], payload)


def validate_request(message: dict[str, Any]) -> None:
    """Validate the top-level fields of a JSON-RPC request."""

    if message.get("jsonrpc") != "2.0":
        raise InvalidRequest("jsonrpc must be exactly '2.0'")
    method = message.get("method")
    if not isinstance(method, str):
        raise InvalidRequest("method must be a string")
    if "id" in message:
        request_id = message["id"]
        if request_id is not None and type(request_id) not in {int, str}:
            raise InvalidRequest("id must be an integer, string, or null")
    if "params" in message and not isinstance(message["params"], (dict, list)):
        raise InvalidRequest("params must be an object or array")


def write_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    """Write one Content-Length-framed JSON-RPC message to ``stream``."""

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


__all__ = [
    "BodyParseError",
    "InvalidRequest",
    "ParseError",
    "read_message",
    "validate_request",
    "write_message",
]
