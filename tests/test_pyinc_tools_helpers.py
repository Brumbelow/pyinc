from __future__ import annotations

import ast

import pytest

import pyinc_tools._document as document
import pyinc_tools._edits as edits
from pyinc.integrations import SourcePosition
from pyinc_tools._document import InvalidParams


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", ((0, 0, 0),)),
        ("a\nb\r\nc\rd", ((0, 1, 2), (2, 3, 5), (5, 6, 7), (7, 8, 8))),
        ("trailing\n", ((0, 8, 9), (9, 9, 9))),
    ],
)
def test_source_line_bounds_support_every_python_line_ending(
    source: str, expected: tuple[tuple[int, int, int], ...]
) -> None:
    assert document._source_line_bounds(source) == expected
    assert document._source_line_count(source) == len(expected)


@pytest.mark.parametrize(
    ("line", "character", "expected"),
    [
        (0, 0, 0),
        (0, 1, 1),
        (1, 1, 4),
        (-1, 0, None),
        (3, 0, None),
        (0, -1, None),
        (0, 2, None),
        (True, 0, None),
        (0, False, None),
    ],
)
def test_source_position_to_offset_validates_coordinates(
    line: int, character: int, expected: int | None
) -> None:
    assert document._source_position_to_offset("a\r\nb", line, character) == expected


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, SourcePosition(0, 0)),
        (1, SourcePosition(0, 1)),
        (2, SourcePosition(0, 1)),
        (3, SourcePosition(1, 0)),
        (4, SourcePosition(1, 1)),
    ],
)
def test_source_offset_to_position_clamps_inside_line_endings(
    offset: int, expected: SourcePosition
) -> None:
    assert document._source_offset_to_position("a\r\nb", offset) == expected


@pytest.mark.parametrize("offset", [-1, 5])
def test_source_offset_to_position_rejects_out_of_bounds(offset: int) -> None:
    with pytest.raises(ValueError, match="outside the document"):
        document._source_offset_to_position("a\r\nb", offset)


def test_source_offset_to_position_requires_an_integer() -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        document._source_offset_to_position("text", True)


def test_source_line_navigation_and_replacement() -> None:
    source = "first\r\nsecond\rthird"

    assert document._next_source_line_start(source, 0) == 7
    assert document._next_source_line_start(source, 7) == 14
    assert document._next_source_line_start(source, 14) is None
    assert document._replace_source_line(source, 1, "changed") == "first\r\nchanged\rthird"
    assert document._replace_source_line(source, -1, "changed") == source
    assert document._replace_source_line(source, 3, "changed") == source


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        (None, "utf-16"),
        ({}, "utf-16"),
        ({"general": []}, "utf-16"),
        ({"general": {"positionEncodings": "utf-8"}}, "utf-16"),
        ({"general": {"positionEncodings": ["unknown"]}}, "utf-16"),
        ({"general": {"positionEncodings": ["unknown", "utf-32", "utf-8"]}}, "utf-32"),
        ({"offsetEncoding": "utf-8"}, "utf-8"),
        ({"offsetEncoding": ["utf-32"]}, "utf-32"),
        ({"offsetEncoding": 7}, "utf-16"),
    ],
)
def test_position_encoding_negotiation_handles_modern_and_legacy_clients(
    capabilities: object, expected: str
) -> None:
    assert document.negotiate_position_encoding(capabilities) == expected


def test_convert_payload_positions_recurses_through_lsp_uri_shapes() -> None:
    sources = {
        "file:///one.py": "😀x\n",
        "file:///two.py": "😀y\n",
    }
    payload = {
        "uri": "file:///one.py",
        "selection": {"line": 0, "character": 1},
        "textDocument": {"uri": "file:///two.py"},
        "changes": {
            "file:///one.py": [
                {
                    "range": {
                        "start": {"line": 0, "character": 1},
                        "end": {"line": 0, "character": 2},
                    }
                }
            ]
        },
        "from": {"uri": "file:///one.py"},
        "fromRanges": [{"line": 0, "character": 1}],
        "untouched": [None, "value", 3],
    }

    converted = document.convert_payload_positions(
        payload,
        encoding="utf-16",
        to_client=True,
        source_for_uri=sources.get,
    )

    assert converted["selection"] == {"line": 0, "character": 2}
    assert converted["changes"]["file:///one.py"][0]["range"] == {
        "start": {"line": 0, "character": 2},
        "end": {"line": 0, "character": 3},
    }
    assert converted["fromRanges"] == [{"line": 0, "character": 2}]
    assert converted["untouched"] == [None, "value", 3]


def test_convert_payload_positions_handles_scalar_folding_coordinates() -> None:
    payload = {
        "uri": "file:///mod.py",
        "startLine": 0,
        "startCharacter": 2,
        "endLine": 0,
        "endCharacter": 3,
        "kind": "region",
    }

    converted = document.convert_payload_positions(
        payload,
        encoding="utf-16",
        to_client=False,
        source_for_uri=lambda _uri: "😀x\n",
    )

    assert converted == {
        "uri": "file:///mod.py",
        "startLine": 0,
        "startCharacter": 1,
        "endLine": 0,
        "endCharacter": 2,
        "kind": "region",
    }


def test_convert_payload_positions_leaves_coordinates_without_source_unchanged() -> None:
    payload = {
        "startLine": 1,
        "startCharacter": 2,
        "position": {"line": 3, "character": 4},
    }

    assert (
        document.convert_payload_positions(
            payload,
            encoding="utf-8",
            to_client=True,
            source_for_uri=lambda _uri: None,
        )
        == payload
    )

    position = {"uri": "file:///missing.py", "line": 3, "character": 4}
    assert (
        document.convert_payload_positions(
            position,
            encoding="utf-8",
            to_client=True,
            source_for_uri=lambda _uri: None,
        )
        == position
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"startLine": 0}, "require both startLine and startCharacter"),
        ({"startCharacter": 0}, "require both startLine and startCharacter"),
        ({"endLine": 0}, "require both endLine and endCharacter"),
        ({"startLine": True, "startCharacter": 0}, "must be integers"),
        ({"line": 0}, "require both line and character"),
        ({"character": 0}, "require both line and character"),
        ({"line": False, "character": 0}, "must be integers"),
    ],
)
def test_convert_payload_positions_rejects_incomplete_or_noninteger_positions(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidParams, match=message):
        document.convert_payload_positions(
            payload,
            encoding="utf-16",
            to_client=False,
            source_for_uri=lambda _uri: "text",
            uri="file:///mod.py",
        )


def test_convert_payload_positions_wraps_document_map_errors() -> None:
    with pytest.raises(InvalidParams, match="outside"):
        document.convert_payload_positions(
            {"startLine": 5, "startCharacter": 0},
            encoding="utf-16",
            to_client=False,
            source_for_uri=lambda _uri: "text",
            uri="file:///mod.py",
        )

    with pytest.raises(InvalidParams, match="outside"):
        document.convert_payload_positions(
            {"line": 5, "character": 0},
            encoding="utf-16",
            to_client=False,
            source_for_uri=lambda _uri: "text",
            uri="file:///mod.py",
        )


@pytest.mark.parametrize(
    ("importer_module", "importer_path", "level", "module", "expected"),
    [
        ("pkg.mod", "/root/pkg/mod.py", 0, "external.mod", "external.mod"),
        ("pkg.mod", "/root/pkg/mod.py", 0, None, None),
        ("pkg.mod", "/root/pkg/mod.py", 1, "sibling", "pkg.sibling"),
        ("pkg", "/root/pkg/__init__.py", 1, "child", "pkg.child"),
        ("pkg.sub.mod", "/root/pkg/sub/mod.py", 2, None, "pkg"),
        ("pkg.mod", "/root/pkg/mod.py", 3, "target", None),
        ("", "/root/mod.py", 1, None, ""),
    ],
)
def test_resolve_import_from_target(
    importer_module: str,
    importer_path: str,
    level: int,
    module: str | None,
    expected: str | None,
) -> None:
    assert (
        edits._resolve_import_from_target(
            importer_module=importer_module,
            importer_path=importer_path,
            level=level,
            module=module,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("importer_module", "importer_path", "level", "expected"),
    [
        ("pkg.mod", "/root/pkg/mod.py", 0, ""),
        ("pkg.mod", "/root/pkg/mod.py", 1, "pkg"),
        ("pkg", "/root/pkg/__init__.py", 1, "pkg"),
        ("pkg.sub.mod", "/root/pkg/sub/mod.py", 2, "pkg"),
        ("pkg.mod", "/root/pkg/mod.py", 3, None),
        ("", "/root/mod.py", 1, ""),
    ],
)
def test_relative_import_anchor(
    importer_module: str,
    importer_path: str,
    level: int,
    expected: str | None,
) -> None:
    assert (
        edits._relative_import_anchor(
            importer_module=importer_module,
            importer_path=importer_path,
            level=level,
        )
        == expected
    )


def _from_import_node(source: str = "from pkg.mod import value") -> ast.ImportFrom:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ImportFrom)
    return node


def test_find_from_module_span_locates_absolute_and_relative_modules() -> None:
    absolute = _from_import_node()
    relative = _from_import_node("from ..pkg import value")

    assert edits._find_from_module_span(["from pkg.mod import value"], absolute) == (0, 5, 12)
    assert edits._find_from_module_span(["from ..pkg import value"], relative) == (0, 5, 10)


@pytest.mark.parametrize(
    ("line", "mutations"),
    [
        ("from pkg.mod import value", {"lineno": 3}),
        ("from pkg.mod import value", {"level": 0, "module": None}),
        ("xxxx pkg.mod import value", {}),
        ("from other import value", {}),
        ("from pkg.mod_more import value", {}),
    ],
)
def test_find_from_module_span_rejects_ambiguous_or_mismatched_source(
    line: str, mutations: dict[str, object]
) -> None:
    node = _from_import_node()
    for name, value in mutations.items():
        setattr(node, name, value)

    assert edits._find_from_module_span([line], node) is None


def test_import_node_for_line_uses_full_statement_span() -> None:
    first = ast.parse("import first").body[0]
    multiline = ast.parse("from pkg import (\n    first,\n    second,\n)").body[0]
    assert isinstance(first, ast.Import)
    assert isinstance(multiline, ast.ImportFrom)

    assert edits._import_node_for_line([first, multiline], None) is None
    assert edits._import_node_for_line([first, multiline], 3) is multiline
    assert edits._import_node_for_line([first], 10) is None
    first.end_lineno = None
    assert edits._import_node_for_line([first], 1) is first


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("pass\n", frozenset()),
        ("value = 1\n", frozenset()),
        ("__all__: list[str] = ['one', 'two']\n", frozenset({"one", "two"})),
        ("__all__ = {'one', 'two'}\n", frozenset({"one", "two"})),
        ("__all__ = ('one',)\n__all__ = ['two']\n", frozenset({"two"})),
        ("__all__: list[str]\n", frozenset()),
        ("__all__ = build_names()\n", frozenset()),
        ("__all__ = ['one', 2]\n", frozenset()),
    ],
)
def test_static_module_all_names_accepts_only_string_literal_collections(
    source: str, expected: frozenset[str]
) -> None:
    assert edits._static_module_all_names(ast.parse(source)) == expected


def test_statement_line_span_handles_missing_and_clamped_end_positions() -> None:
    source = "from pkg import (\n    one,\n    two,\n)"
    node = _from_import_node(source)

    assert edits._statement_line_span(source, node) == (0, 4)
    node.end_lineno = 100
    assert edits._statement_line_span(source, node) == (0, 4)
    node.end_lineno = None
    assert edits._statement_line_span(source, node) is None


def _aliases(source: str) -> list[ast.alias]:
    node = ast.parse(source).body[0]
    assert isinstance(node, (ast.Import, ast.ImportFrom))
    return node.names


def test_alias_list_deletion_absorbs_the_next_or_previous_live_alias() -> None:
    source = "from pkg import one, two, three"
    aliases = _aliases(source)

    middle = edits._alias_list_deletion_edits(
        importer_path="mod.py", source=source, aliases=aliases, dead_indices=[1]
    )
    last = edits._alias_list_deletion_edits(
        importer_path="mod.py", source=source, aliases=aliases, dead_indices=[2]
    )

    assert middle[0].range.start == SourcePosition(0, 21)
    assert middle[0].range.end == SourcePosition(0, 26)
    assert last[0].range.start == SourcePosition(0, 24)
    assert last[0].range.end == SourcePosition(0, 31)


def test_alias_list_deletion_coalesces_adjacent_dead_aliases() -> None:
    source = "from pkg import one, two, three, four"
    aliases = _aliases(source)

    result = edits._alias_list_deletion_edits(
        importer_path="mod.py", source=source, aliases=aliases, dead_indices=[1, 2]
    )

    assert len(result) == 1
    assert result[0].range.start == SourcePosition(0, 21)
    assert result[0].range.end == SourcePosition(0, 33)


def test_alias_list_deletion_falls_back_to_the_alias_span_without_live_siblings() -> None:
    source = "import only"
    aliases = _aliases(source)

    result = edits._alias_list_deletion_edits(
        importer_path="mod.py", source=source, aliases=aliases, dead_indices=[0]
    )

    assert result[0].range.start == SourcePosition(0, 7)
    assert result[0].range.end == SourcePosition(0, 11)


def test_alias_list_deletion_skips_incomplete_ast_positions() -> None:
    aliases = _aliases("import one, two")
    aliases[0].end_lineno = None
    assert (
        edits._alias_list_deletion_edits(
            importer_path="mod.py", source="", aliases=aliases, dead_indices=[0]
        )
        == []
    )

    aliases = _aliases("import one, two")
    aliases[1].lineno = None  # type: ignore[assignment]
    assert (
        edits._alias_list_deletion_edits(
            importer_path="mod.py", source="", aliases=aliases, dead_indices=[0]
        )
        == []
    )

    aliases = _aliases("import one, two")
    aliases[0].end_lineno = None
    assert (
        edits._alias_list_deletion_edits(
            importer_path="mod.py", source="", aliases=aliases, dead_indices=[1]
        )
        == []
    )
