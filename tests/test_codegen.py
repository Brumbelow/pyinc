from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import pyinc_codegen
from pyinc import Database
from pyinc_codegen import (
    DiagnosticSeverity,
    SchemaGenerationError,
    generate,
    schema_analysis,
)
from pyinc_codegen.schema import (
    _canonical_json_token,
    definition_names,
    model_doc,
    model_python,
    schema_text,
)

_SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "schemas" / "sample.schema.json"


def _write_schema(path: Path, schema: object) -> None:
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith(".pyinc-action.")
    }


# --------------------------------------------------------------------------- #
# Task 2B.1 — canonical cutoff (C1)
# --------------------------------------------------------------------------- #


def test_canonical_token_ignores_whitespace_and_key_order() -> None:
    assert _canonical_json_token('{"a": 1, "b": 2}') == _canonical_json_token('{\n "b":2,\n "a":1}')


def test_whitespace_edit_backdates_and_writes_nothing(tmp_path: Path) -> None:  # C1
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    schema = {"$defs": {"A": {"type": "object", "properties": {"x": {"type": "integer"}}}}}
    _write_schema(p, schema)
    db = Database(mode="strict")
    generate(db, str(p), out)
    p.write_text(json.dumps(schema, indent=4, sort_keys=True), encoding="utf-8")
    res = generate(db, str(p), out)
    assert res.created == () and res.updated == () and res.repaired == ()
    assert res.deleted == ()
    assert db.inspect(schema_text, str(p)).last_recompute == "backdated"
    assert db.inspect(model_python, str(p), "A").last_decision == "reused"


# --------------------------------------------------------------------------- #
# Task 2B.2 — defs, refs, models, diagnostics
# --------------------------------------------------------------------------- #


def test_definition_names_merges_defs_and_definitions(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    _write_schema(p, {"$defs": {"B": {}}, "definitions": {"A": {}}})
    db = Database(mode="strict")
    assert definition_names(db, str(p)) == ("A", "B")


def test_model_captures_fields_required_enum_nullable_refs(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    _write_schema(p, json.loads(_SAMPLE.read_text(encoding="utf-8")))
    db = Database(mode="strict")
    analysis = schema_analysis(db, str(p))
    by_name = {m.name: m for m in analysis.models}

    assert by_name["Status"].kind == "enum"
    assert by_name["Status"].enum_values == ("'active'", "'inactive'", "'pending'")

    address = by_name["Address"]
    zip_field = next(f for f in address.fields if f.name == "zip")
    assert zip_field.type_expr == "str | None"
    assert not zip_field.required
    street_field = next(f for f in address.fields if f.name == "street")
    assert street_field.required

    user = by_name["User"]
    assert set(user.refs) == {"Address", "Status"}
    tags = next(f for f in user.fields if f.name == "tags")
    assert tags.type_expr == "list[str]"


def test_unsupported_construct_yields_diagnostic(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    _write_schema(
        p,
        {
            "$defs": {
                "A": {
                    "type": "object",
                    "properties": {"x": {"allOf": [{"type": "string"}, {"type": "integer"}]}},
                }
            }
        },
    )
    db = Database(mode="strict")
    analysis = schema_analysis(db, str(p))
    codes = {d.code for d in analysis.diagnostics}
    assert "unsupported-construct" in codes
    diagnostic = next(d for d in analysis.diagnostics if d.code == "unsupported-construct")
    assert diagnostic.severity is DiagnosticSeverity.ERROR
    assert diagnostic.json_pointer == "/$defs/A/properties/x/allOf"


def test_validation_keywords_warn_in_each_supported_context(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "code": {"type": "string", "format": "uuid"},
                        "count": {"type": "integer", "minimum": 0},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                    },
                }
            }
        },
    )

    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert analysis.errors == ()
    ignored = {
        diagnostic.json_pointer: diagnostic
        for diagnostic in analysis.diagnostics
        if diagnostic.code == "ignored-constraint"
    }
    assert set(ignored) == {
        "/$defs/Thing/additionalProperties",
        "/$defs/Thing/properties/code/format",
        "/$defs/Thing/properties/count/minimum",
        "/$defs/Thing/properties/tags/minItems",
    }
    for pointer, diagnostic in ignored.items():
        assert diagnostic.severity is DiagnosticSeverity.WARNING
        # The warning names the keyword it is not enforcing.
        assert repr(pointer.rsplit("/", 1)[-1]) in diagnostic.message

    generate(db, schema_path, out)
    source = (out / "thing.py").read_text(encoding="utf-8")
    assert "code: str | None = None" in source
    assert "count: int | None = None" in source
    assert "tags: list[str] | None = None" in source


def test_nested_array_schema_keywords_are_validated_recursively(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Nested": {
                    "type": "object",
                    "properties": {
                        "values": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "format": "date-time"},
                            },
                        }
                    },
                }
            }
        },
    )

    analysis = schema_analysis(Database(mode="strict"), schema_path)
    assert analysis.errors == ()
    assert {
        diagnostic.json_pointer
        for diagnostic in analysis.diagnostics
        if diagnostic.code == "ignored-constraint"
    } == {
        "/$defs/Nested/properties/values/items/minItems",
        "/$defs/Nested/properties/values/items/items/format",
    }


def test_malformed_constraint_values_are_errors(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Good": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    _write_schema(
        schema_path,
        {
            "$defs": {
                "Good": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "format": 7, "pattern": "^ok$"},
                        "count": {"type": "integer", "minimum": True, "multipleOf": 0},
                        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": -1},
                    },
                }
            }
        },
    )
    analysis = schema_analysis(db, schema_path)
    assert {
        diagnostic.json_pointer
        for diagnostic in analysis.errors
        if diagnostic.code == "invalid-constraint"
    } == {
        "/$defs/Good/properties/code/format",
        "/$defs/Good/properties/count/minimum",
        "/$defs/Good/properties/count/multipleOf",
        "/$defs/Good/properties/tags/maxItems",
    }
    # A well-formed sibling of a malformed keyword is still merely ignored.
    assert "/$defs/Good/properties/code/pattern" in {
        diagnostic.json_pointer
        for diagnostic in analysis.diagnostics
        if diagnostic.code == "ignored-constraint"
    }

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    assert _tree(out) == before


def test_inline_enum_and_const_render_literal_types(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Status": {"type": "string", "enum": ["open", "closed"]},
                "Ticket": {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "ticket"},
                        "priority": {"type": "integer", "enum": [1, 2, 3]},
                        "state": {"enum": ["open", "closed", None]},
                        "labels": {"type": "array", "items": {"enum": ["red", "blue"]}},
                        "status": {"$ref": "#/$defs/Status"},
                    },
                    "required": ["kind", "priority"],
                },
            }
        },
    )
    db = Database(mode="strict")
    assert schema_analysis(db, schema_path).errors == ()
    generate(db, schema_path, out)

    # ``Literal`` is imported exactly once, after ``TYPE_CHECKING``, and the
    # enum member that is ``null`` makes the field nullable without a second
    # ``| None``.
    assert (out / "ticket.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
        "\n"
        "from dataclasses import dataclass\n"
        "from typing import TYPE_CHECKING, Literal\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from .status import Status\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Ticket:\n"
        "    kind: Literal['ticket']\n"
        "    priority: Literal[1, 2, 3]\n"
        "    labels: list[Literal['red', 'blue']] | None = None\n"
        "    state: Literal['open', 'closed', None] = None\n"
        "    status: Status | None = None\n"
    )
    compile((out / "ticket.py").read_text(encoding="utf-8"), "ticket.py", "exec")


def test_const_definition_renders_a_literal_alias(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Kind": {"const": "ticket"}}})
    db = Database(mode="strict")
    assert schema_analysis(db, schema_path).errors == ()
    generate(db, schema_path, out)

    # The alias branch needs ``Literal`` too: the forward reference is a string,
    # but the name must still resolve for a type checker.
    assert (out / "kind.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
        "\n"
        "from typing import Literal, TypeAlias\n"
        "\n"
        "Kind: TypeAlias = \"Literal['ticket']\"\n"
    )
    compile((out / "kind.py").read_text(encoding="utf-8"), "kind.py", "exec")


def test_models_without_literals_import_nothing_extra(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {"$defs": {"Plain": {"type": "object", "properties": {"name": {"type": "string"}}}}},
    )
    generate(Database(mode="strict"), schema_path, out)
    assert "from typing import" not in (out / "plain.py").read_text(encoding="utf-8")


def test_float_literal_members_are_rejected_for_const_and_enum(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Ratio": {
                    "type": "object",
                    "properties": {
                        "exact": {"const": 1.5},
                        "choice": {"enum": [1.5, 2.5]},
                    },
                }
            }
        },
    )
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert {(d.code, d.json_pointer) for d in analysis.errors} == {
        ("unsupported-const-value", "/$defs/Ratio/properties/exact/const"),
        ("unsupported-enum-value", "/$defs/Ratio/properties/choice/enum/0"),
        ("unsupported-enum-value", "/$defs/Ratio/properties/choice/enum/1"),
    }
    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)


def test_const_value_must_agree_with_the_declared_type(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "properties": {"flag": {"type": "integer", "const": "yes"}},
                }
            }
        },
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    diagnostic = next(d for d in analysis.errors if d.code == "const-type-mismatch")
    assert diagnostic.json_pointer == "/$defs/Thing/properties/flag/const"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        pytest.param("ticket", "const-type-mismatch", id="scalar"),
        pytest.param({"a": 1}, "unsupported-const-value", id="non-scalar"),
    ],
)
def test_const_beside_type_object_is_rejected_at_definition_position(
    value: object,
    code: str,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Kind": {"type": "object", "const": value}}})
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    # A definition selects its shape before ``type`` is read, so the const is
    # diagnosed instead of being dropped into an empty frozen dataclass.
    assert [(d.code, d.json_pointer) for d in analysis.diagnostics] == [
        (code, "/$defs/Kind/const")
    ]
    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    assert _tree(out) == {}


def test_nullable_and_single_branch_references_render_model_types(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Address": {"type": "object", "properties": {"city": {"type": "string"}}},
                "MaybeAddress": {"anyOf": [{"$ref": "#/$defs/Address"}, {"type": "null"}]},
                "Profile": {
                    "type": "object",
                    "properties": {
                        "billing": {"allOf": [{"$ref": "#/$defs/Address"}]},
                        "home": {"anyOf": [{"$ref": "#/$defs/Address"}, {"type": "null"}]},
                        "work": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/Address"}]},
                    },
                    "required": ["billing"],
                },
            }
        },
    )
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert analysis.errors == ()
    # References found through a combinator join the model's reference graph.
    assert {model.name: model.refs for model in analysis.models} == {
        "Address": (),
        "MaybeAddress": ("Address",),
        "Profile": ("Address",),
    }

    generate(db, schema_path, out)
    assert (out / "profile.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
        "\n"
        "from dataclasses import dataclass\n"
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from .address import Address\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Profile:\n"
        "    billing: Address\n"
        "    home: Address | None = None\n"
        "    work: Address | None = None\n"
    )
    assert (out / "maybe_address.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
        "\n"
        "from typing import TYPE_CHECKING, TypeAlias\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from .address import Address\n"
        "\n"
        "MaybeAddress: TypeAlias = 'Address | None'\n"
    )


def test_null_branch_annotations_are_validated_like_every_other_node(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "anyOf": [{"type": "string"}, {"type": "null", "description": 123}]
                        }
                    },
                }
            }
        },
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    # The null branch names optionality rather than a type, but it is still a
    # schema node, so a malformed annotation blocks there as it does anywhere.
    assert [(d.code, d.json_pointer) for d in analysis.diagnostics] == [
        ("invalid-description", "/$defs/Thing/properties/name/anyOf/1/description")
    ]


def test_any_of_two_null_branches_is_rejected(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "properties": {"name": {"anyOf": [{"type": "null"}, {"type": "null"}]}},
                }
            }
        },
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    # Neither branch names a type to make optional, so the union carries no
    # information — a one- or three-branch 'anyOf' is an error for the same reason.
    diagnostic = next(d for d in analysis.errors if d.code == "unsupported-construct")
    assert diagnostic.json_pointer == "/$defs/Thing/properties/name/anyOf"


@pytest.mark.parametrize(
    ("spec", "keyword"),
    [
        ({"allOf": [{"$ref": "#/$defs/Address"}, {"type": "object"}]}, "allOf"),
        ({"anyOf": [{"$ref": "#/$defs/Address"}, {"type": "string"}]}, "anyOf"),
        ({"anyOf": [{"$ref": "#/$defs/Address"}]}, "anyOf"),
        ({"oneOf": [{"$ref": "#/$defs/Address"}, {"type": "null"}]}, "oneOf"),
    ],
)
def test_multi_branch_combinators_remain_errors(
    spec: dict[str, Any],
    keyword: str,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Address": {"type": "object", "properties": {"city": {"type": "string"}}},
                "Holder": {"type": "object", "properties": {"value": spec}},
            }
        },
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    diagnostic = next(d for d in analysis.errors if d.code == "unsupported-construct")
    assert diagnostic.json_pointer == f"/$defs/Holder/properties/value/{keyword}"


def test_schema_valued_additional_properties_compile_to_mappings(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Address": {"type": "object", "properties": {"city": {"type": "string"}}},
                "Registry": {
                    "type": "object",
                    "properties": {
                        "free": {"type": "object", "additionalProperties": True},
                        "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                        "matrix": {
                            "additionalProperties": {
                                "type": "array",
                                "items": {"type": "integer"},
                            }
                        },
                        "places": {
                            "type": "object",
                            "additionalProperties": {"$ref": "#/$defs/Address"},
                        },
                    },
                },
            }
        },
    )
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert analysis.errors == ()
    # A boolean value is still unenforceable and still merely ignored.
    assert [(d.code, d.json_pointer) for d in analysis.diagnostics] == [
        ("ignored-constraint", "/$defs/Registry/properties/free/additionalProperties")
    ]

    generate(db, schema_path, out)
    assert (out / "registry.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
        "\n"
        "from dataclasses import dataclass\n"
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from .address import Address\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Registry:\n"
        "    free: dict[str, object] | None = None\n"
        "    labels: dict[str, str] | None = None\n"
        "    matrix: dict[str, list[int]] | None = None\n"
        "    places: dict[str, Address] | None = None\n"
    )


def test_schema_valued_additional_properties_stays_unsupported(tmp_path: Path) -> None:
    # A definition of type object generates a dataclass, which cannot carry
    # free-form entries: the keyword is only compiled in property position.
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Bag": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "properties": {"name": {"type": "string"}},
                }
            }
        },
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    diagnostic = next(d for d in analysis.errors if d.code == "unsupported-construct")
    assert diagnostic.json_pointer == "/$defs/Bag/additionalProperties"


def test_a_rejected_mapping_definition_reports_only_its_own_cause(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {"$defs": {"Bag": {"type": "object", "additionalProperties": {"type": "string"}}}},
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    # The author did constrain the instance, so the 'model with no fields'
    # warning would name a cause that is not the one being reported.
    assert [(d.code, d.json_pointer) for d in analysis.diagnostics] == [
        ("unsupported-construct", "/$defs/Bag/additionalProperties")
    ]


def test_nullable_object_definition_renders_a_mapping_alias_without_warning(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Bag": {"type": ["object", "null"]}}})
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    # ``unconstrained-object-model`` names the empty dataclass that a bare
    # ``{"type": "object"}`` definition generates. This spelling takes the alias
    # path instead and keeps the instance data, so it has nothing to warn about.
    assert analysis.diagnostics == ()

    generate(db, schema_path, out)
    assert (out / "bag.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
        "\n"
        "from typing import TypeAlias\n"
        "\n"
        "Bag: TypeAlias = 'dict[str, object] | None'\n"
    )


def test_object_definition_without_properties_warns_without_blocking(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Bag": {"type": "object"},
                "Declared": {"type": "object", "properties": {}},
            }
        },
    )
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert analysis.errors == ()
    # Declaring an empty property set is a statement about the model; omitting
    # the keyword entirely is the case that silently drops instance data.
    assert [(d.code, d.json_pointer) for d in analysis.diagnostics] == [
        ("unconstrained-object-model", "/$defs/Bag")
    ]
    assert next(d for d in analysis.diagnostics).severity is DiagnosticSeverity.WARNING

    generate(db, schema_path, out)
    assert "    pass" in (out / "bag.py").read_text(encoding="utf-8")
    assert "unconstrained-object-model" in (out / "docs" / "bag.md").read_text(encoding="utf-8")
    assert "unconstrained-object-model" not in (out / "docs" / "declared.md").read_text(
        encoding="utf-8"
    )


_FIELDLESS_MODEL = (
    "from __future__ import annotations\n"
    "\n"
    "from dataclasses import dataclass\n"
    "\n"
    "\n"
    "@dataclass(frozen=True)\n"
    "class K:\n"
    "    pass\n"
)


@pytest.mark.parametrize(
    "definition",
    [
        {"type": "object", "properties": {}},
        {"properties": {}},
        {"type": "object", "properties": {}, "required": []},
    ],
    ids=["typed", "type-less", "empty-required"],
)
def test_empty_properties_definition_emits_a_fieldless_dataclass_with_no_diagnostic(
    definition: dict[str, Any],
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"K": definition}})
    db = Database(mode="strict")
    assert schema_analysis(db, schema_path).diagnostics == ()

    generate(db, schema_path, out)
    assert (out / "k.py").read_text(encoding="utf-8") == _FIELDLESS_MODEL
    assert (out / "docs" / "k.md").read_text(encoding="utf-8") == "# K\n\nFields:\n"


def test_absent_properties_emits_the_same_model_and_differs_only_in_the_doc(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"K": {"type": "object"}}})
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert [(d.code, d.json_pointer) for d in analysis.diagnostics] == [
        ("unconstrained-object-model", "/$defs/K")
    ]

    generate(db, schema_path, out)
    assert (out / "k.py").read_text(encoding="utf-8") == _FIELDLESS_MODEL
    assert (out / "docs" / "k.md").read_text(encoding="utf-8") == (
        "# K\n"
        "\n"
        "Fields:\n"
        "- warning diagnostic `unconstrained-object-model` at `/$defs/K`: an object definition "
        "without 'properties' generates a model with no fields, so it represents none of the "
        "data it accepts\n"
    )


def test_tuple_form_items_is_reported_as_an_unsupported_tuple(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {"$defs": {"Pair": {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}}},
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    codes = {diagnostic.code for diagnostic in analysis.diagnostics}
    # The draft-07 tuple form is a valid schema node, so it must not be reported
    # as one that is neither an object nor a boolean.
    assert "invalid-schema-node" not in codes
    diagnostic = next(d for d in analysis.errors if d.code == "unsupported-tuple-items")
    assert diagnostic.json_pointer == "/$defs/Pair/items"


def test_prefix_items_does_not_also_report_unconstrained_items(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {"$defs": {"Pair": {"type": "array", "prefixItems": [{"type": "string"}]}}},
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    assert "unconstrained-array-items" not in {d.code for d in analysis.diagnostics}
    diagnostic = next(d for d in analysis.errors if d.code == "unsupported-construct")
    assert diagnostic.json_pointer == "/$defs/Pair/prefixItems"


def test_ambiguous_schema_combinations_are_rejected_at_each_sibling(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Target": {"type": "string"},
                "RefEnum": {"$ref": "#/$defs/Target", "enum": ["value"]},
                "RefObject": {
                    "$ref": "#/$defs/Target",
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
                "RefConstraint": {"$ref": "#/$defs/Target", "minimum": 0},
                "EnumObject": {
                    "enum": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
            }
        },
    )

    analysis = schema_analysis(Database(mode="strict"), schema_path)
    assert {
        diagnostic.json_pointer
        for diagnostic in analysis.errors
        if diagnostic.code == "ambiguous-schema-combination"
    } == {
        "/$defs/EnumObject/properties",
        "/$defs/RefEnum/enum",
        "/$defs/RefObject/properties",
        "/$defs/RefObject/type",
    }
    # A validation-only keyword never competes with the selected shape: it is
    # ignored beside a $ref exactly as it is anywhere else.
    constraint = next(
        d for d in analysis.diagnostics if d.json_pointer == "/$defs/RefConstraint/minimum"
    )
    assert constraint.code == "ignored-constraint"
    assert constraint.severity is DiagnosticSeverity.WARNING


def test_documented_annotations_are_accepted_without_affecting_generation(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.test/schema",
            "$comment": "root comment",
            "title": "Root title",
            "description": "Root description",
            "$defs": {
                "Thing": {
                    "$comment": "definition comment",
                    "title": "Thing title",
                    "description": "Thing docs",
                    "type": "object",
                    "properties": {
                        "values": {
                            "$comment": "property comment",
                            "title": "Values title",
                            "description": "Values docs",
                            "type": "array",
                            "items": {
                                "$comment": "item comment",
                                "title": "Item title",
                                "description": "Item description",
                                "type": "string",
                            },
                        }
                    },
                }
            },
        },
    )
    db = Database(mode="strict")

    assert schema_analysis(db, schema_path).errors == ()
    generate(db, schema_path, out)
    assert (out / "thing.py").is_file()


# --------------------------------------------------------------------------- #
# Task 2B.3 — granularity (C2, C3)
# --------------------------------------------------------------------------- #


def test_description_only_change_rewrites_doc_not_model(tmp_path: Path) -> None:  # C2
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    schema: dict[str, Any] = {
        "$defs": {"A": {"type": "object", "properties": {"x": {"type": "integer"}}}}
    }
    _write_schema(p, schema)
    db = Database(mode="strict")
    generate(db, str(p), out)

    schema["$defs"]["A"]["description"] = "hi"
    _write_schema(p, schema)
    res = generate(db, str(p), out)

    assert res.updated == ("docs/a.md",)
    assert res.created == () and res.repaired == ()
    assert db.inspect(model_python, str(p), "A").last_decision == "reused"
    assert db.inspect(model_doc, str(p), "A").last_recompute == "executed"


def test_internal_change_rewrites_model_revalidates_referrer_only(tmp_path: Path) -> None:  # C3
    # Closure reading: a change to A re-validates A and the reference-graph
    # closure (B refs A); each is rewritten only if its bytes change. An
    # A-internal requiredness change does not alter B's bytes (B refers to A by
    # name), so B backdates; the unrelated C is reused.
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    schema: dict[str, Any] = {
        "$defs": {
            "A": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}},
            "C": {"type": "object", "properties": {"y": {"type": "string"}}},
        }
    }
    _write_schema(p, schema)
    db = Database(mode="strict")
    generate(db, str(p), out)

    schema["$defs"]["A"]["required"] = []  # x now optional
    _write_schema(p, schema)
    res = generate(db, str(p), out)

    assert set(res.updated) == {"a.py", "docs/a.md"}  # only the affected model + its doc
    assert res.created == () and res.repaired == ()
    assert db.inspect(model_python, str(p), "B").last_recompute == "backdated"  # in closure
    assert db.inspect(model_python, str(p), "C").last_decision == "reused"  # outside closure


def test_removed_ref_target_fails_without_mutating_outputs(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    schema: dict[str, Any] = {
        "$defs": {
            "A": {"type": "object", "properties": {"x": {"type": "integer"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}},
        }
    }
    _write_schema(p, schema)
    db = Database(mode="strict")
    generate(db, str(p), out)
    before = _tree(out)

    del schema["$defs"]["A"]
    _write_schema(p, schema)
    with pytest.raises(SchemaGenerationError) as caught:
        generate(db, str(p), out)

    diagnostic = next(d for d in caught.value.diagnostics if d.code == "unknown-ref")
    assert diagnostic.json_pointer == "/$defs/B/properties/a/$ref"
    assert _tree(out) == before


def test_consistent_rename_propagates_referenced_identifier_to_referrer(tmp_path: Path) -> None:
    # Interface change through a STILL-VALID graph: rename A -> Aaa and update
    # B's $ref in the same edit; U is independent (no ref to A) and untouched.
    # A consistent rename MOVES A's files (delete a.*, create aaa.*) — A is not
    # rewritten in place. The propagation claim lives on the referrer B, whose
    # ONLY byte delta is the A -> Aaa identifier at the reference site. U proves
    # the closure is selective: neither rewritten nor re-touched.
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    schema: dict[str, Any] = {
        "$defs": {
            "A": {"type": "object", "properties": {"x": {"type": "integer"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}},
            "U": {"type": "object", "properties": {"y": {"type": "string"}}},
        }
    }
    _write_schema(p, schema)
    db = Database(mode="strict")
    generate(db, str(p), out)
    before_b = (out / "b.py").read_text(encoding="utf-8")
    before_u = (out / "u.py").read_text(encoding="utf-8")
    assert "from .a import A" in before_b and "a: A | None = None" in before_b

    schema["$defs"]["Aaa"] = schema["$defs"].pop("A")  # rename the key
    schema["$defs"]["B"]["properties"]["a"]["$ref"] = "#/$defs/Aaa"  # and the referrer
    _write_schema(p, schema)
    res = generate(db, str(p), out)

    # Discriminator vs the dangling case: the graph stays fully resolved, so NO
    # diagnostic fires. If one did, we'd be back in the fallback path.
    assert "unknown-ref" not in {d.code for d in schema_analysis(db, str(p)).diagnostics}

    # Full contrast in one verdict: Aaa created, A deleted, B rewritten, U absent.
    assert set(res.created) == {"aaa.py", "docs/aaa.md"}
    assert set(res.updated) == {"b.py", "docs/b.md", "__init__.py"}
    assert res.repaired == ()
    assert set(res.deleted) == {"a.py", "docs/a.md"}

    # B rewrote because of the class-name byte-delta and ONLY that: applying the
    # A -> Aaa identifier substitution to the old bytes reproduces the new bytes.
    after_b = (out / "b.py").read_text(encoding="utf-8")
    expected_b = before_b.replace("from .a import A", "from .aaa import Aaa").replace(
        "a: A | None", "a: Aaa | None"
    )
    assert after_b == expected_b

    # U neither references A nor was renamed: absent from the write set, bytes equal.
    assert "u.py" not in res.updated and "docs/u.md" not in res.updated
    assert (out / "u.py").read_text(encoding="utf-8") == before_u


# --------------------------------------------------------------------------- #
# Task 2B.4 — add/remove (C4/C5), from-scratch (C6), sample, contract lock
# --------------------------------------------------------------------------- #


def test_add_definition_creates_its_files_plus_index(tmp_path: Path) -> None:  # C4
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    _write_schema(p, {"$defs": {"A": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, str(p), out)

    _write_schema(p, {"$defs": {"A": {"type": "object"}, "B": {"type": "object"}}})
    res = generate(db, str(p), out)
    assert set(res.created) == {"b.py", "docs/b.md"}
    assert res.updated == ("__init__.py",)
    assert res.repaired == ()
    assert res.deleted == ()


def test_adding_unrelated_definition_does_not_execute_existing_model_renderers(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {"$defs": {"A": {"type": "object"}, "C": {"type": "object"}}},
    )
    db = Database(mode="strict")
    generate(db, schema_path, out)
    existing_labels = {
        db.inspect(model_python, str(schema_path), name).label for name in ("A", "C")
    }
    db.reset_statistics()

    _write_schema(
        schema_path,
        {
            "$defs": {
                "A": {"type": "object"},
                "B": {"type": "object"},
                "C": {"type": "object"},
            }
        },
    )
    generate(db, schema_path, out)

    executed_labels = {profile.query_label for profile in db.query_profile()}
    assert existing_labels.isdisjoint(executed_labels)


def test_remove_definition_deletes_only_its_outputs(tmp_path: Path) -> None:  # C5
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    _write_schema(p, {"$defs": {"A": {"type": "object"}, "B": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, str(p), out)

    _write_schema(p, {"$defs": {"A": {"type": "object"}}})
    res = generate(db, str(p), out)
    assert set(res.deleted) == {"b.py", "docs/b.md"}
    assert res.updated == ("__init__.py",)  # index updated; A untouched
    assert res.created == () and res.repaired == ()


def test_generate_from_sample_fixture(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    db = Database(mode="strict")
    res = generate(db, _SAMPLE, out)
    assert (out / "user.py").exists()
    assert (out / "status.py").read_text(encoding="utf-8").startswith("from __future__")
    assert "Literal" in (out / "status.py").read_text(encoding="utf-8")
    assert (out / "docs" / "address.md").exists()
    assert "from .user import User" in (out / "__init__.py").read_text(encoding="utf-8")
    assert res.deleted == ()
    # Every generated module must be VALID Python, not just substring-correct.
    for py in sorted(out.rglob("*.py")):
        compile(py.read_text(encoding="utf-8"), str(py), "exec")


def test_keyword_property_name_blocks_generation(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    valid = {
        "$defs": {
            "Thing": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            }
        }
    }
    _write_schema(p, valid)
    db = Database(mode="strict")
    generate(db, str(p), out)
    before = _tree(out)

    _write_schema(
        p,
        {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "properties": {"class": {"type": "string"}, "id": {"type": "integer"}},
                    "required": ["id"],
                }
            }
        },
    )
    with pytest.raises(SchemaGenerationError) as caught:
        generate(db, str(p), out)
    diagnostic = next(d for d in caught.value.diagnostics if d.code == "unsupported-field-name")
    assert diagnostic.json_pointer == "/$defs/Thing/properties/class"
    assert _tree(out) == before


def test_reserved_dunder_property_names_block_generation(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Thing": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    reserved_names = ("__slots__", "__weakref__", "＿＿dict＿＿")
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "properties": {name: {"type": "string"} for name in reserved_names},
                }
            }
        },
    )
    analysis = schema_analysis(db, schema_path)
    diagnostics = [
        diagnostic for diagnostic in analysis.errors if diagnostic.code == "reserved-field-name"
    ]
    assert {diagnostic.json_pointer for diagnostic in diagnostics} == {
        f"/$defs/Thing/properties/{name}" for name in reserved_names
    }

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    with pytest.raises(SchemaGenerationError):
        pyinc_codegen.generate_outputs.reconcile(db, str(schema_path), root=out)
    assert _tree(out) == before


def test_non_identifier_definition_name_blocks_generation(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    _write_schema(p, {"$defs": {"Good": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, str(p), out)
    before = _tree(out)

    _write_schema(
        p,
        {
            "$defs": {
                "class": {"type": "object", "properties": {"x": {"type": "integer"}}},
                "Good": {"type": "object", "properties": {"y": {"type": "string"}}},
            }
        },
    )
    with pytest.raises(SchemaGenerationError) as caught:
        generate(db, str(p), out)
    assert "unsupported-definition-name" in {d.code for d in caught.value.diagnostics}
    assert _tree(out) == before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_codegen_incremental_byte_identical_to_fresh(mode: str, tmp_path: Path) -> None:  # C6
    p = tmp_path / "s.json"
    out_inc = tmp_path / "inc"
    inc_db = Database(mode=mode)

    base: dict[str, Any] = {
        "$defs": {
            "Color": {"type": "string", "enum": ["red", "green"]},
            "Widget": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "color": {"$ref": "#/$defs/Color"}},
                "required": ["id"],
            },
        }
    }

    def variant(step: int) -> dict[str, Any]:
        schema: dict[str, Any] = json.loads(json.dumps(base))  # deep copy; steps 0/1 unchanged
        defs = schema["$defs"]
        if step == 2:
            defs["Widget"]["description"] = "A widget."  # description-only
        elif step == 3:
            defs["Widget"]["required"] = []  # requiredness change
        elif step == 4:
            defs["Gadget"] = {"type": "object", "properties": {"n": {"type": "string"}}}  # add
        elif step == 5:
            del defs["Color"]
            del defs["Widget"]["properties"]["color"]  # consistent removal
        return schema

    for step in range(6):
        schema = variant(step)
        # whitespace varies per step to prove formatting is irrelevant
        p.write_text(json.dumps(schema, indent=(2 if step % 2 == 0 else 4)), encoding="utf-8")
        generate(inc_db, str(p), out_inc)

        out_fresh = tmp_path / "fresh"
        if out_fresh.exists():
            shutil.rmtree(out_fresh)
        fresh_db = Database(mode=mode)
        generate(fresh_db, str(p), out_fresh)

        assert _tree(out_inc) == _tree(out_fresh)


# --------------------------------------------------------------------------- #
# v3 validation boundary, portable names, and import-safe output
# --------------------------------------------------------------------------- #


def test_malformed_schema_preserves_existing_outputs(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Good": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    schema_path.write_text('{"$defs": ', encoding="utf-8")
    analysis = schema_analysis(db, schema_path)
    assert len(analysis.errors) == 1
    assert analysis.errors[0].code == "invalid-json"
    assert analysis.errors[0].json_pointer == ""

    with pytest.raises(SchemaGenerationError) as caught:
        generate(db, schema_path, out)
    assert caught.value.analysis == analysis
    assert _tree(out) == before

    # Calling the public action directly has the same validation boundary.
    with pytest.raises(SchemaGenerationError):
        pyinc_codegen.generate_outputs.reconcile(db, str(schema_path), root=out)
    assert _tree(out) == before


def test_invalid_utf8_schema_preserves_existing_outputs(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Good": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    schema_path.write_bytes(b'{"$defs":{"Bad":\xff}}')
    analysis = schema_analysis(db, schema_path)
    assert len(analysis.errors) == 1
    diagnostic = analysis.errors[0]
    assert diagnostic.code == "invalid-json"
    assert diagnostic.json_pointer == ""
    assert "not valid UTF-8" in diagnostic.message

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    with pytest.raises(SchemaGenerationError):
        pyinc_codegen.generate_outputs.reconcile(db, str(schema_path), root=out)
    assert _tree(out) == before


@pytest.mark.parametrize(
    ("raw_schema", "message_fragment"),
    [
        pytest.param("1e999", "non-finite JSON number", id="overflow-number"),
        pytest.param("NaN", "non-finite JSON number", id="nan-constant"),
        pytest.param("Infinity", "non-finite JSON number", id="infinity-constant"),
        pytest.param("-Infinity", "non-finite JSON number", id="negative-infinity-constant"),
        pytest.param(r'"\ud800"', "unpaired surrogate", id="surrogate-value"),
        pytest.param(r'{"\udfff": 0}', "unpaired surrogate", id="surrogate-key"),
        pytest.param(
            '{"$defs":{"Thing":' + "[" * 300 + "null" + "]" * 300 + "}}",
            "JSON nesting exceeds",
            id="excessive-nesting",
        ),
    ],
)
def test_unsafe_json_preserves_existing_outputs(
    raw_schema: str,
    message_fragment: str,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Good": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    schema_path.write_text(raw_schema, encoding="utf-8")
    analysis = schema_analysis(db, schema_path)
    assert len(analysis.errors) == 1
    diagnostic = analysis.errors[0]
    assert diagnostic.code == "invalid-json"
    assert diagnostic.json_pointer == ""
    assert message_fragment in diagnostic.message

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    with pytest.raises(SchemaGenerationError):
        pyinc_codegen.generate_outputs.reconcile(db, str(schema_path), root=out)
    assert _tree(out) == before


def test_root_unsupported_construct_preserves_existing_outputs(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Old": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    _write_schema(
        schema_path,
        {"oneOf": [{"type": "string"}, {"type": "integer"}]},
    )
    analysis = schema_analysis(db, schema_path)
    diagnostic = next(d for d in analysis.errors if d.code == "unsupported-construct")
    assert diagnostic.json_pointer == "/oneOf"

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    assert _tree(out) == before


def test_nested_ignored_keyword_generates_and_records_its_warning(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Old": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)

    _write_schema(
        schema_path,
        {
            "$defs": {
                "Replacement": {
                    "type": "object",
                    "properties": {
                        "identifiers": {
                            "type": "array",
                            "items": {"type": "string", "format": "uuid"},
                        }
                    },
                }
            }
        },
    )
    analysis = schema_analysis(db, schema_path)
    diagnostic = next(
        item
        for item in analysis.diagnostics
        if item.json_pointer == "/$defs/Replacement/properties/identifiers/items/format"
    )
    assert diagnostic.code == "ignored-constraint"
    assert analysis.errors == ()

    result = generate(db, schema_path, out)
    assert set(result.deleted) == {"old.py", "docs/old.md"}
    assert "identifiers: list[str] | None = None" in (out / "replacement.py").read_text(
        encoding="utf-8"
    )
    # The ignored constraint is recorded where a reader of the model will see it.
    assert "ignored-constraint" in (out / "docs" / "replacement.md").read_text(encoding="utf-8")


def test_nested_unsupported_keyword_fails_before_reconciliation(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Old": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    _write_schema(
        schema_path,
        {
            "$defs": {
                "Replacement": {
                    "type": "object",
                    "properties": {
                        "identifiers": {
                            "type": "array",
                            "items": {"patternProperties": {"^x": {"type": "string"}}},
                        }
                    },
                }
            }
        },
    )
    analysis = schema_analysis(db, schema_path)
    diagnostic = next(
        item
        for item in analysis.errors
        if item.json_pointer == "/$defs/Replacement/properties/identifiers/items/patternProperties"
    )
    assert diagnostic.code == "unsupported-construct"

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    assert _tree(out) == before

    with pytest.raises(SchemaGenerationError):
        pyinc_codegen.generate_outputs.reconcile(db, str(schema_path), root=out)
    assert _tree(out) == before


def test_root_model_schema_preserves_existing_outputs(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Old": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    _write_schema(
        schema_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Value holder",
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
    )
    analysis = schema_analysis(db, schema_path)
    # One diagnostic that states the rule, not one per root keyword.
    root_errors = [
        diagnostic for diagnostic in analysis.errors if diagnostic.code == "unsupported-root-schema"
    ]
    assert len(root_errors) == 1
    assert root_errors[0].json_pointer == ""
    assert "$defs" in root_errors[0].message
    assert all(
        keyword in root_errors[0].message for keyword in ("'properties'", "'required'", "'type'")
    )

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    assert _tree(out) == before


def test_root_annotation_keywords_are_ignored_without_blocking(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "examples": [{"name": "example"}],
            "$defs": {"Thing": {"type": "object", "properties": {"name": {"type": "string"}}}},
        },
    )
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert analysis.errors == ()
    diagnostic = next(d for d in analysis.diagnostics if d.code == "ignored-constraint")
    assert diagnostic.json_pointer == "/examples"

    generate(db, schema_path, out)
    assert (out / "thing.py").is_file()


@pytest.mark.parametrize(
    ("definition_name", "code"),
    [
        ("Class", "invalid-module-name"),
        ("CON", "nonportable-module-name"),
        ("A" * 300, "nonportable-module-name"),
    ],
)
def test_invalid_or_nonportable_module_stem_preserves_existing_outputs(
    definition_name: str,
    code: str,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Good": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, schema_path, out)
    before = _tree(out)

    _write_schema(schema_path, {"$defs": {definition_name: {"type": "object"}}})
    analysis = schema_analysis(db, schema_path)
    diagnostic = next(d for d in analysis.errors if d.code == code)
    assert diagnostic.json_pointer == f"/$defs/{definition_name}"

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    assert _tree(out) == before


def test_optional_ref_name_containing_none_is_still_nullable(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "NoneValue": {"type": "object"},
                "Holder": {
                    "type": "object",
                    "properties": {"value": {"$ref": "#/$defs/NoneValue"}},
                },
            }
        },
    )

    generate(Database(mode="strict"), schema_path, out)
    source = (out / "holder.py").read_text(encoding="utf-8")
    assert "value: NoneValue | None = None" in source


@pytest.mark.parametrize(
    "names",
    [
        ("HTTPServer", "http_server"),
        ("User", "user"),
        ("Caf\N{LATIN SMALL LETTER E WITH ACUTE}", "Cafe\N{COMBINING ACUTE ACCENT}"),
    ],
)
def test_portable_module_name_collisions_are_errors(names: tuple[str, str], tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {"$defs": {name: {"type": "object"} for name in names}},
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    diagnostics = [d for d in analysis.errors if d.code == "module-name-collision"]
    assert len(diagnostics) == 2
    assert {d.json_pointer for d in diagnostics} == {
        f"/$defs/{name.replace('~', '~0').replace('/', '~1')}" for name in names
    }


def test_reserved_package_index_name_is_an_error(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path, {"$defs": {"__INIT__": {"type": "object"}}})
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    diagnostic = next(d for d in analysis.errors if d.code == "reserved-module-name")
    assert diagnostic.json_pointer == "/$defs/__INIT__"


def test_json_pointer_escapes_invalid_property_names(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "properties": {"bad/name~here": {"type": "string"}},
                }
            }
        },
    )
    analysis = schema_analysis(Database(mode="strict"), schema_path)
    diagnostic = next(d for d in analysis.errors if d.code == "unsupported-field-name")
    assert diagnostic.json_pointer == "/$defs/Thing/properties/bad~1name~0here"


def test_unconstrained_schema_uses_explicit_warning_policy(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    out = tmp_path / "generated"
    _write_schema(schema_path, {"$defs": {"Anything": {}}})
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    warning = next(d for d in analysis.diagnostics if d.code == "unconstrained-schema")
    assert warning.severity is DiagnosticSeverity.WARNING
    assert analysis.errors == ()

    generate(db, schema_path, out)
    source = (out / "anything.py").read_text(encoding="utf-8")
    assert "Anything: TypeAlias = 'object'" in source
    compile(source, "anything.py", "exec")


def test_empty_and_invalid_enums_are_errors_but_render_valid_python(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Empty": {"type": "string", "enum": []},
                "FloatValue": {"type": "number", "enum": [1.5]},
            }
        },
    )
    db = Database(mode="strict")
    analysis = schema_analysis(db, schema_path)
    assert {d.code for d in analysis.errors} >= {"empty-enum", "unsupported-enum-value"}
    assert next(d for d in analysis.errors if d.code == "empty-enum").json_pointer == (
        "/$defs/Empty/enum"
    )
    compile(model_python(db, str(schema_path), "Empty"), "empty.py", "exec")
    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, tmp_path / "generated")


def test_mutually_recursive_models_and_aliases_import_on_python_311_plus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_path = tmp_path / "schema.json"
    package = tmp_path / "cycle_models"
    _write_schema(
        schema_path,
        {
            "$defs": {
                "Left": {
                    "type": "object",
                    "properties": {"right": {"$ref": "#/$defs/Right"}},
                },
                "Right": {
                    "type": "object",
                    "properties": {"left": {"$ref": "#/$defs/Left"}},
                },
                "FirstAlias": {"$ref": "#/$defs/SecondAlias"},
                "SecondAlias": {"$ref": "#/$defs/FirstAlias"},
                "LeftList": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Left"},
                },
            }
        },
    )
    generate(Database(mode="strict"), schema_path, package)

    left_source = (package / "left.py").read_text(encoding="utf-8")
    assert "if TYPE_CHECKING:" in left_source
    assert "from .right import Right" in left_source
    alias_source = (package / "first_alias.py").read_text(encoding="utf-8")
    assert "FirstAlias: TypeAlias = 'SecondAlias'" in alias_source

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    try:
        generated = importlib.import_module("cycle_models")
        assert generated.Left.__name__ == "Left"
        assert generated.Right.__name__ == "Right"
        assert generated.FirstAlias == "SecondAlias"
        assert generated.SecondAlias == "FirstAlias"
        assert generated.LeftList == "list[Left]"
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "cycle_models" or module_name.startswith("cycle_models."):
                sys.modules.pop(module_name, None)


def test_codegen_exports_only_stable_api() -> None:
    assert set(pyinc_codegen.__all__) == {
        "Diagnostic",
        "DiagnosticSeverity",
        "FieldModel",
        "SchemaAnalysis",
        "SchemaGenerationError",
        "SchemaModel",
        "generate",
        "generate_outputs",
        "schema_analysis",
    }
    assert hasattr(pyinc_codegen, "generate")
    assert hasattr(pyinc_codegen, "schema_analysis")
    # Experimental query helpers stay module-local.
    assert not hasattr(pyinc_codegen, "schema_text")
    assert not hasattr(pyinc_codegen, "model_python")
    assert not hasattr(pyinc_codegen, "definition_names")
