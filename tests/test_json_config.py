from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.json_config import (
    JsonAnalysis,
    json_analysis,
    workspace_json_analysis,
)

Operation = tuple[Literal["write", "delete"], str, str | None]

_MINIMAL_JSON = json.dumps(
    {
        "name": "example",
        "version": "0.1.0",
        "description": "An example package",
        "scripts": {
            "build": "tsc",
            "test": "jest",
        },
        "dependencies": {
            "express": "^4.18.0",
            "lodash": "^4.17.21",
        },
        "devDependencies": {
            "typescript": "^5.0.0",
        },
        "config": {
            "port": 3000,
            "debug": False,
        },
    },
    indent=2,
)


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_json_config_stable_api() -> None:
    assert "JsonAnalysis" in integrations.__all__
    assert "JsonKey" in integrations.__all__
    assert "JsonSection" in integrations.__all__
    assert "json_analysis" in integrations.__all__
    assert "workspace_json_analysis" in integrations.__all__
    assert hasattr(integrations, "json_analysis")
    assert hasattr(integrations, "workspace_json_analysis")
    assert hasattr(integrations, "JsonAnalysis")
    # Experimental helpers must not leak.
    assert not hasattr(integrations, "json_file_text")
    assert not hasattr(integrations, "json_sections_payload")
    assert not hasattr(integrations, "json_analysis_payload")
    assert not hasattr(integrations, "json_diagnostics_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_json_analysis_extracts_sections_and_keys(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "package.json"
    path.write_text(_MINIMAL_JSON, encoding="utf-8")

    db = Database(mode=mode)
    result = json_analysis(db, str(path))

    assert isinstance(result, JsonAnalysis)
    assert result.path == str(path)

    section_names = {s.name for s in result.sections}
    assert "<root>" in section_names
    assert "scripts" in section_names
    assert "dependencies" in section_names
    assert "devDependencies" in section_names
    assert "config" in section_names

    root_section = next(s for s in result.sections if s.name == "<root>")
    root_key_names = {k.key for k in root_section.keys}
    assert "name" in root_key_names
    assert "version" in root_key_names
    assert "description" in root_key_names

    name_key = next(k for k in root_section.keys if k.key == "name")
    assert name_key.value_type == "string"
    assert name_key.string_value == "'example'"

    assert result.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_json_analysis_reports_syntax_errors(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{"key": }', encoding="utf-8")

    db = Database(mode=mode)
    result = json_analysis(db, str(path))

    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "json-decode-error"
    assert result.sections == ()


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


def test_json_analysis_handles_empty_object(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("{}", encoding="utf-8")

    db = Database()
    result = json_analysis(db, str(path))

    assert len(result.sections) == 1
    assert result.sections[0].name == "<root>"
    assert result.sections[0].keys == ()
    assert result.sections[0].subsections == ()
    assert result.diagnostics == ()


def test_json_analysis_handles_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")

    db = Database()
    result = json_analysis(db, str(path))

    # Empty string is not valid JSON but is treated as empty input (no diagnostics).
    assert result.sections == ()
    assert result.diagnostics == ()


def test_json_analysis_handles_nested_objects(tmp_path: Path) -> None:
    data = {
        "level1": {
            "level2": {
                "key": "value",
            },
            "sibling": 42,
        },
    }
    path = tmp_path / "nested.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    db = Database()
    result = json_analysis(db, str(path))

    section_names = {s.name for s in result.sections}
    assert "<root>" in section_names
    assert "level1" in section_names
    assert "level1.level2" in section_names

    level1 = next(s for s in result.sections if s.name == "level1")
    assert "level1.level2" in level1.subsections
    sibling_key = next(k for k in level1.keys if k.key == "sibling")
    assert sibling_key.value_type == "number"


def test_json_analysis_handles_non_object_top_level(tmp_path: Path) -> None:
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    db = Database()
    result = json_analysis(db, str(path))

    # Non-object top-level produces no sections (not walkable as key/value).
    assert result.sections == ()
    assert result.diagnostics == ()


def test_json_analysis_value_types(tmp_path: Path) -> None:
    data = {
        "str_val": "hello",
        "num_int": 42,
        "num_float": 3.14,
        "bool_val": True,
        "null_val": None,
        "arr_val": [1, 2, 3],
    }
    path = tmp_path / "types.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    db = Database()
    result = json_analysis(db, str(path))

    root = next(s for s in result.sections if s.name == "<root>")
    type_map = {k.key: k.value_type for k in root.keys}
    assert type_map["str_val"] == "string"
    assert type_map["num_int"] == "number"
    assert type_map["num_float"] == "number"
    assert type_map["bool_val"] == "boolean"
    assert type_map["null_val"] == "null"
    assert type_map["arr_val"] == "array"


# ---------------------------------------------------------------------------
# Cutoff / backdating
# ---------------------------------------------------------------------------


def test_whitespace_only_edit_backdates_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(_MINIMAL_JSON, encoding="utf-8")

    db = Database()
    first = json_analysis(db, str(path))

    # Reformat with different indentation — semantically identical.
    parsed = json.loads(_MINIMAL_JSON)
    path.write_text(json.dumps(parsed, indent=4), encoding="utf-8")
    second = json_analysis(db, str(path))

    assert first == second


def test_semantic_edit_invalidates_downstream(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(_MINIMAL_JSON, encoding="utf-8")

    db = Database()
    first = json_analysis(db, str(path))

    # Change a value — semantic edit.
    parsed = json.loads(_MINIMAL_JSON)
    parsed["version"] = "1.0.0"
    path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    second = json_analysis(db, str(path))

    assert first != second


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_workspace_json_analysis_discovers_package_json(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    config = root / "package.json"
    config.write_text(_MINIMAL_JSON, encoding="utf-8")

    db = Database()
    result = workspace_json_analysis(db, str(root))
    assert result is not None
    assert isinstance(result, JsonAnalysis)

    section_names = {s.name for s in result.sections}
    assert "dependencies" in section_names


def test_workspace_json_analysis_custom_filename(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    config = root / "tsconfig.json"
    config.write_text('{"compilerOptions": {"strict": true}}', encoding="utf-8")

    db = Database()
    result = workspace_json_analysis(db, str(root), filename="tsconfig.json")
    assert result is not None
    assert isinstance(result, JsonAnalysis)

    section_names = {s.name for s in result.sections}
    assert "compilerOptions" in section_names


def test_json_analysis_on_nonexistent_file(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.json"

    db = Database()
    result = json_analysis(db, str(path))

    # Missing file reads as empty string — no sections, no diagnostics.
    assert result.sections == ()
    assert result.diagnostics == ()


def test_workspace_json_analysis_returns_none_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    db = Database()
    result = workspace_json_analysis(db, str(root))
    assert result is None


# ---------------------------------------------------------------------------
# From-scratch oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_json_analysis_matches_fresh_recomputation_over_changes(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    base = json.loads(_MINIMAL_JSON)

    modified_version = {**base, "version": "1.0.0"}
    added_key = {**base, "license": "MIT"}
    changed_dep = {**base, "dependencies": {**base["dependencies"], "axios": "^1.0.0"}}
    removed_key = {k: v for k, v in base.items() if k != "description"}

    steps: tuple[tuple[str, str], ...] = (
        ("initial", json.dumps(base, indent=2)),
        ("reformat", json.dumps(base, indent=4)),
        ("change version", json.dumps(modified_version, indent=2)),
        ("add key", json.dumps(added_key, indent=2)),
        ("change dep", json.dumps(changed_dep, indent=2)),
        ("remove key", json.dumps(removed_key, indent=2)),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert json_analysis(incremental, str(path)) == json_analysis(fresh, str(path))
