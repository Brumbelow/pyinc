from __future__ import annotations

import re
from typing import Any, cast

from pyinc.integrations import (
    DocumentMap,
    PositionEncoding,
    SourcePosition,
)

_SUPPORTED_ENCODINGS: tuple[PositionEncoding, ...] = ("utf-8", "utf-16", "utf-32")


class InvalidParams(ValueError):
    """The client supplied a position outside the negotiated document map."""


def _source_line_bounds(source: str) -> tuple[tuple[int, int, int], ...]:
    """Return ``(start, content_end, next_start)`` for every source line."""

    bounds: list[tuple[int, int, int]] = []
    start = 0
    for match in re.finditer(r"\r\n?|\n", source):
        bounds.append((start, match.start(), match.end()))
        start = match.end()
    bounds.append((start, len(source), len(source)))
    return tuple(bounds)


def _source_position_to_offset(source: str, line: int, character: int) -> int | None:
    if type(line) is not int or type(character) is not int:
        return None
    bounds = _source_line_bounds(source)
    if line < 0 or line >= len(bounds) or character < 0:
        return None
    start, content_end, _next_start = bounds[line]
    if character > content_end - start:
        return None
    return start + character


def _source_offset_to_position(source: str, offset: int) -> SourcePosition:
    if type(offset) is not int:
        raise TypeError("source offset must be an integer")
    if offset < 0 or offset > len(source):
        raise ValueError("source offset is outside the document")
    for line, (start, content_end, next_start) in enumerate(_source_line_bounds(source)):
        if offset <= content_end:
            return SourcePosition(line, offset - start)
        if offset < next_start:
            return SourcePosition(line, content_end - start)
    raise AssertionError("source line map did not contain the offset")


def _next_source_line_start(source: str, offset: int) -> int | None:
    match = re.search(r"\r\n?|\n", source[offset:])
    return None if match is None else offset + match.end()


def _replace_source_line(source: str, line: int, replacement: str) -> str:
    bounds = _source_line_bounds(source)
    if not 0 <= line < len(bounds):
        return source
    start, content_end, _next_start = bounds[line]
    return source[:start] + replacement + source[content_end:]


def _source_line_count(source: str) -> int:
    return len(_source_line_bounds(source))


def negotiate_position_encoding(capabilities: Any) -> PositionEncoding:
    """Choose the first client-preferred encoding supported by the server."""

    if not isinstance(capabilities, dict):
        return "utf-16"
    general = capabilities.get("general")
    offered = general.get("positionEncodings") if isinstance(general, dict) else None
    if not isinstance(offered, list):
        # Older clients used this non-standard field before the LSP capability
        # was standardized. Accepting it costs nothing and eases upgrades.
        offered = capabilities.get("offsetEncoding")
        if isinstance(offered, str):
            offered = [offered]
    if isinstance(offered, list):
        for value in offered:
            if value in _SUPPORTED_ENCODINGS:
                return cast(PositionEncoding, value)
    return "utf-16"


def convert_payload_positions(
    value: Any,
    *,
    encoding: PositionEncoding,
    to_client: bool,
    source_for_uri: Any,
    uri: str | None = None,
) -> Any:
    """Recursively convert protocol position objects for their document URI."""

    if isinstance(value, list):
        return [
            convert_payload_positions(
                item,
                encoding=encoding,
                to_client=to_client,
                source_for_uri=source_for_uri,
                uri=uri,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    current_uri = uri
    own_uri = value.get("uri")
    if isinstance(own_uri, str):
        current_uri = own_uri
    text_document = value.get("textDocument")
    if isinstance(text_document, dict) and isinstance(text_document.get("uri"), str):
        current_uri = text_document["uri"]

    scalar_position_fields = (
        ("startLine", "startCharacter"),
        ("endLine", "endCharacter"),
    )
    if any(
        line_key in value or character_key in value
        for line_key, character_key in scalar_position_fields
    ):
        converted_value = dict(value)
        source = source_for_uri(current_uri) if current_uri is not None else None
        document = DocumentMap(source) if source is not None else None
        for line_key, character_key in scalar_position_fields:
            if line_key not in value and character_key not in value:
                continue
            if line_key not in value or character_key not in value:
                raise InvalidParams(f"positions require both {line_key} and {character_key}")
            line = value[line_key]
            character = value[character_key]
            if type(line) is not int or type(character) is not int:
                raise InvalidParams(f"{line_key} and {character_key} must be integers")
            if document is None:
                continue
            try:
                position = SourcePosition(line, character)
                converted = (
                    document.from_codepoint(position, encoding)
                    if to_client
                    else document.to_codepoint(position, encoding)
                )
            except (TypeError, ValueError) as exc:
                raise InvalidParams(str(exc)) from exc
            converted_value[line_key] = converted.line
            converted_value[character_key] = converted.character
        value = converted_value

    has_position_field = "line" in value or "character" in value
    if has_position_field:
        if "line" not in value or "character" not in value:
            raise InvalidParams("positions require both line and character")
        line = value["line"]
        character = value["character"]
        if type(line) is not int or type(character) is not int:
            raise InvalidParams("line and character must be integers")
    if has_position_field and current_uri is not None:
        source = source_for_uri(current_uri)
        if source is not None:
            document = DocumentMap(source)
            try:
                position = SourcePosition(line, character)
                converted = (
                    document.from_codepoint(position, encoding)
                    if to_client
                    else document.to_codepoint(position, encoding)
                )
            except (TypeError, ValueError) as exc:
                raise InvalidParams(str(exc)) from exc
            converted_payload = dict(value)
            converted_payload["line"] = converted.line
            converted_payload["character"] = converted.character
            return converted_payload

    result: dict[str, Any] = {}
    from_item = value.get("from")
    from_uri = (
        from_item.get("uri")
        if isinstance(from_item, dict) and isinstance(from_item.get("uri"), str)
        else None
    )
    for key, item in value.items():
        if key == "changes" and isinstance(item, dict):
            result[key] = {
                change_uri: convert_payload_positions(
                    edits,
                    encoding=encoding,
                    to_client=to_client,
                    source_for_uri=source_for_uri,
                    uri=change_uri,
                )
                for change_uri, edits in item.items()
            }
            continue
        item_uri = from_uri if key == "fromRanges" and from_uri is not None else current_uri
        result[key] = convert_payload_positions(
            item,
            encoding=encoding,
            to_client=to_client,
            source_for_uri=source_for_uri,
            uri=item_uri,
        )
    return result


__all__ = ["InvalidParams", "convert_payload_positions", "negotiate_position_encoding"]
