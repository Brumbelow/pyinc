from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pyinc import Database
from pyinc_codegen.models import DiagnosticPayload, ModelPayload
from pyinc_codegen.schema import (
    _annotation_diagnostics,
    _build_enum,
    _build_model,
    _canonical_json_token,
    _decode_pointer_segment,
    _definition_entries,
    _DuplicateKeyError,
    _duplicates,
    _effective_type,
    _enum_type_matches,
    _field_collision_diagnostics,
    _InvalidJsonError,
    _load,
    _local_ref_target,
    _module_name_diagnostics,
    _object_from_pairs,
    _parse_float,
    _percent_decode,
    _render_doc,
    _render_python,
    _render_type,
    _schema_node_diagnostics,
    _snake,
    _type_checking_imports,
    definition_model,
    definition_names,
    definition_pointer,
    definition_raw,
    document_diagnostics,
    index_init,
)


def _codes(diagnostics: tuple[DiagnosticPayload, ...]) -> set[str]:
    return {diagnostic[0] for diagnostic in diagnostics}


def _write_schema(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_json_object_hook_and_loader_reject_duplicate_keys() -> None:
    with pytest.raises(_DuplicateKeyError, match="duplicate JSON object key 'a'"):
        _object_from_pairs([("a", 1), ("a", 2)])
    with pytest.raises(_InvalidJsonError, match="duplicate JSON object key 'a'"):
        _load('{"a": 1, "a": 2}')


def test_loader_preserves_an_already_normalized_json_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _InvalidJsonError("already normalized")

    def fail(*args: object, **kwargs: object) -> object:
        raise expected

    monkeypatch.setattr("pyinc_codegen.schema.json.loads", fail)
    with pytest.raises(_InvalidJsonError) as caught:
        _load("ignored")
    assert caught.value is expected


def test_float_parser_and_canonical_token_cover_nonfinite_and_raw_input() -> None:
    assert _parse_float("1.25") == 1.25
    with pytest.raises(ValueError, match="non-finite"):
        _parse_float("1e999")
    assert _canonical_json_token("not json") == ("raw", "not json")


def test_definition_entries_handle_nonobjects_and_prefer_defs() -> None:
    assert _definition_entries(1) == ()
    entries = _definition_entries(
        {
            "$defs": {"Same": {"type": "string"}},
            "definitions": {
                "Same": {"type": "integer"},
                "Other": {"type": "boolean"},
            },
        }
    )
    assert [(name, pointer) for name, _fragment, pointer in entries] == [
        ("Same", "/$defs/Same"),
        ("Other", "/definitions/Other"),
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", "model"),
        ("HTTPServer", "http_server"),
        ("Version2Model", "version2_model"),
        ("XML2Parser", "xml2_parser"),
    ],
)
def test_snake_handles_empty_acronym_and_digit_boundaries(name: str, expected: str) -> None:
    assert _snake(name) == expected


def test_module_name_diagnostics_cover_every_portability_failure() -> None:
    assert _codes(_module_name_diagnostics("Class", "/Class")) == {"invalid-module-name"}
    assert _codes(_module_name_diagnostics("CON", "/CON")) == {"nonportable-module-name"}
    assert _codes(_module_name_diagnostics("__INIT__", "/__INIT__")) == {"reserved-module-name"}
    assert _codes(_module_name_diagnostics("A" * 300, "/long")) == {"nonportable-module-name"}
    assert _module_name_diagnostics("SafeName", "/SafeName") == ()


def test_pointer_decoders_cover_valid_escapes_and_malformed_sequences() -> None:
    assert _decode_pointer_segment("a~0b~1c") == "a~b/c"
    assert _decode_pointer_segment("trailing~") is None
    assert _decode_pointer_segment("bad~2escape") is None

    assert _percent_decode("/$defs/Caf%C3%A9") == "/$defs/Café"
    assert _percent_decode("%") is None
    assert _percent_decode("%GG") is None
    assert _percent_decode("%FF") is None


@pytest.mark.parametrize(
    ("ref", "target", "problem"),
    [
        ("#/$defs/A", "A", None),
        ("#/definitions/a~1b", "a/b", None),
        ("https://example.test/schema", None, "only local"),
        ("#/%FF", None, "invalid percent-encoded UTF-8"),
        ("#/$defs/bad~2name", None, "invalid '~' escape"),
        ("#/$defs/A/child", None, "only direct references"),
        ("#/properties/A", None, "only direct references"),
    ],
)
def test_local_ref_target_reports_each_invalid_reference_shape(
    ref: str,
    target: str | None,
    problem: str | None,
) -> None:
    actual_target, actual_problem = _local_ref_target(ref)
    assert actual_target == target
    if problem is None:
        assert actual_problem is None
    else:
        assert problem in str(actual_problem)


def test_annotation_and_schema_node_diagnostics_cover_context_branches() -> None:
    annotations = _annotation_diagnostics(
        {"description": 1, "title": [], "$comment": "ok"},
        "/node",
        frozenset({"description", "title", "$comment"}),
    )
    assert _codes(annotations) == {"invalid-description", "invalid-annotation"}

    assert _effective_type("string") == "string"
    assert _effective_type(["string", "null"]) == "string"
    assert _effective_type(["string"]) == ["string"]
    assert _effective_type(["string", 1]) == ["string", 1]

    enum_codes = _codes(
        _schema_node_diagnostics({"enum": ["x"]}, "/enum", definition_context=False)
    )
    assert enum_codes == {"unsupported-construct"}

    conflict_codes = _codes(
        _schema_node_diagnostics(
            {"type": "string", "properties": {}},
            "/model",
            definition_context=True,
        )
    )
    assert "ambiguous-schema-combination" in conflict_codes

    object_codes = _codes(
        _schema_node_diagnostics(
            {"type": "object", "properties": {}, "required": []},
            "/model",
            definition_context=True,
        )
    )
    assert object_codes == set()


@pytest.mark.parametrize(
    ("spec", "definition_exists", "expected_type", "expected_codes"),
    [
        (True, lambda name: False, "object", {"unsupported-boolean-schema"}),
        (False, lambda name: False, "Never", {"unsupported-boolean-schema"}),
        (1, lambda name: False, "object", {"invalid-schema-node"}),
        ({"$ref": 1}, lambda name: False, "object", {"invalid-ref"}),
        (
            {"$ref": "https://example.test/Model"},
            lambda name: False,
            "object",
            {"unsupported-ref"},
        ),
        ({"$ref": "#/$defs/Missing"}, lambda name: False, "object", {"unknown-ref"}),
        (
            {"$ref": "#/$defs/not-valid"},
            lambda name: True,
            "object",
            {"unsupported-ref-name"},
        ),
        ({"enum": "bad"}, lambda name: False, "object", {"invalid-enum"}),
        ({"enum": []}, lambda name: False, "object", {"empty-enum"}),
        ({"enum": [1, 1]}, lambda name: False, "object", {"duplicate-enum-value"}),
        ({"enum": [[]]}, lambda name: False, "object", {"unsupported-enum-value"}),
        (
            {"type": "string", "enum": [1]},
            lambda name: False,
            "str",
            {"enum-type-mismatch"},
        ),
        ({"type": []}, lambda name: False, "object", {"unsupported-union"}),
        ({"type": ["string", 1]}, lambda name: False, "object", {"unsupported-union"}),
        (
            {"type": ["string", "integer"]},
            lambda name: False,
            "object",
            {"unsupported-union"},
        ),
        (
            {"type": "array"},
            lambda name: False,
            "list[object]",
            {"unconstrained-array-items"},
        ),
        (
            {"type": "object", "properties": []},
            lambda name: False,
            "dict[str, object]",
            {"invalid-properties"},
        ),
        ({"type": "mystery"}, lambda name: False, "object", {"unsupported-type"}),
        ({"type": 3}, lambda name: False, "object", {"invalid-type"}),
        ({}, lambda name: False, "object", {"unconstrained-schema"}),
    ],
)
def test_render_type_reports_malformed_and_unsupported_shapes(
    spec: object,
    definition_exists: Any,
    expected_type: str,
    expected_codes: set[str],
) -> None:
    rendered, _refs, diagnostics, _allows_none = _render_type(spec, definition_exists, "/value")
    assert rendered == expected_type
    assert expected_codes <= _codes(diagnostics)


def test_render_type_handles_valid_refs_nullable_unions_and_nested_arrays() -> None:
    rendered, refs, diagnostics, allows_none = _render_type(
        {"type": ["null", "string"]}, lambda name: False, "/nullable"
    )
    assert (rendered, refs, diagnostics, allows_none) == ("str | None", (), (), True)

    rendered, refs, diagnostics, allows_none = _render_type(
        {"type": "array", "items": {"$ref": "#/$defs/Target"}},
        lambda name: name == "Target",
        "/array",
    )
    assert (rendered, refs, diagnostics, allows_none) == (
        "list[Target]",
        ("Target",),
        (),
        False,
    )

    assert _render_type({"type": "object"}, lambda name: False, "/object") == (
        "dict[str, object]",
        (),
        (),
        False,
    )


@pytest.mark.parametrize(
    ("value", "declared_type", "matches"),
    [
        (None, None, True),
        (None, "null", True),
        (False, "boolean", True),
        (1, "integer", True),
        (True, "integer", False),
        (1, "number", True),
        (1.5, "number", True),
        (False, "number", False),
        ("x", "string", True),
        ("x", "object", False),
    ],
)
def test_enum_type_matching_covers_all_supported_primitive_types(
    value: object,
    declared_type: object,
    matches: bool,
) -> None:
    assert _enum_type_matches(value, declared_type) is matches


def test_duplicate_detection_is_type_sensitive() -> None:
    assert _duplicates([1, 1])
    assert not _duplicates([1, True])
    assert not _duplicates([1, 2])


def test_build_enum_reports_invalid_values_duplicates_mismatches_and_types() -> None:
    invalid = _build_enum("E", {"enum": "bad"}, "", "/E", ())
    assert _codes(invalid[7]) == {"invalid-enum"}

    malformed = _build_enum(
        "E",
        {"type": ["string"], "enum": ["x", "x", [], 1]},
        "docs",
        "/E",
        (),
    )
    assert {
        "duplicate-enum-value",
        "unsupported-enum-value",
        "enum-type-mismatch",
        "unsupported-enum-type",
    } <= _codes(malformed[7])
    assert malformed[5] == "docs"


def test_field_collision_diagnostics_are_emitted_for_each_normalized_name() -> None:
    diagnostics = _field_collision_diagnostics(
        ["Café", "Cafe\N{COMBINING ACUTE ACCENT}", "distinct"],
        "/properties",
    )
    assert len(diagnostics) == 2
    assert _codes(diagnostics) == {"field-name-collision"}


def test_build_model_covers_invalid_definition_and_object_metadata() -> None:
    boolean_model = _build_model("Flag", False, lambda name: False, "/Flag")
    assert _codes(boolean_model[7]) == {"unsupported-boolean-schema"}

    scalar_model = _build_model("Value", 1, lambda name: False, "/Value")
    assert _codes(scalar_model[7]) == {"invalid-definition"}

    invalid_name = _build_model("not-valid", {}, lambda name: False, "/not-valid")
    assert "unsupported-definition-name" in _codes(invalid_name[7])

    model = _build_model(
        "Model",
        {
            "type": "object",
            "description": 1,
            "required": ["missing", "missing"],
            "properties": {
                "valid": {"type": "string", "description": 1},
                "class": {"type": "string"},
                "__dict__": {"type": "string"},
                "Café": {"type": "string"},
                "Cafe\N{COMBINING ACUTE ACCENT}": {"type": "string"},
            },
        },
        lambda name: False,
        "/Model",
    )
    assert {
        "invalid-description",
        "duplicate-required-name",
        "unsupported-required-property",
        "unsupported-field-name",
        "reserved-field-name",
        "field-name-collision",
    } <= _codes(model[7])
    valid_field = next(field for field in model[2] if field[0] == "valid")
    assert valid_field[3] == ""


def test_build_model_reports_invalid_required_and_properties_containers() -> None:
    invalid_required = _build_model(
        "Model",
        {"type": "object", "required": "value", "properties": {}},
        lambda name: False,
        "/Model",
    )
    assert "invalid-required" in _codes(invalid_required[7])

    invalid_properties = _build_model(
        "Model",
        {"type": "object", "properties": []},
        lambda name: False,
        "/Model",
    )
    assert "invalid-properties" in _codes(invalid_properties[7])


def test_render_helpers_cover_invalid_names_empty_enums_aliases_objects_and_docs() -> None:
    invalid_name: ModelPayload = ("not-valid", "alias", (), (), "object", "", (), ())
    assert "cannot be emitted" in _render_python(invalid_name)

    empty_enum: ModelPayload = ("Empty", "enum", (), (), "object", "", (), ())
    assert "Never" in _render_python(empty_enum)
    assert "No values are allowed." in _render_doc(empty_enum)

    alias: ModelPayload = (
        "Alias",
        "alias",
        (),
        (),
        "Target",
        "Alias docs",
        ("Alias", "Target"),
        (),
    )
    alias_source = _render_python(alias)
    assert "TYPE_CHECKING" in alias_source
    assert "from .target import Target" in alias_source
    assert "Type alias for `Target`." in _render_doc(alias)
    assert _type_checking_imports(("Self", "Other"), "Self") == ["    from .other import Other"]

    model: ModelPayload = (
        "Model",
        "object",
        (
            ("required", "int", True, "required docs", False),
            ("optional", "str", False, "optional docs", False),
            ("nullable", "str | None", False, "", True),
        ),
        (),
        "",
        "Model docs",
        (),
        (("warning", "message", "warning", ""),),
    )
    source = _render_python(model)
    assert "required: int" in source
    assert "optional: str | None = None" in source
    assert "nullable: str | None = None" in source
    docs = _render_doc(model)
    assert "Model docs" in docs
    assert "required docs" in docs
    assert "at `/`" in docs

    empty_model: ModelPayload = ("EmptyModel", "object", (), (), "", "", (), ())
    assert "    pass" in _render_python(empty_model)


def test_document_queries_cover_invalid_roots_sections_duplicates_and_empty_indexes(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    db = Database()

    _write_schema(schema_path, [1, 2])
    assert _codes(document_diagnostics(db, str(schema_path))) == {"invalid-schema-root"}

    _write_schema(schema_path, {"$defs": []})
    assert _codes(document_diagnostics(db, str(schema_path))) == {"invalid-definitions"}

    _write_schema(
        schema_path,
        {
            "description": 1,
            "$id": 2,
            "$defs": {"Same": {}},
            "definitions": {"Same": {}},
        },
    )
    codes = _codes(document_diagnostics(db, str(schema_path)))
    assert {"invalid-description", "invalid-annotation", "duplicate-definition"} <= codes

    _write_schema(schema_path, {})
    assert index_init(db, str(schema_path)) == "__all__: list[str] = []\n"


def test_definition_queries_return_safe_fallbacks_for_malformed_and_missing_data(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("not json", encoding="utf-8")
    db = Database()
    assert definition_names(db, str(schema_path)) == ()
    assert definition_raw(db, str(schema_path), "Missing") == ""
    assert definition_pointer(db, str(schema_path), "Missing") == ""

    _write_schema(schema_path, {"$defs": {"Present": {"type": "string"}}})
    assert definition_raw(db, str(schema_path), "Missing") == ""
    assert definition_pointer(db, str(schema_path), "Missing") == ""
    missing = definition_model(db, str(schema_path), "Missing")
    assert _codes(missing[7]) == {"missing-definition"}
