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
    _write_schema(p, {"$defs": {"A": {"type": "object", "properties": {"x": {"allOf": [{}]}}}}})
    db = Database(mode="strict")
    analysis = schema_analysis(db, str(p))
    codes = {d.code for d in analysis.diagnostics}
    assert "unsupported-construct" in codes
    diagnostic = next(d for d in analysis.diagnostics if d.code == "unsupported-construct")
    assert diagnostic.severity is DiagnosticSeverity.ERROR
    assert diagnostic.json_pointer == "/$defs/A/properties/x/allOf"


def test_unsupported_keywords_are_rejected_in_each_supported_context(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
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

    errors = schema_analysis(Database(mode="strict"), schema_path).errors
    unsupported = {
        diagnostic.json_pointer: diagnostic.code
        for diagnostic in errors
        if diagnostic.code == "unsupported-construct"
    }
    assert unsupported == {
        "/$defs/Thing/additionalProperties": "unsupported-construct",
        "/$defs/Thing/properties/code/format": "unsupported-construct",
        "/$defs/Thing/properties/count/minimum": "unsupported-construct",
        "/$defs/Thing/properties/tags/minItems": "unsupported-construct",
    }


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

    errors = schema_analysis(Database(mode="strict"), schema_path).errors
    assert {
        diagnostic.json_pointer
        for diagnostic in errors
        if diagnostic.code == "unsupported-construct"
    } == {
        "/$defs/Nested/properties/values/items/minItems",
        "/$defs/Nested/properties/values/items/items/format",
    }


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

    errors = schema_analysis(Database(mode="strict"), schema_path).errors
    assert {
        diagnostic.json_pointer
        for diagnostic in errors
        if diagnostic.code == "ambiguous-schema-combination"
    } == {
        "/$defs/EnumObject/properties",
        "/$defs/RefConstraint/minimum",
        "/$defs/RefEnum/enum",
        "/$defs/RefObject/properties",
        "/$defs/RefObject/type",
    }


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
        for item in analysis.errors
        if item.json_pointer == "/$defs/Replacement/properties/identifiers/items/format"
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
        {"type": "object", "properties": {"value": {"type": "string"}}},
    )
    analysis = schema_analysis(db, schema_path)
    pointers = {
        diagnostic.json_pointer
        for diagnostic in analysis.errors
        if diagnostic.code == "unsupported-root-schema"
    }
    assert pointers == {"/properties", "/type"}

    with pytest.raises(SchemaGenerationError):
        generate(db, schema_path, out)
    assert _tree(out) == before


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
