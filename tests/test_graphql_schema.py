from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

from pyinc import Database
from pyinc.actions import ActionManifest
from pyinc.integrations import graphql_schema as gql

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "graphql" / "schema.json"

EXPECTED_FILES = {
    "models/__init__.py",
    "models/DateTime.py",
    "models/Mutation.py",
    "models/Node.py",
    "models/Post.py",
    "models/Query.py",
    "models/Role.py",
    "models/SearchResult.py",
    "models/User.py",
    "models/UserFilter.py",
    "operations/createUser.py",
    "operations/node.py",
    "operations/search.py",
    "operations/users.py",
    "docs/index.md",
    "docs/types/Boolean.md",
    "docs/types/DateTime.md",
    "docs/types/ID.md",
    "docs/types/Mutation.md",
    "docs/types/Node.md",
    "docs/types/Post.md",
    "docs/types/Query.md",
    "docs/types/Role.md",
    "docs/types/SearchResult.md",
    "docs/types/String.md",
    "docs/types/User.md",
    "docs/types/UserFilter.md",
}


def _doc() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(FIXTURE.read_text()))


def _write(obj: dict[str, Any], path: Path, *, indent: int = 2) -> None:
    path.write_text(json.dumps(obj, indent=indent) + "\n")


def _types(obj: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", obj["data"]["__schema"]["types"])


def _find(obj: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in _types(obj) if t.get("name") == name)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def _generate(db: Database, schema: Path, out: Path, state: Path) -> Any:
    return gql.generate_graphql(db, schema, out, state_dir=state)


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    schema = tmp_path / "schema.json"
    _write(_doc(), schema)
    return schema, tmp_path / "out", tmp_path / "state"


# ---------------------------------------------------------------------------
# Schema model
# ---------------------------------------------------------------------------


def test_schema_model_resolves_kinds_and_wrappers() -> None:
    db = Database()
    schema = gql.graphql_analysis(db, FIXTURE)
    assert schema.query_type == "Query"
    assert schema.mutation_type == "Mutation"
    assert schema.diagnostics == ()
    user = next(t for t in schema.types if t.name == "User")
    assert user.kind == "OBJECT"
    assert user.interfaces == ("Node",)
    posts = next(f for f in user.fields if f.name == "posts")
    assert posts.signature == "[Post!]!"  # LIST + NON_NULL wrappers normalized
    created = next(f for f in user.fields if f.name == "createdAt")
    assert created.signature == "DateTime"  # nullable custom scalar
    role = next(t for t in schema.types if t.name == "Role")
    assert tuple(v.name for v in role.enum_values) == ("ADMIN", "MEMBER")


# ---------------------------------------------------------------------------
# Generation + incremental behavior
# ---------------------------------------------------------------------------


def test_cold_generation_produces_expected_files(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    result = _generate(Database(), schema, out, state)
    assert set(_tree(out)) == EXPECTED_FILES
    assert result.deletions == ()


def test_generated_python_parses(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    _generate(Database(), schema, out, state)
    for path in out.rglob("*.py"):
        ast.parse(path.read_text(), filename=str(path))


def test_identical_rerun_zero_writes(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    db = Database()
    _generate(db, schema, out, state)
    result = _generate(db, schema, out, state)
    assert result.writes == ()
    assert result.unchanged == len(EXPECTED_FILES)


def test_whitespace_and_key_order_edit_zero_writes(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    db = Database()
    _generate(db, schema, out, state)
    # Re-serialize with different indentation (whitespace) and key order.
    reordered = json.loads(json.dumps(_doc()))
    schema.write_text(json.dumps(reordered, indent=8, sort_keys=True) + "\n")
    result = _generate(db, schema, out, state)
    assert result.writes == ()


def test_description_only_edit_regenerates_only_that_doc(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    db = Database()
    _generate(db, schema, out, state)
    mod = _doc()
    _find(mod, "User")["description"] = "A completely rewritten description."
    _write(mod, schema)
    result = _generate(db, schema, out, state)
    assert result.writes == ("docs/types/User.md",)
    assert result.deletions == ()


def test_field_signature_edit_regenerates_only_dependent_code_and_doc(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    db = Database()
    _generate(db, schema, out, state)
    mod = _doc()
    name_field = next(f for f in _find(mod, "User")["fields"] if f["name"] == "name")
    name_field["type"] = name_field["type"]["ofType"]  # String! -> String
    _write(mod, schema)
    result = _generate(db, schema, out, state)
    assert set(result.writes) == {"models/User.py", "docs/types/User.md"}


def test_type_removal_deletes_only_owned_artifacts(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    db = Database()
    _generate(db, schema, out, state)
    mod = _doc()
    mod["data"]["__schema"]["types"] = [t for t in _types(mod) if t.get("name") != "Post"]
    _write(mod, schema)
    result = _generate(db, schema, out, state)
    assert set(result.deletions) == {"models/Post.py", "docs/types/Post.md"}
    assert not (out / "models/Post.py").exists()
    # Aggregates legitimately rewrite (Post removed from index/union).
    assert "models/__init__.py" in result.writes
    assert "docs/index.md" in result.writes


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_malformed_json_returns_deterministic_diagnostic(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{not valid json")
    diags = gql.graphql_analysis(Database(), schema).diagnostics
    assert len(diags) == 1
    assert diags[0].code == "json-decode-error"


def test_missing_schema_returns_diagnostic(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"data": {}}))
    diags = gql.graphql_analysis(Database(), schema).diagnostics
    assert [d.code for d in diags] == ["missing-schema"]


def test_unsupported_kind_returns_diagnostic(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    mod = _doc()
    _types(mod).append({"kind": "WEIRD_KIND", "name": "Mystery"})
    schema.write_text(json.dumps(mod))
    diags = gql.graphql_analysis(Database(), schema).diagnostics
    assert any(d.code == "unsupported-kind" and "Mystery" in d.message for d in diags)


# ---------------------------------------------------------------------------
# From-scratch consistency over an edit sequence
# ---------------------------------------------------------------------------


def test_incremental_matches_from_scratch_over_edit_sequence(tmp_path: Path) -> None:
    schema, out, state = _setup(tmp_path)
    db = Database()
    _generate(db, schema, out, state)

    def edit_description(obj: dict[str, Any]) -> None:
        _find(obj, "Post")["description"] = "Edited post description."

    def edit_signature(obj: dict[str, Any]) -> None:
        f = next(x for x in _find(obj, "Post")["fields"] if x["name"] == "title")
        f["type"] = f["type"]["ofType"]

    def add_type(obj: dict[str, Any]) -> None:
        _types(obj).append(
            {
                "kind": "ENUM",
                "name": "Status",
                "description": "A status.",
                "fields": None,
                "inputFields": None,
                "interfaces": None,
                "enumValues": [
                    {"name": "OPEN", "description": None, "isDeprecated": False, "deprecationReason": None},
                ],
                "possibleTypes": None,
            }
        )

    def remove_type(obj: dict[str, Any]) -> None:
        obj["data"]["__schema"]["types"] = [t for t in _types(obj) if t.get("name") != "UserFilter"]

    state_obj = _doc()
    for i, edit in enumerate([edit_description, edit_signature, add_type, remove_type]):
        edit(state_obj)
        _write(state_obj, schema)
        _generate(db, schema, out, state)

        fresh_out = tmp_path / f"fresh_{i}"
        fresh_state = tmp_path / f"fresh_state_{i}"
        fresh_schema = tmp_path / f"fresh_schema_{i}.json"
        _write(state_obj, fresh_schema)
        _generate(Database(), fresh_schema, fresh_out, fresh_state)

        assert _tree(out) == _tree(fresh_out)
        inc_manifest = ActionManifest.from_json_bytes((state / "manifest.json").read_bytes())
        fresh_manifest = ActionManifest.from_json_bytes((fresh_state / "manifest.json").read_bytes())
        assert inc_manifest.owned_paths == fresh_manifest.owned_paths


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_graphql_schema_all_is_exact() -> None:
    assert set(gql.__all__) == {
        "GraphQLArgument",
        "GraphQLDiagnostic",
        "GraphQLEnumValue",
        "GraphQLField",
        "GraphQLSchema",
        "GraphQLType",
        "generate_graphql",
        "graphql_analysis",
        "graphql_artifacts",
    }


def test_payload_queries_not_reexported_from_integrations() -> None:
    from pyinc import integrations

    for experimental in ("artifacts_payload", "code_model", "doc_model", "schema_text"):
        assert experimental not in integrations.__all__
