from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.toml_config import (
    ConfigAnalysis,
    config_analysis,
    workspace_config_analysis,
)

Operation = tuple[Literal["write", "delete"], str, str | None]

_MINIMAL_TOML = """\
[project]
name = "example"
version = "0.1.0"
dependencies = ["requests>=2.0", "click"]

[project.optional-dependencies]
dev = ["pytest", "mypy"]

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F"]
"""


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_toml_config_stable_api() -> None:
    assert "ConfigAnalysis" in integrations.__all__
    assert "ConfigKey" in integrations.__all__
    assert "ConfigSection" in integrations.__all__
    assert "config_analysis" in integrations.__all__
    assert "workspace_config_analysis" in integrations.__all__
    assert hasattr(integrations, "config_analysis")
    assert hasattr(integrations, "workspace_config_analysis")
    assert hasattr(integrations, "ConfigAnalysis")
    # Experimental helpers must not leak.
    assert not hasattr(integrations, "config_file_text")
    assert not hasattr(integrations, "config_sections_payload")
    assert not hasattr(integrations, "config_analysis_payload")
    assert not hasattr(integrations, "config_dependencies_payload")
    assert not hasattr(integrations, "config_tool_configs_payload")
    assert not hasattr(integrations, "config_diagnostics_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_config_analysis_extracts_sections_and_keys(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_MINIMAL_TOML, encoding="utf-8")

    db = Database(mode=mode)
    result = config_analysis(db, str(path))

    assert isinstance(result, ConfigAnalysis)
    assert result.path == str(path)

    section_names = {s.name for s in result.sections}
    assert "project" in section_names
    assert "tool.ruff" in section_names
    assert "tool.ruff.lint" in section_names

    assert "requests>=2.0" in result.dependencies
    assert "click" in result.dependencies
    assert "ruff" in result.tool_configs
    assert result.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_config_analysis_reports_syntax_errors(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("[project\nname = ", encoding="utf-8")

    db = Database(mode=mode)
    result = config_analysis(db, str(path))

    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "toml-decode-error"
    assert result.sections == ()


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


def test_config_analysis_handles_optional_dependencies(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_MINIMAL_TOML, encoding="utf-8")

    db = Database()
    result = config_analysis(db, str(path))

    assert len(result.optional_dependency_groups) == 1
    group_name, group_deps = result.optional_dependency_groups[0]
    assert group_name == "dev"
    assert "pytest" in group_deps
    assert "mypy" in group_deps


def test_config_analysis_handles_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("", encoding="utf-8")

    db = Database()
    result = config_analysis(db, str(path))

    # Empty TOML is valid — produces a root section with no keys.
    assert len(result.sections) == 1
    assert result.sections[0].name == "<root>"
    assert result.sections[0].keys == ()
    assert result.dependencies == ()
    assert result.diagnostics == ()


def test_config_analysis_extracts_tool_configs(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n",
        encoding="utf-8",
    )

    db = Database()
    result = config_analysis(db, str(path))

    assert "ruff" in result.tool_configs
    assert "mypy" in result.tool_configs


def test_config_analysis_supports_toml_datetime_values(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("released = 1979-05-27T07:32:00Z\n", encoding="utf-8")

    result = config_analysis(Database(mode="strict"), path)

    assert result.diagnostics == ()
    released = result.sections[0].keys[0]
    assert released.key == "released"
    assert released.value_type == "datetime"
    assert released.string_value.startswith("1979-05-27T07:32:00")


def test_config_analysis_reports_syntactically_valid_wrong_shapes(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('project = "wrong"\ntool = ["wrong"]\n', encoding="utf-8")

    result = config_analysis(Database(mode="strict"), path)

    assert {code for code, _message in result.diagnostics} == {
        "invalid-project",
        "invalid-tool",
    }
    assert result.dependencies == ()
    assert result.optional_dependency_groups == ()
    assert result.tool_configs == ()


def test_config_analysis_reports_invalid_dependency_shapes(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\ndependencies = "wrong"\n[project.optional-dependencies]\ndev = "wrong"\n',
        encoding="utf-8",
    )

    result = config_analysis(Database(mode="strict"), path)

    assert {code for code, _message in result.diagnostics} == {
        "invalid-project-dependencies",
        "invalid-optional-dependency-group",
    }
    assert result.dependencies == ()
    assert result.optional_dependency_groups == ()


# ---------------------------------------------------------------------------
# Cutoff / backdating
# ---------------------------------------------------------------------------


def test_comment_only_edit_backdates_config(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_MINIMAL_TOML, encoding="utf-8")

    db = Database()
    first = config_analysis(db, str(path))

    # Add a comment — TOML comments don't affect parsed structure.
    path.write_text("# A comment\n" + _MINIMAL_TOML, encoding="utf-8")
    second = config_analysis(db, str(path))

    assert first == second


def test_semantic_edit_invalidates_downstream(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_MINIMAL_TOML, encoding="utf-8")

    db = Database()
    first = config_analysis(db, str(path))
    assert first.dependencies == ("requests>=2.0", "click")

    # Change dependencies — semantic edit.
    updated = _MINIMAL_TOML.replace(
        'dependencies = ["requests>=2.0", "click"]',
        'dependencies = ["httpx>=0.24"]',
    )
    path.write_text(updated, encoding="utf-8")
    second = config_analysis(db, str(path))

    assert second.dependencies == ("httpx>=0.24",)
    assert first != second


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_workspace_config_analysis_discovers_pyproject_toml(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    config = root / "pyproject.toml"
    config.write_text(_MINIMAL_TOML, encoding="utf-8")

    db = Database()
    result = workspace_config_analysis(db, str(root))
    assert result is not None
    assert isinstance(result, ConfigAnalysis)
    assert "requests>=2.0" in result.dependencies


def test_config_analysis_on_nonexistent_file(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.toml"

    db = Database()
    result = config_analysis(db, str(path))

    # Missing file reads as empty string — produces a root section, no diagnostics.
    assert len(result.sections) == 1
    assert result.sections[0].name == "<root>"
    assert result.dependencies == ()
    assert result.diagnostics == ()


def test_workspace_config_analysis_returns_none_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    db = Database()
    result = workspace_config_analysis(db, str(root))
    assert result is None


# ---------------------------------------------------------------------------
# From-scratch oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_config_analysis_matches_fresh_recomputation_over_changes(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "pyproject.toml"
    steps: tuple[tuple[str, str], ...] = (
        ("initial", _MINIMAL_TOML),
        ("add comment", "# comment\n" + _MINIMAL_TOML),
        (
            "change deps",
            _MINIMAL_TOML.replace(
                'dependencies = ["requests>=2.0", "click"]',
                'dependencies = ["httpx"]',
            ),
        ),
        ("add tool", _MINIMAL_TOML + "\n[tool.black]\nline-length = 88\n"),
        (
            "change version",
            _MINIMAL_TOML.replace('version = "0.1.0"', 'version = "1.0.0"'),
        ),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert config_analysis(incremental, str(path)) == config_analysis(fresh, str(path))
