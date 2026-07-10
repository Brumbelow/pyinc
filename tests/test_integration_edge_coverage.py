from __future__ import annotations

import ast
import csv
import datetime
import sys
from pathlib import Path
from typing import Any

import pytest

from pyinc import Database
from pyinc.integrations import (
    _pep440,
    csv_data,
    deep_module_resolution,
    dependency_check,
    env_file,
    installed_packages,
    json_config,
    notebook,
    python_source,
    requirement_evaluation,
    source_geometry,
    symbol_resolution,
    toml_config,
    xml_config,
)
from pyinc.integrations.requirements_txt import RequirementPayload
from pyinc.integrations.source_geometry import DocumentMap, SourcePosition, SourceRange


def test_pep440_normalization_and_uncommon_release_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _pep440._canonical_pre("unknown") is None
    assert _pep440.parse_version("v1.2-3") == _pep440.Version(0, (1, 2), None, 3, None, ())
    assert _pep440.parse_version("1.2alpha") == _pep440.Version(0, (1, 2), ("a", 0), None, None, ())
    assert _pep440.parse_version("1.2rev") == _pep440.Version(0, (1, 2), None, 0, None, ())
    assert _pep440.parse_version("1.2dev") == _pep440.Version(0, (1, 2), None, None, 0, ())
    assert _pep440.parse_version("1.2+linux.7") == _pep440.Version(
        0, (1, 2), None, None, None, ("linux", 7)
    )

    monkeypatch.setattr(_pep440, "_canonical_pre", lambda _letter: None)
    assert _pep440.parse_version("1.0rc1") is None


def test_pep440_specifier_parser_and_unsupported_edges() -> None:
    assert _pep440.parse_specifier_set("") == ()
    assert _pep440.parse_specifier_set(", >=1, , <2, ") == ((">=", "1"), ("<", "2"))
    assert _pep440.parse_specifier_set("banana") is None

    version = _pep440.parse_version("1.2.3+local")
    assert version is not None
    assert _pep440._satisfies_single("==", "bad.*", version) is None
    assert _pep440._satisfies_single(">", "1.2.*", version) is None
    assert _pep440._satisfies_single("==", "bad", version) is None
    assert _pep440._satisfies_single("~=", "1", version) is None
    assert _pep440._satisfies_single("!=", "1.2.3+other", version) is True
    assert _pep440._satisfies_single("?", "1.2.3", version) is None
    assert _pep440._without_local(_pep440.Version(0, (1,), None, None, None, ())) == (
        _pep440.Version(0, (1,), None, None, None, ())
    )
    assert _pep440.satisfies((), "1.0", include_prerelease=False) == (
        True,
        "1.0 satisfies (no constraint)",
    )


def test_pep440_ordering_exclusion_edges() -> None:
    post = _pep440.parse_version("1.0.post1")
    prerelease = _pep440.parse_version("1.0rc1")
    assert post is not None and prerelease is not None
    assert _pep440._satisfies_single(">", "1.0", post) is False
    assert _pep440._satisfies_single("<", "1.0", prerelease) is False


def test_source_geometry_rejects_invalid_coordinates_and_boundaries() -> None:
    with pytest.raises(TypeError):
        DocumentMap("x").line(True)
    with pytest.raises(TypeError):
        DocumentMap("x").from_ast(1, True)
    with pytest.raises(ValueError):
        DocumentMap("x").from_ast(0, 0)
    with pytest.raises(ValueError):
        DocumentMap("x").from_ast(1, -1)
    with pytest.raises(ValueError):
        DocumentMap("x").from_ast(1, 2)
    with pytest.raises(ValueError, match="UTF-8 boundary"):
        DocumentMap("é").from_ast(1, 1)
    with pytest.raises(ValueError, match="complete source range"):
        DocumentMap("x").ast_range(ast.Load())

    document = DocumentMap("😀")
    with pytest.raises(ValueError, match="beyond"):
        document.to_codepoint(SourcePosition(0, 2), "utf-32")
    with pytest.raises(ValueError, match="splits"):
        document.to_codepoint(SourcePosition(0, 1), "utf-16")
    with pytest.raises(ValueError, match="beyond"):
        document.to_codepoint(SourcePosition(0, 5), "utf-8")
    with pytest.raises(ValueError, match="beyond"):
        document.from_codepoint(SourcePosition(0, 2), "utf-16")


def test_source_geometry_range_helpers_and_identifier_fallbacks() -> None:
    document = DocumentMap("a😀b")
    codepoints = SourceRange(SourcePosition(0, 1), SourcePosition(0, 2))
    encoded = document.range_from_codepoint(codepoints, "utf-16")
    assert encoded == SourceRange(SourcePosition(0, 1), SourcePosition(0, 3))
    assert document.range_to_codepoint(encoded, "utf-16") == codepoints
    assert source_geometry._encoded_width("x", "utf-32") == 1
    with pytest.raises(ValueError, match="unsupported"):
        source_geometry._encoded_width("x", "invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported"):
        source_geometry._validate_encoding("invalid")

    expression = ast.parse("x + x", mode="eval").body
    assert source_geometry.ast_range("x + x", expression) == SourceRange(
        SourcePosition(0, 0), SourcePosition(0, 5)
    )
    assert source_geometry.identifier_range("x + x", expression, "x", reverse=True) == (
        SourceRange(SourcePosition(0, 4), SourcePosition(0, 5))
    )

    malformed = ast.Name(id="missing")
    malformed.lineno = 1
    malformed.col_offset = 0
    malformed.end_lineno = 1
    malformed.end_col_offset = 3
    assert source_geometry.identifier_range('"""', malformed, "missing") == SourceRange(
        SourcePosition(0, 0), SourcePosition(0, 0)
    )


def test_marker_tokenizer_and_parser_rejection_edges() -> None:
    tokenize = requirement_evaluation._tokenize_marker
    assert tokenize("('x' === 'x')") is not None
    assert tokenize("'unterminated") is None
    assert tokenize("name = 'x'") is None
    assert tokenize("name ! 'x'") is None
    assert tokenize("name not value") is None
    assert tokenize("@") is None

    parse = requirement_evaluation._parse_marker
    for marker in (
        "or x == 'x'",
        "x == 'x' or",
        "x == 'x' and",
        "(x == 'x'",
        "()",
        "x",
        "x ==",
        "== 'x'",
        "x == 'x' 'tail'",
    ):
        assert parse(marker) is None


def test_marker_comparison_edges() -> None:
    env = _fixed_environment()
    evaluate = requirement_evaluation._eval_compare

    diagnostics: list[tuple[str, str]] = []
    unknown = requirement_evaluation._CompareNode("name", "unknown", "==", "string", "")
    assert evaluate(unknown, env, diagnostics) is True
    assert diagnostics[0][0] == "unknown-marker-variable"
    assert requirement_evaluation._env_lookup("unknown", env) == ""

    cases = (
        ("<", "a", "b", True),
        ("<=", "a", "a", True),
        (">", "b", "a", True),
        (">=", "b", "b", True),
        ("==", "a", "a", True),
        ("!=", "a", "b", True),
        ("===", "a", "a", True),
        ("in", "a", "cat", True),
        ("not in", "z", "cat", True),
        ("?", "a", "a", False),
    )
    for operator, left, right, expected in cases:
        node = requirement_evaluation._CompareNode("string", left, operator, "string", right)
        assert evaluate(node, env, []) is expected

    for operator, expected in (
        ("<", True),
        ("<=", True),
        (">", False),
        (">=", False),
        ("==", False),
        ("!=", True),
    ):
        node = requirement_evaluation._CompareNode(
            "name", "python_version", operator, "string", "4.0"
        )
        assert evaluate(node, env, []) is expected

    invalid_compatible = requirement_evaluation._CompareNode("string", "bad", "~=", "string", "1.0")
    invalid_diagnostics: list[tuple[str, str]] = []
    assert evaluate(invalid_compatible, env, invalid_diagnostics) is False
    assert invalid_diagnostics[0][0] == "unparseable-version"

    compatible = requirement_evaluation._CompareNode(
        "name", "python_version", "~=", "string", "3.1"
    )
    assert evaluate(compatible, env, []) is True


def test_marker_boolean_nodes_and_environment_resource() -> None:
    env = requirement_evaluation._current_python_env()
    assert len(env) == 11
    resource = requirement_evaluation._PythonEnvironmentResource()
    assert resource.probe("python") == env
    assert resource.load(Database(), "python") == env

    yes = requirement_evaluation._CompareNode("string", "x", "==", "string", "x")
    no = requirement_evaluation._CompareNode("string", "x", "!=", "string", "x")
    assert (
        requirement_evaluation._eval_node(requirement_evaluation._OrNode((no, yes)), env, [])
        is True
    )
    assert (
        requirement_evaluation._eval_node(requirement_evaluation._AndNode((yes, no)), env, [])
        is False
    )


def test_requirement_status_edges() -> None:
    env = _fixed_environment()

    def requirement(specifier: str, marker: str = "") -> RequirementPayload:
        return ("Demo_Name", "", 1, (), specifier, marker, False)

    malformed, diagnostics = requirement_evaluation._evaluate_requirement(
        requirement("", "broken marker"), {}, env
    )
    assert malformed[5] == "not_applicable"
    assert diagnostics[0][0] == "marker-parse-error"

    false_marker, _ = requirement_evaluation._evaluate_requirement(
        requirement("", 'sys_platform == "win32"'), {}, env
    )
    assert false_marker[5] == "not_applicable"

    url_missing, diagnostics = requirement_evaluation._evaluate_requirement(
        requirement("@ https://example.invalid/demo.whl"), {}, env
    )
    assert url_missing[5] == "missing"
    assert diagnostics[0][0] == "url-requirement-deferred"

    url_present, _ = requirement_evaluation._evaluate_requirement(
        requirement("@ https://example.invalid/demo.whl"), {"demo-name": "1.0"}, env
    )
    assert url_present[5] == "satisfied"

    unconstrained, _ = requirement_evaluation._evaluate_requirement(
        requirement(""), {"demo-name": "1.0"}, env
    )
    assert unconstrained[5] == "satisfied"
    invalid, _ = requirement_evaluation._evaluate_requirement(
        requirement("not-a-spec"), {"demo-name": "1.0"}, env
    )
    assert invalid[5] == "ambiguous"
    arbitrary, _ = requirement_evaluation._evaluate_requirement(
        requirement("===1.0"), {"demo-name": "1.0"}, env
    )
    assert arbitrary[5] == "ambiguous"
    mismatch, _ = requirement_evaluation._evaluate_requirement(
        requirement(">=2"), {"demo-name": "1.0"}, env
    )
    assert mismatch[5] == "version_mismatch"


def _fixed_environment() -> requirement_evaluation.PythonEnvironmentPayload:
    return (
        "3.12",
        "3.12.3",
        "cpython",
        "3.12.3",
        "posix",
        "linux",
        "Linux",
        "6.0",
        "x86_64",
        "CPython",
        "#1",
    )


def test_python_source_resource_and_decode_edges(tmp_path: Path) -> None:
    resource = python_source._SourceTextResource()
    missing = tmp_path / "missing.py"
    assert resource.probe(str(missing)) == ("missing",)
    assert resource.load(Database(), str(missing)) == ("", None)

    source = tmp_path / "latin.py"
    source.write_bytes(b"# coding: latin-1\nname = '\xe9'\n")
    probe = resource.probe(str(source))
    assert probe[0] == "present"
    assert resource.load(Database(), str(source))[0].endswith("name = 'é'\n")

    decoded, error = python_source._decode_python_source(b"# coding: missing-codec\n", str(source))
    assert decoded == "" and error is not None
    decoded, error = python_source._decode_python_source(b"# coding: ascii\n# \xff\n", str(source))
    assert decoded == "" and error is not None


def test_python_source_binding_helpers_cover_dynamic_shapes() -> None:
    starred = ast.parse("[*items]").body[0]
    assert isinstance(starred, ast.Expr) and isinstance(starred.value, ast.List)
    assert python_source._target_bound_names(starred.value) == ("items",)
    assert (
        python_source._target_bound_names(ast.Attribute(value=ast.Name(id="obj"), attr="x")) == ()
    )
    target = ast.parse("a, *rest = values").body[0]
    assert isinstance(target, ast.Assign)
    assert python_source._target_bound_names(target.targets[0]) == ("a", "rest")
    assert python_source._literal_string_collection(None) is None
    assert python_source._literal_string_collection(ast.Constant(value="x")) is None
    assert python_source._literal_string_collection(ast.parse("['x', 1]", mode="eval").body) is None

    tree = ast.parse(
        """
__all__ = dynamic
__all__: list[str] = ['public']
__all__ += more
del __all__
print(__all__)
try:
    import optional
    from package import item
except ImportError:
    pass
from wildcard import *
while condition:
    pass
"""
    )
    explicit, wildcard, reasons = python_source._module_binding_analysis(tree)
    assert {"optional", "item"} <= set(explicit)
    assert wildcard == ("item", "optional")
    assert "dynamic __all__" in reasons
    assert "top-level wildcard re-export" in reasons
    assert "unsupported top-level While" in reasons


def test_python_source_resolution_helper_edges(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a Python source"):
        python_source._module_name_for_path(str(tmp_path), str(tmp_path / "data.txt"))
    assert python_source._top_level_module_name("") is None
    assert (
        python_source._resolve_relative_base("pkg.mod", str(tmp_path / "mod.py"), "....x") is None
    )

    groups: dict[str, tuple[str, ...]] = {
        "pkg": (str(tmp_path / "pkg.py"),),
        "pkg.child": (str(tmp_path / "child.py"),),
    }
    assert python_source._resolve_workspace_module("pkg.child", groups)[2] == "ambiguous"
    ambiguous: dict[str, tuple[str, ...]] = {"pkg": ("a.py", "b.py")}
    assert python_source._resolve_workspace_module("pkg.child", ambiguous)[2] == "ambiguous"
    assert python_source._resolve_workspace_module("missing", groups) == (None, None, None)

    assert python_source._missing_resolution(
        "", {}, prefer_external=True, stdlib_modules=frozenset(), package_top_levels={}
    ) == ("missing", None, None)
    assert python_source._missing_resolution(
        "pkg.child", groups, prefer_external=True, stdlib_modules=frozenset(), package_top_levels={}
    ) == ("missing", None, None)
    assert python_source._missing_resolution(
        "json", {}, prefer_external=True, stdlib_modules=frozenset({"json"}), package_top_levels={}
    ) == ("stdlib", None, None)
    assert python_source._missing_resolution(
        "demo.mod",
        {},
        prefer_external=True,
        stdlib_modules=frozenset(),
        package_top_levels={"demo": ("Demo", "1.0")},
    ) == ("installed", "Demo", "1.0")

    assert (
        python_source._installed_module_candidates(
            request_module="",
            kind="import",
            imported_name=None,
            current_module="pkg.mod",
            current_path=str(tmp_path / "mod.py"),
        )
        == ()
    )
    assert (
        python_source._installed_module_candidates(
            request_module="....base",
            kind="from",
            imported_name="item",
            current_module="pkg.mod",
            current_path=str(tmp_path / "mod.py"),
        )
        == ()
    )
    assert (
        python_source._installed_module_candidates(
            request_module=".",
            kind="from",
            imported_name="item",
            current_module="pkg.__init__",
            current_path=str(tmp_path / "pkg" / "__init__.py"),
        )[-1]
        == "pkg.__init__"
    )


def test_python_source_import_guard_helpers() -> None:
    assert python_source._is_type_checking_test(ast.Name(id="TYPE_CHECKING"))
    assert python_source._is_type_checking_test(
        ast.Attribute(value=ast.Name(id="typing"), attr="TYPE_CHECKING")
    )
    assert not python_source._is_type_checking_test(ast.Name(id="other"))

    handlers = ast.parse("try:\n x\nexcept:\n pass\n").body[0]
    assert isinstance(handlers, ast.Try)
    assert not python_source._has_import_error_handler(handlers.handlers)
    handlers = ast.parse("try:\n x\nexcept (ValueError, ImportError):\n pass\n").body[0]
    assert isinstance(handlers, ast.Try)
    assert python_source._has_import_error_handler(handlers.handlers)


def test_workspace_python_files_non_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "plain.py"
    file_path.write_text("value = 1\n", encoding="utf-8")
    assert python_source.workspace_python_files(Database(), str(file_path)) == ()


@pytest.mark.parametrize(
    "resource",
    [
        csv_data._CsvFileResource(),
        deep_module_resolution._PthFileResource(),
        env_file._EnvFileResource(),
        installed_packages._DistInfoMetadataResource(),
        json_config._JsonFileResource(),
        notebook._NotebookFileResource(),
        toml_config._ConfigFileResource(),
        xml_config._XmlFileResource(),
    ],
)
def test_integration_text_resources_probe_and_load_both_states(
    resource: Any, tmp_path: Path
) -> None:
    path = tmp_path / "payload.txt"
    probe = resource.probe
    load = resource.load
    assert probe(str(path)) == ("missing",)
    assert load(Database(), str(path)) == ""

    path.write_text("payload\n", encoding="utf-8")
    present = probe(str(path))
    assert present[0] == "present"
    assert load(Database(), str(path)) == "payload\n"


def test_site_packages_resource_probe_and_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = (str(tmp_path),)
    monkeypatch.setattr(installed_packages, "_get_site_packages_dirs", lambda: expected)
    resource = installed_packages._SitePackagesResource()
    assert resource.probe("python") == expected
    assert resource.load(Database(), "python") == expected


def test_dependency_specifier_extraction_edge_cases() -> None:
    assert dependency_check._check_version_constraints("not a spec", "1.0")[0] == "ambiguous"
    assert dependency_check._check_version_constraints("===1", "1.0")[0] == "ambiguous"
    assert dependency_check._check_version_constraints(">=1", "bad")[0] == "ambiguous"
    assert dependency_check._extract_dep_name_and_spec("Demo @ https://example.invalid/x") == (
        "demo",
        "",
    )
    assert dependency_check._extract_dep_name_and_spec("!bad") == ("!bad", "")
    assert dependency_check._extract_dep_name_and_spec("demo[broken>=1") == (
        "demo",
        "[broken>=1",
    )


def test_env_parser_unclosed_quotes_and_interpolation() -> None:
    entries, diagnostics = env_file._parse_env_lines(
        "DOUBLE=\"unterminated\nSINGLE='unterminated\nPLAIN=value # comment\nREF=${HOME}\n"
    )
    by_name = {entry[0]: entry for entry in entries}
    assert by_name["DOUBLE"][1] == '"unterminated'
    assert by_name["SINGLE"][1] == "'unterminated"
    assert by_name["PLAIN"][1] == "value"
    assert diagnostics[0][0] == "interpolation-reference"


def test_csv_empty_reader_and_sniffer_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csv_data, "_detect_delimiter", lambda _text: ",")
    monkeypatch.setattr(csv_data, "_detect_has_header", lambda _text: False)
    monkeypatch.setattr(csv, "reader", lambda *_args, **_kwargs: iter(()))
    assert csv_data._parse_csv("not empty") == ([], 0, ",", False, [])


def test_json_helper_uncommon_values_and_failures() -> None:
    assert json_config._json_value_type(object()) == "unknown"
    assert json_config._json_value_to_string({"b": 1, "a": 2}) == "[('a', 2), ('b', 1)]"
    assert json_config._json_cutoff_token("{broken")[0] == "raw"
    assert json_config._try_parse_json("[]") is None
    assert json_config._try_parse_json("{broken") is None


def test_toml_helper_uncommon_values_and_failures() -> None:
    assert toml_config._toml_value_type(1.5) == "float"
    assert toml_config._toml_value_type({}) == "table"
    assert toml_config._toml_value_type(object()) == "unknown"
    assert toml_config._toml_value_to_string({"b": 1, "a": 2}) == "[('a', 2), ('b', 1)]"
    assert toml_config._config_cutoff_token("[broken")[0] == "raw"
    assert toml_config._toml_cutoff_value(datetime.date(2026, 7, 9)) == (
        "date",
        "2026-07-09",
    )
    assert toml_config._toml_cutoff_value(datetime.time(1, 2, 3)) == (
        "time",
        "01:02:03",
    )


def test_notebook_cutoff_and_cell_helper_edges() -> None:
    assert notebook._coerce_source(42) == ""
    assert notebook._notebook_cutoff_token("not json")[0] == "raw"
    assert notebook._notebook_cutoff_token('{"cells": {}}')[0] == "raw"
    cutoff = notebook._notebook_cutoff_token(
        '{"cells":[null],"metadata":{"kernelspec":null,"language_info":{"name":"python"}}}'
    )
    assert cutoff == ("nb", "None", "'python'", "invalid-cell")


def test_deep_module_sys_path_filtering(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    valid = tmp_path / "valid"
    valid.mkdir()
    duplicate = valid / "."
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        sys,
        "path",
        [None, "", str(valid), str(duplicate), str(missing)],
    )
    assert deep_module_resolution._get_sys_path_entries() == (str(valid.resolve()),)


def test_symbol_table_low_level_binding_edges(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a Python source"):
        symbol_resolution._module_name_for_path(str(tmp_path), str(tmp_path / "data.txt"))

    target = ast.parse("a, *rest = values").body[0]
    assert isinstance(target, ast.Assign)
    assert symbol_resolution._target_bound_names(target.targets[0]) == ("a", "rest")
    assert (
        symbol_resolution._target_bound_names(ast.Attribute(value=ast.Name(id="obj"), attr="field"))
        == ()
    )

    function = ast.parse(
        "def f(a: int, /, b, *args: str, c: bytes, **kwargs: object) -> bool: pass"
    ).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert symbol_resolution._parameter_payloads_from_args(function.args) == (
        ("a", "int"),
        ("b", None),
        ("*args", "str"),
        ("c", "bytes"),
        ("**kwargs", "object"),
    )

    cls = ast.parse(
        """
class C:
    obj.field: int
    __all__ = []
"""
    ).body[0]
    assert isinstance(cls, ast.ClassDef)
    symbols: list[symbol_resolution.SymbolPayload] = []
    symbol_resolution._class_body_walk(cls, "C", symbols)
    assert symbols == []


def test_symbol_table_type_checking_wildcard_and_type_alias() -> None:
    block = ast.parse("from provider import *").body
    symbols: list[symbol_resolution.SymbolPayload] = []
    symbol_resolution._type_checking_block_walk(block, symbols)
    assert symbols[0][1] == "wildcard_import_stub"

    if hasattr(ast, "TypeAlias"):
        tree = ast.parse("type Alias = int")
        payload, _reasons = symbol_resolution._module_symbol_walk(tree)
        assert payload[0][0] == "Alias"

        cls = ast.parse("class C:\n    type Alias = int\n").body[0]
        assert isinstance(cls, ast.ClassDef)
        nested: list[symbol_resolution.SymbolPayload] = []
        symbol_resolution._class_body_walk(cls, "C", nested)
        assert nested[0][0] == "C.Alias"


def test_symbol_table_ignores_invalid_module_targets_and_deduplicates_impurity() -> None:
    tree = ast.parse(
        """
obj.field: int
__all__: list[str]
while first:
    pass
while second:
    pass
"""
    )
    symbols, reasons = symbol_resolution._module_symbol_walk(tree)
    assert symbols == ()
    assert reasons == ("conditional top-level binding",)

    type_alias_cls: Any = getattr(ast, "TypeAlias", None)
    if type_alias_cls is not None:
        invalid_alias = type_alias_cls(
            name=ast.Attribute(value=ast.Name(id="owner"), attr="Alias"),
            type_params=[],
            value=ast.Name(id="int"),
        )
        invalid_tree = ast.Module(body=[invalid_alias], type_ignores=[])
        assert symbol_resolution._module_symbol_walk(invalid_tree) == ((), ())


def test_symbol_resolution_small_helpers() -> None:
    assert symbol_resolution._is_module_target("/root", "/root/pkg.py", "pkg")
    assert symbol_resolution._is_module_target("/root", "/root/a/pkg.py", "pkg")

    posonly = ast.parse("def f(self, /): pass").body[0]
    keyword_only = ast.parse("def f(*, value): pass").body[0]
    assert isinstance(posonly, ast.FunctionDef) and isinstance(keyword_only, ast.FunctionDef)
    assert symbol_resolution._first_param_name(posonly) == "self"
    assert symbol_resolution._first_param_name(keyword_only) is None

    assert symbol_resolution._encode_base(ast.parse("Base[T]", mode="eval").body) == (
        "name",
        "Base",
    )
    text_base = symbol_resolution._encode_base(ast.parse("factory()", mode="eval").body)
    assert text_base == ("text", "factory()")
    assert symbol_resolution._base_text(("attr", "pkg", "Base")) == "pkg.Base"

    unpack = ast.parse("self.a, *self.rest = values").body[0]
    assert isinstance(unpack, ast.Assign)
    assert symbol_resolution._self_attribute_names(unpack.targets[0]) == ("a", "rest")
    assert (
        symbol_resolution._self_attribute_names(ast.Attribute(value=ast.Name(id="other"), attr="x"))
        == ()
    )


def test_match_import_skips_nonmatching_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    entries: Any = (
        ("other", "import", 1, None, None, None, "missing", None, None),
        ("pkg", "import", 2, None, None, "/target", "workspace", None, None),
        ("pkg", "from", 3, "other", None, "/target", "workspace", None, None),
    )
    monkeypatch.setattr(
        symbol_resolution,
        "resolved_imports_for_file",
        lambda _db, _root, _path: entries,
    )
    assert (
        symbol_resolution._match_import(Database(), "/root", "/root/mod.py", "pkg", "name") is None
    )

    matching: Any = (("pkg", "from", 1, "name", None, "/target", "workspace", None, None),)
    monkeypatch.setattr(
        symbol_resolution,
        "resolved_imports_for_file",
        lambda _db, _root, _path: matching,
    )
    assert symbol_resolution._match_import(Database(), "/root", "/root/mod.py", "pkg", "name") == (
        "workspace",
        "/target",
        None,
        None,
    )


def test_symbol_class_member_edge_shapes() -> None:
    cls = ast.parse(
        """
class C:
    __all__ = []
    duplicate: int
    duplicate = 1
    def method(*, value):
        pass
    def first(self):
        self.value: int = 1
        self.left, *self.rest = values
        def nested():
            self.hidden = 1
"""
    ).body[0]
    assert isinstance(cls, ast.ClassDef)
    members = symbol_resolution._class_member_walk(cls)
    by_name = {member[0]: member for member in members}
    assert "__all__" not in by_name
    assert by_name["duplicate"][1] == "class_variable"
    assert by_name["value"][1:] == ("instance_variable", 9, "int", None)
    assert by_name["left"][1] == "instance_variable"
    assert by_name["rest"][1] == "instance_variable"
    assert "hidden" not in by_name


def test_symbol_reference_and_class_site_validation(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="SymbolId"):
        symbol_resolution.find_references(
            Database(),
            tmp_path,
            "not-a-symbol",  # type: ignore[arg-type]
        )

    non_workspace: symbol_resolution._ResolvedSymbolPayload = (
        "mod",
        "name",
        "missing",
        None,
        None,
        None,
        None,
        None,
        0,
        (),
    )
    assert (
        symbol_resolution._class_site_from_resolved(Database(), str(tmp_path), non_workspace)
        is None
    )
    missing_site: symbol_resolution._ResolvedSymbolPayload = (
        "mod",
        "name",
        "workspace",
        "mod",
        None,
        None,
        None,
        None,
        0,
        (),
    )
    assert (
        symbol_resolution._class_site_from_resolved(Database(), str(tmp_path), missing_site) is None
    )


def test_symbol_class_site_rejects_nonclass_and_deduplicates_missing_bases(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "module.py"
    path.write_text("value = 1\nclass C(Missing, Missing):\n    pass\n", encoding="utf-8")
    db = Database()
    resolved = symbol_resolution._resolve_symbol_payload(db, str(root), str(path), "value")
    assert symbol_resolution._class_site_from_resolved(db, str(root), resolved) is None
    model = symbol_resolution.class_model(db, root, path, "C")
    assert model.unresolved_bases == ("Missing",)
