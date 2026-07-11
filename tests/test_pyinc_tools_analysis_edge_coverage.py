from __future__ import annotations

import ast
from typing import cast

import pytest

import pyinc_tools._analysis as analysis
from pyinc import Database
from pyinc.integrations import (
    ModuleSymbolTable,
    Parameter,
    ResolvedImportRef,
    Signature,
    SourcePosition,
    SourceRange,
    Symbol,
    SymbolId,
)


def _range(start: int = 0, end: int = 1) -> SourceRange:
    return SourceRange(SourcePosition(0, start), SourcePosition(0, end))


def _symbol(
    name: str,
    kind: str = "function",
    *,
    source_module: str | None = None,
    source_name: str | None = None,
) -> Symbol:
    return Symbol(name, kind, _range(), None, None, source_module, source_name)  # type: ignore[arg-type]


def _resolved_import(
    *,
    path: str | None,
    resolution: str = "workspace",
    imported_name: str | None = "value",
) -> ResolvedImportRef:
    return ResolvedImportRef(
        module="provider",
        kind="from",
        range=_range(),
        imported_name=imported_name,
        resolved_module="provider",
        resolved_path=path,
        resolution=resolution,  # type: ignore[arg-type]
        distribution_name=None,
        distribution_version=None,
    )


def test_matching_import_without_source_module_needs_no_analysis() -> None:
    assert (
        analysis._matching_import(
            cast(Database, object()), "/workspace", "/workspace/mod.py", _symbol("alias")
        )
        is None
    )


def test_resolve_target_stops_cycles_before_symbol_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/workspace/mod.py"
    table = ModuleSymbolTable("mod", path, (), ())
    monkeypatch.setattr(analysis, "module_symbol_table", lambda *_args: table)

    result = analysis.resolve_target(
        cast(Database, object()),
        "/workspace",
        path,
        "value",
        _visited=frozenset({(path, "value")}),
    )

    assert result.resolution == "ambiguous"
    assert result.follow_depth == 1


def test_resolve_target_uses_known_symbol_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    path = "/workspace/mod.py"
    declaration = _range(4, 9)
    symbol_id = SymbolId(path, "mod", "value", declaration)
    table = ModuleSymbolTable("mod", path, (), ())
    monkeypatch.setattr(analysis, "module_symbol_table", lambda *_args: table)
    monkeypatch.setattr(analysis, "_resolve_at_known_positions", lambda *_args: symbol_id)

    result = analysis.resolve_target(cast(Database, object()), "/workspace", path, "value")

    assert result.resolution == "workspace"
    assert result.range == declaration


@pytest.mark.parametrize(
    ("matching", "expected"),
    [
        (None, "missing"),
        (_resolved_import(path=None), "missing"),
    ],
)
def test_resolve_target_handles_unmatched_or_pathless_import_aliases(
    matching: ResolvedImportRef | None,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/workspace/mod.py"
    symbol = _symbol("value", "from_import_alias", source_module="provider", source_name="value")
    table = ModuleSymbolTable("mod", path, (symbol,), ())
    monkeypatch.setattr(analysis, "module_symbol_table", lambda *_args: table)
    monkeypatch.setattr(analysis, "_resolve_at_known_positions", lambda *_args: None)
    monkeypatch.setattr(analysis, "_matching_import", lambda *_args: matching)

    result = analysis.resolve_target(cast(Database, object()), "/workspace", path, "value")

    assert result.resolution == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("   (", None),
        ("value](", None),
        ("123(", None),
        ("for(", None),
        ("prefix name(", "name"),
        (".func(", "func"),
        ("1.func(", "func"),
        ("pkg . inner.func(", "func"),
        ("pkg.func(", "pkg.func"),
        ("(func(", "func"),
    ],
)
def test_identifier_immediately_before_rejects_non_calls_and_handles_owners(
    source: str, expected: str | None
) -> None:
    assert analysis._identifier_immediately_before(source, len(source) - 1) == expected


@pytest.mark.parametrize("source", ["   (", "value]("])
def test_identifier_start_before_rejects_missing_identifier(source: str) -> None:
    assert analysis._identifier_start_before(source, len(source) - 1) is None


@pytest.mark.parametrize(
    ("source", "line", "character", "expected_name", "expected_parameter"),
    [
        ('foo("""text""", ', 0, 16, "foo", 1),
        ('foo("""unterminated', 0, 19, "foo", 0),
        ('foo("a\\"b", ', 0, 12, "foo", 1),
        ('foo("unterminated\nnext', 1, 4, "foo", 0),
        ("foo([), ", 0, 8, "foo", 0),
        ('foo("', 0, 5, "foo", 0),
    ],
)
def test_find_call_scanner_tolerates_strings_and_mismatched_brackets(
    source: str,
    line: int,
    character: int,
    expected_name: str,
    expected_parameter: int,
) -> None:
    result = analysis._find_call_at_position(source, line, character)
    assert result is not None
    assert result[:2] == (expected_name, expected_parameter)


def test_find_call_rejects_invalid_position() -> None:
    assert analysis._find_call_at_position("call()", 9, 0) is None


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ('"escaped \\" still open', True),
        ('"closed" value', False),
        ("value # comment", True),
        ("'unterminated", True),
    ],
)
def test_completion_string_comment_scanner(head: str, expected: bool) -> None:
    assert analysis._completion_head_in_string_or_comment(head) is expected


@pytest.mark.parametrize(
    ("source", "line", "character", "expected"),
    [
        ("value\n", 1, 0, ("name", "")),
        ("value\n", 3, 0, None),
        ("from pkg import bad-name\n", 0, 24, None),
        ("from pkg.sub\n", 0, 12, ("import_module", "pkg.sub")),
        ("import first, pkg.s\n", 0, 19, ("import_module", "pkg.s")),
        ("1.attr\n", 0, 6, None),
        ("pkg.sub.attr\n", 0, 12, ("attribute", "pkg.sub", "attr")),
    ],
)
def test_completion_context_edge_shapes(
    source: str, line: int, character: int, expected: tuple[str, ...] | None
) -> None:
    assert analysis._find_completion_context(source, line, character) == expected


def test_repair_caret_line_leaves_invalid_line_unchanged() -> None:
    source = "value = 1\n"
    assert analysis._repair_caret_line(source, -1) == source
    assert analysis._repair_caret_line(source, 5) == source


def test_signature_label_renders_unannotated_default() -> None:
    signature = Signature((Parameter("count", None),), None)
    label, parameters = analysis._build_signature_label("run", signature, {"count": "3"})
    assert label == "def run(count=3)"
    assert parameters[0].label == "count=3"


def test_parameter_defaults_handle_syntax_errors_missing_names_and_keyword_only_values() -> None:
    assert analysis._parameter_defaults_from_source("def broken(", 1, "broken") is None
    assert analysis._parameter_defaults_from_source("def other(): pass\n", 1, "missing") is None
    defaults = analysis._parameter_defaults_from_source(
        "def target(a=1, *, required, optional='yes'): pass\n", 1, "target"
    )
    assert defaults == {"a": "1", "optional": "'yes'"}


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        ("list[", ()),
        ('"list["', ()),
        ("pkg.Widget | Local", (("attribute", "pkg", "Widget"), ("name", "Local"))),
        ("pkg.sub.Widget", (("attribute", "pkg", "sub"),)),
    ],
)
def test_collect_annotation_refs_handles_malformed_and_detached_expressions(
    annotation: str, expected: tuple[tuple[str, ...], ...]
) -> None:
    assert analysis._collect_annotation_type_refs(annotation) == expected


def test_folding_ranges_walk_exception_handlers_and_skip_single_line_definitions() -> None:
    source = (
        "def one(): pass\n"
        "try:\n"
        "    value = 1\n"
        "except Exception:\n"
        "    def nested():\n"
        "        return 2\n"
    )
    ranges = analysis._compute_folding_ranges(source)
    assert len(ranges) == 1
    assert ranges[0].range.start.line == 4


def test_callable_and_scope_helpers_reject_invalid_or_nested_shapes() -> None:
    tree = analysis._parse_python(
        "class Outer:\n"
        "    class Inner:\n"
        "        def method(self): pass\n"
        "def top():\n"
        "    def nested(): pass\n"
    )
    assert analysis._find_callable_node(tree, "") is None
    assert analysis._find_callable_node(tree, "Outer.Inner.method") is not None
    assert analysis._find_callable_node(tree, "top.nested") is None

    function = analysis._parse_python("def f(a, /, *args, **kwargs): pass\n").body[0]
    assert isinstance(function, ast.FunctionDef)
    assert analysis._first_positional_param(function) == "a"
    own_nodes = tuple(analysis._iter_own_scope(function))
    assert not any(isinstance(node, ast.FunctionDef) for node in own_nodes)


def test_annotation_lookup_includes_varargs_kwargs_and_skips_nested_scopes() -> None:
    tree = analysis._parse_python(
        "def outer(*items: Item, **options: Option):\n"
        "    value: First = first\n"
        "    def nested():\n"
        "        value: Nested = nested\n"
        "    value: Second = second\n"
        "    return value\n"
    )
    items = analysis._annotation_expr_for_name_at(tree, 6, "items")
    options = analysis._annotation_expr_for_name_at(tree, 6, "options")
    value = analysis._annotation_expr_for_name_at(tree, 6, "value")
    assert items is not None and ast.unparse(items) == "Item"
    assert options is not None and ast.unparse(options) == "Option"
    assert value is not None and ast.unparse(value) == "Second"


def test_call_and_expression_geometry_defensive_paths() -> None:
    name_call = cast(ast.Call, analysis._parse_python("call()", mode="eval").body)
    name = cast(ast.Name, name_call.func)
    name.end_col_offset = None
    name.end_lineno = None
    assert analysis._call_func_range(name_call) == (0, 0, 0, 4)

    attribute_call = cast(ast.Call, analysis._parse_python("owner.call()", mode="eval").body)
    attribute = cast(ast.Attribute, attribute_call.func)
    attribute.end_col_offset = None
    assert analysis._call_func_range(attribute_call) is None
    attribute.end_col_offset = 2
    attribute.end_lineno = 1
    assert analysis._call_func_range(attribute_call) is None

    unsupported = cast(ast.Call, analysis._parse_python("factory[T]()", mode="eval").body)
    assert analysis._call_func_range(unsupported) is None
    assert analysis._expression_name_position(ast.Constant(value=1)) is None
    attribute.end_col_offset = None
    assert analysis._expression_name_position(attribute) is None


def test_annotation_type_positions_handle_invalid_missing_and_string_annotations() -> None:
    assert analysis._annotation_type_positions("def broken(", "broken", "function", _range()) == ()
    assert analysis._annotation_type_positions("value = 1\n", "value", "variable", _range()) == ()
    assert (
        analysis._annotation_type_positions(
            "value: 'Forward'\n",
            "value",
            "variable",
            SourceRange(SourcePosition(0, 0), SourcePosition(0, 5)),
        )
        == ()
    )


def test_base_and_inlay_helpers_reject_unsupported_or_exhausted_inputs() -> None:
    call_base = analysis._parse_python("factory()", mode="eval").body
    assert analysis._unwrap_base_expression(call_base) is None

    call = cast(ast.Call, analysis._parse_python("run(first, second)", mode="eval").body)
    hints = analysis._inlay_hints_for_call(
        call, (Parameter("**options", None), Parameter("first", None))
    )
    assert hints == []
    assert analysis._inlay_hints_for_call(call, ()) == []

    call.args[0].lineno = None  # type: ignore[assignment]
    assert analysis._inlay_hints_for_call(call, (Parameter("value", None),)) == []


def test_definition_name_and_token_helpers_handle_missing_source_geometry() -> None:
    function = analysis._parse_python("def target(): pass\n").body[0]
    assert isinstance(function, ast.FunctionDef)
    function.lineno = 5
    assert analysis._locate_def_name_offsets_on_header(["def target(): pass"], function) is None
    function.lineno = 1
    function.name = "missing"
    assert analysis._locate_def_name_offsets_on_header(["def target(): pass"], function) is None
    assert analysis._normalized_name_offsets_on_line("'unterminated", "name") is None

    arguments = analysis._parse_python("def f(a, /, b, *args, c, **kwargs): pass\n").body[0]
    assert isinstance(arguments, ast.FunctionDef)
    assert [item.arg for item in analysis._iter_function_args(arguments.args)] == [
        "a",
        "b",
        "args",
        "c",
        "kwargs",
    ]
