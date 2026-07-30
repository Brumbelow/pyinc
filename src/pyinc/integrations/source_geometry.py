from __future__ import annotations

import ast
import re
import tokenize
import unicodedata
from dataclasses import dataclass
from typing import Literal, TypeAlias

from pyinc._python_lexing import identifier_tokens

PositionEncoding: TypeAlias = Literal["utf-8", "utf-16", "utf-32"]


@dataclass(frozen=True, order=True)
class SourcePosition:
    """A zero-based position measured in Unicode code points."""

    line: int
    character: int

    def __post_init__(self) -> None:
        if type(self.line) is not int or type(self.character) is not int:
            raise TypeError("source position coordinates must be integers")
        if self.line < 0 or self.character < 0:
            raise ValueError("source positions cannot be negative")


@dataclass(frozen=True)
class SourceRange:
    """A zero-based, end-exclusive source range."""

    start: SourcePosition
    end: SourcePosition

    def __post_init__(self) -> None:
        if not isinstance(self.start, SourcePosition) or not isinstance(self.end, SourcePosition):
            raise TypeError("source ranges require SourcePosition endpoints")
        if self.end < self.start:
            raise ValueError("a source range cannot end before it starts")

    def contains(self, position: SourcePosition, *, include_end: bool = False) -> bool:
        if include_end:
            return self.start <= position <= self.end
        return self.start <= position < self.end


class DocumentMap:
    """Convert source coordinates between code points and LSP encodings."""

    def __init__(self, source: str) -> None:
        self.source = source
        # All Python and LSP line endings delimit lines without contributing to
        # the character coordinate. ``re.split`` preserves a final empty line.
        self.lines = tuple(re.split(r"\r\n?|\n", source))

    def line(self, line: int) -> str:
        if type(line) is not int:
            raise TypeError("line index must be an integer")
        if not 0 <= line < len(self.lines):
            raise ValueError(f"line {line} is outside the document")
        return self.lines[line]

    def from_ast(self, line: int, utf8_byte_character: int) -> SourcePosition:
        """Convert Python AST coordinates to the public code-point contract."""

        if type(line) is not int or type(utf8_byte_character) is not int:
            raise TypeError("AST coordinates must be integers")
        if line <= 0:
            raise ValueError("AST line numbers are one-based")
        if utf8_byte_character < 0:
            raise ValueError("AST columns cannot be negative")
        text = self.line(line - 1)
        encoded = text.encode("utf-8")
        if utf8_byte_character > len(encoded):
            raise ValueError("AST column is beyond the end of the line")
        prefix = encoded[:utf8_byte_character]
        try:
            character = len(prefix.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("AST column does not fall on a UTF-8 boundary") from exc
        return SourcePosition(line - 1, character)

    def ast_range(self, node: ast.AST) -> SourceRange:
        lineno = getattr(node, "lineno", None)
        col_offset = getattr(node, "col_offset", None)
        end_lineno = getattr(node, "end_lineno", None)
        end_col_offset = getattr(node, "end_col_offset", None)
        if not (
            isinstance(lineno, int)
            and isinstance(col_offset, int)
            and isinstance(end_lineno, int)
            and isinstance(end_col_offset, int)
        ):
            raise ValueError("AST node does not have a complete source range")
        return SourceRange(
            self.from_ast(lineno, col_offset),
            self.from_ast(end_lineno, end_col_offset),
        )

    def to_codepoint(self, position: SourcePosition, encoding: PositionEncoding) -> SourcePosition:
        _validate_encoding(encoding)
        text = self.line(position.line)
        target = position.character
        if encoding == "utf-32":
            if target > len(text):
                raise ValueError("position is beyond the end of the line")
            return position

        consumed = 0
        for index, character in enumerate(text):
            if consumed == target:
                return SourcePosition(position.line, index)
            consumed += _encoded_width(character, encoding)
            if consumed > target:
                raise ValueError(f"position splits a {encoding.upper()} character")
        if consumed == target:
            return SourcePosition(position.line, len(text))
        raise ValueError("position is beyond the end of the line")

    def from_codepoint(
        self, position: SourcePosition, encoding: PositionEncoding
    ) -> SourcePosition:
        _validate_encoding(encoding)
        text = self.line(position.line)
        if position.character > len(text):
            raise ValueError("position is beyond the end of the line")
        if encoding == "utf-32":
            return position
        character = sum(_encoded_width(item, encoding) for item in text[: position.character])
        return SourcePosition(position.line, character)

    def range_to_codepoint(
        self, source_range: SourceRange, encoding: PositionEncoding
    ) -> SourceRange:
        return SourceRange(
            self.to_codepoint(source_range.start, encoding),
            self.to_codepoint(source_range.end, encoding),
        )

    def range_from_codepoint(
        self, source_range: SourceRange, encoding: PositionEncoding
    ) -> SourceRange:
        return SourceRange(
            self.from_codepoint(source_range.start, encoding),
            self.from_codepoint(source_range.end, encoding),
        )


def _encoded_width(character: str, encoding: PositionEncoding) -> int:
    if encoding == "utf-8":
        return len(character.encode("utf-8"))
    if encoding == "utf-16":
        return len(character.encode("utf-16-le")) // 2
    if encoding == "utf-32":
        return 1
    raise ValueError(f"unsupported position encoding: {encoding!r}")


def _validate_encoding(encoding: object) -> None:
    if encoding not in ("utf-8", "utf-16", "utf-32"):
        raise ValueError(f"unsupported position encoding: {encoding!r}")


def ast_range(source: str, node: ast.AST) -> SourceRange:
    return DocumentMap(source).ast_range(node)


def identifier_range_in_tokens(
    document: DocumentMap,
    tokens: tuple[tokenize.TokenInfo, ...],
    node: ast.AST,
    name: str,
    *,
    reverse: bool = False,
) -> SourceRange:
    """identifier_range against a pre-computed token stream for document."""

    full = document.ast_range(node)
    candidates = [
        token
        for token in tokens
        if unicodedata.normalize("NFKC", token.string) == name
        and full.start <= SourcePosition(token.start[0] - 1, token.start[1])
        and SourcePosition(token.end[0] - 1, token.end[1]) <= full.end
    ]
    if reverse:
        candidates.reverse()
    if candidates:
        token = candidates[0]
        return SourceRange(
            SourcePosition(token.start[0] - 1, token.start[1]),
            SourcePosition(token.end[0] - 1, token.end[1]),
        )
    return SourceRange(full.start, full.start)


def identifier_range(
    source: str,
    node: ast.AST,
    name: str,
    *,
    reverse: bool = False,
) -> SourceRange:
    """Return the exact source spelling for an AST-normalized identifier."""

    document = DocumentMap(source)
    normalized_source = "\n".join(document.lines)
    return identifier_range_in_tokens(
        document, identifier_tokens(normalized_source), node, name, reverse=reverse
    )


__all__ = [
    "DocumentMap",
    "PositionEncoding",
    "SourcePosition",
    "SourceRange",
    "ast_range",
    "identifier_range",
    "identifier_range_in_tokens",
]
