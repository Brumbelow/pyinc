from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pyinc import Database
from pyinc_codegen.models import DiagnosticPayload, ModelPayload
from pyinc_codegen.schema import (
    _EMITTER_BOUND_NAMES,
    _IGNORED_KEYWORDS,
    _annotation_diagnostics,
    _build_enum,
    _build_model,
    _canonical_json_token,
    _constraint_shape_problem,
    _decode_pointer_segment,
    _definition_entries,
    _definition_name_diagnostics,
    _DuplicateKeyError,
    _duplicates,
    _effective_type,
    _enum_type_matches,
    _field_collision_diagnostics,
    _ignored_keyword_diagnostics,
    _InvalidJsonError,
    _is_null_schema,
    _load,
    _local_ref_target,
    _mapping_value_schema,
    _module_name_diagnostics,
    _object_from_pairs,
    _parse_float,
    _percent_decode,
    _render_combinator,
    _render_doc,
    _render_python,
    _render_type,
    _schema_node_diagnostics,
    _snake,
    _type_checking_imports,
    _typing_import_names,
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


def test_definition_name_diagnostics_cover_every_emitter_binding() -> None:
    for reserved in sorted(_EMITTER_BOUND_NAMES):
        assert _codes(_definition_name_diagnostics(reserved, f"/$defs/{reserved}")) == {
            "reserved-definition-name"
        }
    # The comparison runs on the NFKC-normalized name, like the emitted class name.
    fullwidth = "\N{FULLWIDTH LATIN SMALL LETTER S}tr"
    assert _codes(_definition_name_diagnostics(fullwidth, "/$defs/str")) == {
        "reserved-definition-name"
    }
    assert _definition_name_diagnostics("Str2", "/$defs/Str2") == ()
    assert _definition_name_diagnostics("STR", "/$defs/STR") == ()


def test_property_names_shadowing_every_emitter_binding_are_rejected() -> None:
    for reserved in sorted(_EMITTER_BOUND_NAMES):
        model = _build_model(
            "Thing",
            {"type": "object", "properties": {reserved: {"type": "string"}}},
            lambda name: False,
            "/$defs/Thing",
        )
        assert [(code, severity, pointer) for code, _message, severity, pointer in model[7]] == [
            ("reserved-field-name", "error", f"/$defs/Thing/properties/{reserved}")
        ]
        assert model[2] == ()

    # The comparison runs on the NFKC-normalized name, like the emitted field.
    fullwidth = "\N{FULLWIDTH LATIN SMALL LETTER S}tr"
    normalized = _build_model(
        "Thing",
        {"type": "object", "properties": {fullwidth: {"type": "string"}}},
        lambda name: False,
        "/$defs/Thing",
    )
    assert _codes(normalized[7]) == {"reserved-field-name"}

    unaffected = _build_model(
        "Thing",
        {
            "type": "object",
            "properties": {"str2": {"type": "string"}, "STR": {"type": "string"}},
        },
        lambda name: False,
        "/$defs/Thing",
    )
    assert unaffected[7] == ()
    assert [field[0] for field in unaffected[2]] == ["STR", "str2"]


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

    # An inline enum selects the shape in every context, so nothing is reported.
    assert _schema_node_diagnostics({"enum": ["x"]}, "/enum", definition_context=False) == ()

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

    # Ignored keywords are removed from the structural set, so they are never
    # reported as unsupported or as conflicting with the selected shape.
    ignored_cases: tuple[tuple[dict[str, object], bool], ...] = (
        ({"type": "string", "format": "email"}, False),
        ({"$ref": "#/$defs/A", "minimum": 0}, False),
        ({"enum": ["x"], "default": "x"}, True),
    )
    for spec, definition_context in ignored_cases:
        codes = _codes(
            _schema_node_diagnostics(spec, "/node", definition_context=definition_context)
        )
        assert codes == {"ignored-constraint"}


@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        ("minimum", 0, None),
        ("minimum", 1.5, None),
        ("minimum", True, "a number"),
        ("exclusiveMaximum", "10", "a number"),
        ("multipleOf", 2, None),
        ("multipleOf", 0, "a number greater than zero"),
        ("maxLength", 0, None),
        ("minItems", -1, "a non-negative integer"),
        ("maxItems", 1.0, "a non-negative integer"),
        ("format", "email", None),
        ("pattern", 7, "a string"),
        ("uniqueItems", True, None),
        ("readOnly", "yes", "a boolean"),
        ("examples", [1], None),
        ("examples", {}, "an array"),
        ("default", {"anything": [1, None]}, None),
    ],
)
def test_constraint_shape_problem_pins_each_accepted_value_shape(
    name: str,
    value: object,
    problem: str | None,
) -> None:
    assert _constraint_shape_problem(name, value) == problem


def test_ignored_keyword_diagnostics_sort_by_keyword_and_separate_shape_failures() -> None:
    diagnostics = _ignored_keyword_diagnostics(
        {
            "type": "string",
            "minLength": 1,
            "format": 7,
            "default": "x",
            "additionalProperties": {"type": "string"},
        },
        "/node",
    )
    assert [(code, pointer) for code, _message, _severity, pointer in diagnostics] == [
        ("unsupported-construct", "/node/additionalProperties"),
        ("ignored-constraint", "/node/default"),
        ("invalid-constraint", "/node/format"),
        ("ignored-constraint", "/node/minLength"),
    ]
    severities = {code: severity for code, _message, severity, _pointer in diagnostics}
    assert severities == {
        "unsupported-construct": "error",
        "ignored-constraint": "warning",
        "invalid-constraint": "error",
    }
    assert _ignored_keyword_diagnostics({"type": "string"}, "/node") == ()


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"type": "object", "additionalProperties": {"type": "string"}}, {"type": "string"}),
        ({"additionalProperties": {"type": "string"}}, {"type": "string"}),
        ({"type": ["object", "null"], "additionalProperties": {}}, {}),
        ({"type": "object", "additionalProperties": False}, None),
        ({"type": "string", "additionalProperties": {"type": "string"}}, None),
        ({"$ref": "#/$defs/A", "additionalProperties": {"type": "string"}}, None),
        ({"enum": ["a"], "additionalProperties": {"type": "string"}}, None),
        ({"allOf": [{}], "additionalProperties": {"type": "string"}}, None),
        ({"type": "object"}, None),
    ],
)
def test_mapping_value_schema_only_claims_nodes_that_render_as_mappings(
    spec: dict[str, object],
    expected: dict[str, object] | None,
) -> None:
    assert _mapping_value_schema(spec) == expected


def test_schema_valued_additional_properties_is_reported_only_where_it_is_ignored() -> None:
    spec: dict[str, object] = {"type": "object", "additionalProperties": {"type": "string"}}
    # Property position compiles it into the value type; a definition generates a
    # dataclass instead, so there the keyword really is dropped.
    assert _schema_node_diagnostics(spec, "/node", definition_context=False) == ()
    assert [
        (code, pointer)
        for code, _message, _severity, pointer in _schema_node_diagnostics(
            spec, "/node", definition_context=True
        )
    ] == [("unsupported-construct", "/node/additionalProperties")]


@pytest.mark.parametrize(
    ("branch", "is_null"),
    [
        ({"type": "null"}, True),
        ({"type": "null", "title": "nothing"}, True),
        ({"type": "null", "minimum": 0}, False),
        ({"type": ["null"]}, False),
        ({"enum": [None]}, False),
        ("null", False),
    ],
)
def test_null_branch_recognition_is_limited_to_the_bare_null_schema(
    branch: object,
    is_null: bool,
) -> None:
    assert _is_null_schema(branch) is is_null


def test_render_combinator_reports_each_unsupported_shape_at_its_keyword() -> None:
    for keyword, branches, fragment in (
        ("allOf", [{}, {}], "single-branch"),
        ("allOf", {"$ref": "#/$defs/A"}, "single-branch"),
        ("anyOf", [{"type": "string"}, {"type": "integer"}], "union"),
        ("anyOf", [], "union"),
    ):
        rendered, refs, diagnostics, allows_none = _render_combinator(
            keyword, branches, lambda name: True, "/node"
        )
        assert (rendered, refs, allows_none) == ("object", (), False)
        assert [(code, pointer) for code, _m, _s, pointer in diagnostics] == [
            ("unsupported-construct", f"/node/{keyword}")
        ]
        assert fragment in diagnostics[0][1]

    # A branch that is itself nullable is not wrapped twice.
    assert _render_combinator(
        "anyOf",
        [{"type": ["string", "null"]}, {"type": "null"}],
        lambda name: False,
        "/node",
    ) == ("str | None", (), (), True)
    # Diagnostics from inside the selected branch are reported at that branch.
    _rendered, _refs, diagnostics, _allows_none = _render_combinator(
        "allOf", [{"$ref": "#/$defs/Missing"}], lambda name: False, "/node"
    )
    assert [(code, pointer) for code, _m, _s, pointer in diagnostics] == [
        ("unknown-ref", "/node/allOf/0/$ref")
    ]


def test_two_null_branches_name_no_type_to_make_optional() -> None:
    rendered, refs, diagnostics, allows_none = _render_combinator(
        "anyOf", [{"type": "null"}, {"type": "null"}], lambda name: True, "/node"
    )
    assert (rendered, refs, allows_none) == ("object", (), False)
    assert [(code, pointer) for code, _m, _s, pointer in diagnostics] == [
        ("unsupported-construct", "/node/anyOf")
    ]
    assert "names no type" in diagnostics[0][1]


def test_null_branch_annotations_are_reported_in_branch_order() -> None:
    _rendered, _refs, diagnostics, _allows_none = _render_combinator(
        "anyOf",
        [{"type": "null", "title": 1}, {"$ref": "#/$defs/Missing"}],
        lambda name: False,
        "/node",
    )
    assert [(code, pointer) for code, _m, _s, pointer in diagnostics] == [
        ("invalid-annotation", "/node/anyOf/0/title"),
        ("unknown-ref", "/node/anyOf/1/$ref"),
    ]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"allOf": [{}], "type": "object"}, [("ambiguous-schema-combination", "/node/type")]),
        ({"anyOf": [{}], "oneOf": [{}]}, [("ambiguous-schema-combination", "/node/oneOf")]),
        ({"allOf": [{}], "anyOf": [{}]}, [("ambiguous-schema-combination", "/node/anyOf")]),
        ({"const": "x", "type": "string"}, []),
        ({"const": "x", "enum": ["x"]}, [("ambiguous-schema-combination", "/node/const")]),
        ({"const": "x", "not": {}}, [("unsupported-construct", "/node/not")]),
    ],
)
def test_shape_selecting_keywords_report_their_siblings(
    spec: dict[str, object],
    expected: list[tuple[str, str]],
) -> None:
    diagnostics = _schema_node_diagnostics(spec, "/node", definition_context=False)
    assert [(code, pointer) for code, _message, _severity, pointer in diagnostics] == expected


def test_typing_import_names_are_ordered_and_only_emitted_when_used() -> None:
    assert _typing_import_names(["str | None"], type_checking=False) == []
    assert _typing_import_names(["str | None"], type_checking=True) == ["TYPE_CHECKING"]
    assert _typing_import_names(["Literal['a']"], type_checking=False) == ["Literal"]
    assert _typing_import_names(["Literal['a']", "list[Literal[1]]"], type_checking=True) == [
        "TYPE_CHECKING",
        "Literal",
    ]


def test_prefix_items_suppresses_only_the_unconstrained_items_warning() -> None:
    rendered, refs, diagnostics, allows_none = _render_type(
        {"type": "array", "prefixItems": [{"type": "string"}]},
        lambda name: False,
        "/tuple",
        validate_current=False,
    )
    assert (rendered, refs, diagnostics, allows_none) == ("list[object]", (), (), False)


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
        ({"enum": [1, 1]}, lambda name: False, "Literal[1, 1]", {"duplicate-enum-value"}),
        ({"enum": [[]]}, lambda name: False, "object", {"unsupported-enum-value"}),
        (
            {"type": "string", "enum": [1]},
            lambda name: False,
            "Literal[1]",
            {"enum-type-mismatch"},
        ),
        ({"const": 1.5}, lambda name: False, "object", {"unsupported-const-value"}),
        (
            {"type": "integer", "const": "x"},
            lambda name: False,
            "Literal['x']",
            {"const-type-mismatch"},
        ),
        (
            {"allOf": [{"type": "string"}, {"type": "integer"}]},
            lambda name: False,
            "object",
            {"unsupported-construct"},
        ),
        ({"allOf": "one"}, lambda name: False, "object", {"unsupported-construct"}),
        (
            {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            lambda name: False,
            "object",
            {"unsupported-construct"},
        ),
        (
            {"anyOf": [{"type": "string"}, {"type": "null", "minimum": 0}]},
            lambda name: False,
            "object",
            {"unsupported-construct"},
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
            {"type": "array", "items": [{"type": "string"}]},
            lambda name: False,
            "list[object]",
            {"unsupported-tuple-items"},
        ),
        (
            {"type": "object", "properties": []},
            lambda name: False,
            "dict[str, object]",
            {"invalid-properties"},
        ),
        ({"type": "mystery"}, lambda name: False, "object", {"unsupported-type"}),
        ({"type": 3}, lambda name: False, "object", {"invalid-type"}),
        ({"type": None}, lambda name: False, "object", {"invalid-type"}),
        (
            {"type": None, "additionalProperties": {"type": "string"}},
            lambda name: False,
            "object",
            {"invalid-type"},
        ),
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
        # The supported nullable union admits the type it names and the null it
        # adds, in either branch order.
        ("x", ["string", "null"], True),
        (None, ["string", "null"], True),
        (None, ["null", "string"], True),
        (1, ["integer", "null"], True),
        (1, ["string", "null"], False),
        ("x", ["object", "null"], False),
        # Unions the compiler cannot render name no single type to check against.
        ("x", ["string", "integer"], False),
        ("x", ["string"], False),
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


def test_build_enum_accepts_a_nullable_union_and_still_checks_its_members() -> None:
    nullable = _build_enum("E", {"type": ["string", "null"], "enum": ["red", None]}, "", "/E", ())
    assert nullable[7] == ()
    assert (nullable[3], nullable[4]) == (("'red'", "None"), "str")

    mismatched = _build_enum("E", {"type": ["string", "null"], "enum": ["red", 7]}, "", "/E", ())
    assert [(code, pointer) for code, _message, _severity, pointer in mismatched[7]] == [
        ("enum-type-mismatch", "/E/enum/1")
    ]


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


def test_bare_object_definition_warns_while_the_same_property_is_a_mapping() -> None:
    model = _build_model("Bag", {"type": "object"}, lambda name: False, "/$defs/Bag")
    assert (model[1], model[2]) == ("object", ())
    assert [(code, severity, pointer) for code, _message, severity, pointer in model[7]] == [
        ("unconstrained-object-model", "warning", "/$defs/Bag")
    ]
    # In property position the same schema keeps its data as a mapping, so the
    # asymmetry that motivates the warning does not exist there.
    assert _render_type({"type": "object"}, lambda name: False, "/value") == (
        "dict[str, object]",
        (),
        (),
        False,
    )
    # A mapping value schema does constrain the instance. It is rejected on its
    # own keyword, and one cause reports one diagnostic.
    mapping = _build_model(
        "Bag",
        {"type": "object", "additionalProperties": {"type": "string"}},
        lambda name: False,
        "/$defs/Bag",
    )
    assert [(code, pointer) for code, _message, _severity, pointer in mapping[7]] == [
        ("unsupported-construct", "/$defs/Bag/additionalProperties")
    ]


def test_build_model_selects_a_shape_keyword_before_the_type_driven_branch() -> None:
    const_model = _build_model(
        "Kind",
        {"type": "object", "const": "ticket"},
        lambda name: False,
        "/$defs/Kind",
    )
    assert (const_model[1], const_model[4]) == ("alias", "Literal['ticket']")
    assert _codes(const_model[7]) == {"const-type-mismatch"}

    ref_model = _build_model(
        "Ref",
        {"$ref": "#/$defs/Target", "type": "object", "properties": {}},
        lambda name: True,
        "/$defs/Ref",
    )
    assert (ref_model[1], ref_model[4], ref_model[6]) == ("alias", "Target", ("Target",))
    assert _codes(ref_model[7]) == {"ambiguous-schema-combination"}


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

    _write_schema(
        schema_path,
        {"oneOf": [{"type": "string"}], "type": "object", "properties": {}, "default": 1},
    )
    root = document_diagnostics(db, str(schema_path))
    assert [(code, pointer) for code, _message, _severity, pointer in root] == [
        ("ignored-constraint", "/default"),
        ("unsupported-construct", "/oneOf"),
        ("unsupported-root-schema", ""),
    ]
    assert "'properties', 'type'" in root[2][1]

    _write_schema(schema_path, {})
    assert index_init(db, str(schema_path)) == "__all__: list[str] = []\n"


_ROOT_IGNORED_VALUES: dict[str, object] = {
    "additionalProperties": False,
    "default": 1,
    "deprecated": True,
    "examples": [1],
    "exclusiveMaximum": 10,
    "exclusiveMinimum": 0,
    "format": "email",
    "maxItems": 5,
    "maxLength": 5,
    "maximum": 10,
    "minItems": 1,
    "minLength": 1,
    "minimum": 0,
    "multipleOf": 2,
    "pattern": "^a$",
    "readOnly": True,
    "uniqueItems": True,
    "writeOnly": True,
}


def test_ignored_keywords_at_the_document_root_warn_without_blocking(tmp_path: Path) -> None:
    assert set(_ROOT_IGNORED_VALUES) == set(_IGNORED_KEYWORDS)
    schema_path = tmp_path / "schema.json"
    db = Database()
    defs = {"$defs": {"K": {"type": "object", "properties": {"a": {"type": "string"}}}}}

    for keyword, value in _ROOT_IGNORED_VALUES.items():
        _write_schema(schema_path, {**defs, keyword: value})
        assert [
            (code, severity, pointer)
            for code, _message, severity, pointer in document_diagnostics(db, str(schema_path))
        ] == [("ignored-constraint", "warning", f"/{keyword}")]

    # A model keyword and an unrecognized keyword state the same rule once,
    # against the whole document, and do block.
    _write_schema(schema_path, {**defs, "type": "object", "wibble": 1})
    assert [
        (code, severity, pointer)
        for code, _message, severity, pointer in document_diagnostics(db, str(schema_path))
    ] == [("unsupported-root-schema", "error", "")]


def test_reordering_definition_keys_keeps_incremental_document_diagnostics_identical_to_fresh(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    fragments: dict[str, Any] = {
        "Class": {"type": "string"},
        "Import": {"type": "integer"},
    }
    states: tuple[dict[str, Any], ...] = (
        {"$defs": {name: fragments[name] for name in ("Class", "Import")}},
        {"$defs": {name: fragments[name] for name in ("Import", "Class")}},
    )

    incremental_db = Database()
    for state in states:
        _write_schema(schema_path, state)
        incremental = document_diagnostics(incremental_db, str(schema_path))
        fresh = document_diagnostics(Database(), str(schema_path))
        assert incremental == fresh


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
