from __future__ import annotations

import tokenize
from typing import Any, cast

import pytest

from pyinc._python_lexing import identifier_tokens
from pyinc.integrations import DocumentMap, SourcePosition, SourceRange


@pytest.mark.parametrize(
    ("line", "character"),
    [(-1, 0), (0, -1)],
)
def test_source_position_rejects_negative_coordinates(line: int, character: int) -> None:
    with pytest.raises(ValueError, match="negative"):
        SourcePosition(line, character)


@pytest.mark.parametrize(
    ("line", "character"),
    [(True, 0), (0, False), (1.0, 0), (0, 1.0), ("0", 0)],
)
def test_source_position_requires_integer_coordinates(line: Any, character: Any) -> None:
    with pytest.raises(TypeError, match="integers"):
        SourcePosition(line, character)


def test_source_range_validates_endpoint_types_and_order() -> None:
    with pytest.raises(TypeError, match="SourcePosition"):
        SourceRange(cast(Any, (0, 0)), SourcePosition(0, 1))
    with pytest.raises(ValueError, match="before"):
        SourceRange(SourcePosition(1, 0), SourcePosition(0, 1))


def test_document_map_converts_ast_utf8_bytes_at_codepoint_boundaries() -> None:
    document = DocumentMap("é😀x\r\nnext\rthird\n")

    assert document.from_ast(1, 0) == SourcePosition(0, 0)
    assert document.from_ast(1, 2) == SourcePosition(0, 1)
    assert document.from_ast(1, 6) == SourcePosition(0, 2)
    assert document.line(1) == "next"
    assert document.line(2) == "third"

    with pytest.raises(ValueError, match="UTF-8 boundary"):
        document.from_ast(1, 1)
    with pytest.raises(ValueError, match="beyond"):
        document.from_ast(1, 8)
    with pytest.raises(ValueError, match="negative"):
        document.from_ast(1, -1)


def test_document_map_converts_all_lsp_position_encodings() -> None:
    document = DocumentMap("é😀x")
    codepoint = SourcePosition(0, 2)

    assert document.from_codepoint(codepoint, "utf-8") == SourcePosition(0, 6)
    assert document.from_codepoint(codepoint, "utf-16") == SourcePosition(0, 3)
    assert document.from_codepoint(codepoint, "utf-32") == codepoint
    assert document.to_codepoint(SourcePosition(0, 6), "utf-8") == codepoint
    assert document.to_codepoint(SourcePosition(0, 3), "utf-16") == codepoint

    with pytest.raises(ValueError, match="splits"):
        document.to_codepoint(SourcePosition(0, 1), "utf-8")
    with pytest.raises(ValueError, match="splits"):
        document.to_codepoint(SourcePosition(0, 2), "utf-16")


def test_document_map_rejects_unknown_encodings_even_for_empty_lines() -> None:
    document = DocumentMap("")
    invalid = cast(Any, "latin-1")

    with pytest.raises(ValueError, match="unsupported position encoding"):
        document.to_codepoint(SourcePosition(0, 0), invalid)
    with pytest.raises(ValueError, match="unsupported position encoding"):
        document.from_codepoint(SourcePosition(0, 0), invalid)


def test_identifier_tokens_repair_unicode_spans_and_exclude_non_code() -> None:
    source = 'e\u0301x = ℘1\na·b = e\u0301x\ntext = f"e\u0301x"\n# ℘1 a·b\n'

    assert [(token.string, token.start, token.end) for token in identifier_tokens(source)] == [
        ("e\u0301x", (1, 0), (1, 3)),
        ("℘1", (1, 6), (1, 8)),
        ("a·b", (2, 0), (2, 3)),
        ("e\u0301x", (2, 6), (2, 9)),
        ("text", (3, 0), (3, 4)),
    ]
    assert identifier_tokens("'''unterminated") == ()


def test_identifier_tokens_repair_python_311_split_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = "e\u0301x ℘1 + tail\n"
    split_tokens = (
        tokenize.TokenInfo(tokenize.NAME, "e", (1, 0), (1, 1), line),
        tokenize.TokenInfo(tokenize.ERRORTOKEN, "\u0301", (1, 1), (1, 2), line),
        tokenize.TokenInfo(tokenize.NAME, "x", (1, 2), (1, 3), line),
        tokenize.TokenInfo(tokenize.ERRORTOKEN, " ", (1, 3), (1, 4), line),
        tokenize.TokenInfo(tokenize.ERRORTOKEN, "℘", (1, 4), (1, 5), line),
        tokenize.TokenInfo(tokenize.NUMBER, "1", (1, 5), (1, 6), line),
        tokenize.TokenInfo(tokenize.OP, "+", (1, 7), (1, 8), line),
        tokenize.TokenInfo(tokenize.NAME, "tail", (1, 9), (1, 13), line),
    )
    monkeypatch.setattr(
        tokenize,
        "generate_tokens",
        lambda _readline: iter(split_tokens),
    )

    assert [(token.string, token.start, token.end) for token in identifier_tokens(line)] == [
        ("e\u0301x", (1, 0), (1, 3)),
        ("℘1", (1, 4), (1, 6)),
        ("tail", (1, 9), (1, 13)),
    ]
