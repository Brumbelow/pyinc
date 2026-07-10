from __future__ import annotations

from typing import Any, cast

import pytest

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
