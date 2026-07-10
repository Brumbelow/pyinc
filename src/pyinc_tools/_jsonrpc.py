from __future__ import annotations

import json
from typing import Any, BinaryIO, cast


class ParseError(ValueError):
    """A framed message could not be decoded as JSON."""


class InvalidRequest(ValueError):
    """A decoded JSON value is not a valid JSON-RPC request object."""


_MAX_HEADER_BYTES = 64 * 1024
_MAX_CONTENT_LENGTH = 16 * 1024 * 1024


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


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
    if len(significant_length) > len(str(_MAX_CONTENT_LENGTH)):
        raise ParseError(f"Content-Length exceeds the {_MAX_CONTENT_LENGTH}-byte JSON-RPC limit")
    try:
        length = int(content_length)
    except (ValueError, OverflowError) as exc:
        raise ParseError("Content-Length must be a non-negative integer") from exc
    if length > _MAX_CONTENT_LENGTH:
        raise ParseError(f"Content-Length exceeds the {_MAX_CONTENT_LENGTH}-byte JSON-RPC limit")
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
        raise ParseError("invalid UTF-8 JSON-RPC body") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("JSON-RPC request must be an object")
    return cast(dict[str, Any], payload)


def validate_request(message: dict[str, Any]) -> None:
    """Validate the base JSON-RPC request envelope."""

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
    "InvalidRequest",
    "ParseError",
    "read_message",
    "validate_request",
    "write_message",
]
