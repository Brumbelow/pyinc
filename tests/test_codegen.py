from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import pyinc_codegen
from pyinc import Database
from pyinc_codegen import generate, schema_analysis
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
        if p.is_file()
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
    assert res.written == () and res.deleted == ()
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
    assert "unsupported-combinator" in codes


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

    assert res.written == ("docs/a.md",)
    assert db.inspect(model_python, str(p), "A").last_recompute == "backdated"
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

    assert set(res.written) == {"a.py", "docs/a.md"}  # only the affected model + its doc
    assert db.inspect(model_python, str(p), "B").last_recompute == "backdated"  # in closure
    assert db.inspect(model_python, str(p), "C").last_decision == "reused"  # outside closure


def test_removed_ref_target_falls_back_to_object_and_rewrites_referrer(tmp_path: Path) -> None:
    # Interface change via *removal*. B's $ref dangles, so B's field annotation
    # falls back to `object` and B gains an unknown-ref diagnostic. B is
    # rewritten in place for THAT reason — the fallback, not identifier
    # propagation. ("falls_back" in the name stops a future reader collapsing
    # this into the consistent-rename sibling.)
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
    assert "a: A | None = None" in (out / "b.py").read_text(encoding="utf-8")

    del schema["$defs"]["A"]
    _write_schema(p, schema)
    res = generate(db, str(p), out)

    # Referrer rewritten in place (same path) with the FALLBACK annotation — the
    # reason asserted on the emitted bytes, not just membership in `written`.
    assert "b.py" in res.written
    b_src = (out / "b.py").read_text(encoding="utf-8")
    assert "a: object | None = None" in b_src  # fallback type
    assert "import A" not in b_src  # the referenced class name is gone
    # The dangling $ref surfaces a diagnostic — this is *why* it rewrote.
    assert "unknown-ref" in {d.code for d in schema_analysis(db, str(p)).diagnostics}
    # The removed target's own files are the orphan-deletes; the index drops it.
    assert set(res.deleted) == {"a.py", "docs/a.md"}
    assert "__init__.py" in res.written


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
    assert set(res.written) == {"aaa.py", "docs/aaa.md", "b.py", "docs/b.md", "__init__.py"}
    assert set(res.deleted) == {"a.py", "docs/a.md"}

    # B rewrote because of the class-name byte-delta and ONLY that: applying the
    # A -> Aaa identifier substitution to the old bytes reproduces the new bytes.
    after_b = (out / "b.py").read_text(encoding="utf-8")
    expected_b = before_b.replace("from .a import A", "from .aaa import Aaa").replace(
        "a: A | None", "a: Aaa | None"
    )
    assert after_b == expected_b

    # U neither references A nor was renamed: absent from the write set, bytes equal.
    assert "u.py" not in res.written and "docs/u.md" not in res.written
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
    assert set(res.written) == {"b.py", "docs/b.md", "__init__.py"}
    assert res.deleted == ()


def test_remove_definition_deletes_only_its_outputs(tmp_path: Path) -> None:  # C5
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    _write_schema(p, {"$defs": {"A": {"type": "object"}, "B": {"type": "object"}}})
    db = Database(mode="strict")
    generate(db, str(p), out)

    _write_schema(p, {"$defs": {"A": {"type": "object"}}})
    res = generate(db, str(p), out)
    assert set(res.deleted) == {"b.py", "docs/b.md"}
    assert res.written == ("__init__.py",)  # index updated; A untouched


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


def test_keyword_property_name_is_diagnosed_not_emitted(tmp_path: Path) -> None:  # #1 field name
    # A property named after a Python keyword would emit `class: str` (invalid).
    # It is dropped with a diagnostic so the model stays compilable.
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
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
    db = Database(mode="strict")
    generate(db, str(p), out)
    src = (out / "thing.py").read_text(encoding="utf-8")
    compile(src, "thing.py", "exec")  # must be valid Python
    assert "class: str" not in src  # the keyword field is omitted...
    assert "id: int" in src  # ...the valid field remains
    assert "unsupported-field-name" in {d.code for d in schema_analysis(db, str(p)).diagnostics}


def test_non_identifier_definition_name_emits_valid_placeholder(tmp_path: Path) -> None:  # #1 def name
    # A definition whose name is not a valid identifier can't become `class <name>:`.
    # It renders a valid placeholder module, is excluded from the index, and is
    # diagnosed; valid siblings are unaffected.
    p = tmp_path / "s.json"
    out = tmp_path / "gen"
    _write_schema(
        p,
        {
            "$defs": {
                "class": {"type": "object", "properties": {"x": {"type": "integer"}}},
                "Good": {"type": "object", "properties": {"y": {"type": "string"}}},
            }
        },
    )
    db = Database(mode="strict")
    generate(db, str(p), out)
    placeholder = (out / "class.py").read_text(encoding="utf-8")
    compile(placeholder, "class.py", "exec")  # placeholder is valid Python
    assert "class class:" not in placeholder  # no invalid class statement
    init_src = (out / "__init__.py").read_text(encoding="utf-8")
    assert "import class" not in init_src  # excluded from the aggregate index
    assert "from .good import Good" in init_src  # the valid sibling is indexed
    assert "unsupported-definition-name" in {d.code for d in schema_analysis(db, str(p)).diagnostics}


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
            del defs["Color"]  # remove (Widget.color now dangles)
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


def test_codegen_exports_only_stable_api() -> None:
    assert set(pyinc_codegen.__all__) == {
        "Diagnostic",
        "FieldModel",
        "SchemaAnalysis",
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
