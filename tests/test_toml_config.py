from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database, FileSystemArtifactStore
from pyinc.integrations import json_config, toml_config, xml_config
from pyinc.integrations.toml_config import (
    _MAX_TOML_DEPTH,
    ConfigAnalysis,
    _load_toml,
    _structure_depth,
    _walk_sections,
    config_analysis,
    config_analysis_payload,
    config_sections_payload,
    workspace_config_analysis,
)
from pyinc.value import _MAX_SNAPSHOT_DEPTH

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
# Incremental answers and backdating
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


# Five edits a read must not answer past. Each pair encodes identically once the
# document has been parsed and canonicalized -- a table and a two-element array,
# a temporal and a two-string array, and an inline table whose keys were sorted
# -- while the text and the public strings differ. Both halves of every pair are
# pinned: the document on the left has to analyse to `before_expected` and the one
# on the right to `expected`, so a warm answer that still describes the left-hand
# document fails against a value rather than against a marker, and neither half
# can drift unnoticed.
#
# The `date` and `time` rows pin `value_type == 'datetime'` on the left, over a
# real TOML date and a real TOML time: `_toml_value_type` maps all three of TOML's
# temporal types onto that one public string. That mapping is true before and
# after this change and is not what these rows are about, but this is the only
# place left that holds it.
_COLLIDING_EDITS: tuple[tuple[str, str, str, tuple[Any, ...], tuple[Any, ...]], ...] = (
    (
        "a table and an array of two strings",
        '[x]\na = "b"\n',
        'x = [["a", "b"]]\n',
        (("<root>", (), ("x",)), ("x", (("x", "a", "string", "'b'"),), ())),
        (("<root>", (("<root>", "x", "array", "[['a', 'b']]"),), ()),),
    ),
    (
        "a datetime and an array of two strings",
        "x = 1979-05-27T07:32:00\n",
        'x = ["datetime", "1979-05-27T07:32:00"]\n',
        (("<root>", (("<root>", "x", "datetime", "1979-05-27T07:32:00"),), ()),),
        (
            (
                "<root>",
                (("<root>", "x", "array", "['datetime', '1979-05-27T07:32:00']"),),
                (),
            ),
        ),
    ),
    (
        "a date and an array of two strings",
        "x = 1979-05-27\n",
        'x = ["date", "1979-05-27"]\n',
        (("<root>", (("<root>", "x", "datetime", "1979-05-27"),), ()),),
        (("<root>", (("<root>", "x", "array", "['date', '1979-05-27']"),), ()),),
    ),
    (
        "a time and an array of two strings",
        "x = 07:32:00\n",
        'x = ["time", "07:32:00"]\n',
        (("<root>", (("<root>", "x", "datetime", "07:32:00"),), ()),),
        (("<root>", (("<root>", "x", "array", "['time', '07:32:00']"),), ()),),
    ),
    (
        "an inline table's key order",
        "x = [{b = 1, a = 2}]\n",
        "x = [{a = 2, b = 1}]\n",
        (("<root>", (("<root>", "x", "array", "[{'b': 1, 'a': 2}]"),), ()),),
        (("<root>", (("<root>", "x", "array", "[{'a': 2, 'b': 1}]"),), ()),),
    ),
)

_COLLISION_IDS = ("table-array", "datetime", "date", "time", "inline-key-order")

# Reorder edits: the key order a document was written in survives into the public
# string but not into any sorted projection of it. The ladder walks the same
# reorder down through an array of tables and then through one, two and three
# levels of container.
_REORDERED_EDITS: tuple[tuple[str, str, str, tuple[Any, ...]], ...] = (
    (
        "an array of tables",
        '[[tool.x.items]]\nname = "a"\nversion = "1"\n',
        '[[tool.x.items]]\nversion = "1"\nname = "a"\n',
        (
            ("<root>", (), ("tool",)),
            ("tool", (), ("tool.x",)),
            (
                "tool.x",
                (("tool.x", "items", "array", "[{'version': '1', 'name': 'a'}]"),),
                (),
            ),
        ),
    ),
    (
        "an inline table one level down",
        "a = [{ x = 1, y = 2 }]\n",
        "a = [{ y = 2, x = 1 }]\n",
        (("<root>", (("<root>", "a", "array", "[{'y': 2, 'x': 1}]"),), ()),),
    ),
    (
        "an inline table two levels down",
        "a = [[{ x = 1, y = 2 }]]\n",
        "a = [[{ y = 2, x = 1 }]]\n",
        (("<root>", (("<root>", "a", "array", "[[{'y': 2, 'x': 1}]]"),), ()),),
    ),
    (
        "an inline table three levels down",
        "a = [{ b = [{ x = 1, y = 2 }] }]\n",
        "a = [{ b = [{ y = 2, x = 1 }] }]\n",
        (("<root>", (("<root>", "a", "array", "[{'b': [{'y': 2, 'x': 1}]}]"),), ()),),
    ),
)

_REORDER_IDS = ("array-of-tables", "depth-1", "depth-2", "depth-3")


def _sections_of(result: ConfigAnalysis) -> tuple[Any, ...]:
    """Project an analysis back onto the section payload shape, for readable failures."""
    return tuple(
        (
            section.name,
            tuple((k.section, k.key, k.value_type, k.string_value) for k in section.keys),
            section.subsections,
        )
        for section in result.sections
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    ("label", "before", "after", "before_expected", "expected"),
    _COLLIDING_EDITS,
    ids=_COLLISION_IDS,
)
def test_a_colliding_edit_is_answered_with_the_text_that_was_read(
    label: str,
    before: str,
    after: str,
    before_expected: tuple[Any, ...],
    expected: tuple[Any, ...],
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(before, encoding="utf-8")

    db = Database(mode=mode)
    first = config_analysis(db, str(path))
    assert _sections_of(first) == before_expected, (
        f"{label} | before {_sections_of(first)} | expected {before_expected}"
    )

    path.write_text(after, encoding="utf-8")
    warm = config_analysis(db, str(path))
    fresh = config_analysis(Database(mode=mode), str(path))

    assert _sections_of(fresh) == expected, f"{label} | fresh {_sections_of(fresh)}"
    assert _sections_of(warm) == expected, (
        f"{label} | warm {_sections_of(warm)} | expected {expected}"
    )
    assert warm == fresh, f"{label} | {before!r} -> {after!r} | warm {_sections_of(warm)}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_colliding_edit_is_answered_the_same_way_through_workspace_discovery(
    mode: str, tmp_path: Path
) -> None:
    label, before, after, before_expected, expected = _COLLIDING_EDITS[0]
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "pyproject.toml"
    path.write_text(before, encoding="utf-8")

    db = Database(mode=mode)
    first = workspace_config_analysis(db, str(root))
    assert first is not None
    assert _sections_of(first) == before_expected, (
        f"{label} | before {_sections_of(first)} | expected {before_expected}"
    )

    path.write_text(after, encoding="utf-8")
    warm = workspace_config_analysis(db, str(root))
    fresh = workspace_config_analysis(Database(mode=mode), str(root))

    assert warm is not None and fresh is not None
    assert _sections_of(warm) == expected, (
        f"{label} | warm {_sections_of(warm)} | expected {expected}"
    )
    assert warm == fresh, f"{label} | {before!r} -> {after!r} | warm {_sections_of(warm)}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    ("label", "before", "after", "before_expected", "expected"),
    _COLLIDING_EDITS,
    ids=_COLLISION_IDS,
)
def test_a_colliding_edit_survives_a_checkpoint(
    label: str,
    before: str,
    after: str,
    before_expected: tuple[Any, ...],
    expected: tuple[Any, ...],
    mode: str,
    tmp_path: Path,
) -> None:
    # Both the edit and a drive that lets the stale answer form have to happen
    # before the save. Saving first and editing after does not reproduce: on
    # reload the resource probe mismatches, the read executes on the new bytes,
    # and no earlier answer is left to serve -- the row would then be green
    # whether or not the defect is present.
    path = tmp_path / "pyproject.toml"
    path.write_text(before, encoding="utf-8")

    store_dir = tmp_path / "store"
    saver = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    first = config_analysis(saver, str(path))
    assert _sections_of(first) == before_expected, (
        f"{label} | before {_sections_of(first)} | expected {before_expected}"
    )

    path.write_text(after, encoding="utf-8")
    config_analysis(saver, str(path))
    key = saver.save_checkpoint()

    reloaded = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    reloaded.load_checkpoint(key)

    # Values only: a reloaded record reports `executed` or `reused` either way,
    # so no recompute marker can tell a restored answer from a stale one.
    warm = config_analysis(reloaded, str(path))
    fresh = config_analysis(Database(mode=mode), str(path))

    assert _sections_of(warm) == expected, (
        f"{label} | reloaded {_sections_of(warm)} | expected {expected}"
    )
    assert warm == fresh, f"{label} | {before!r} -> {after!r} | reloaded {_sections_of(warm)}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    ("label", "before", "after", "expected"), _REORDERED_EDITS, ids=_REORDER_IDS
)
def test_a_reorder_edit_is_answered_with_the_text_that_was_read(
    label: str, before: str, after: str, expected: tuple[Any, ...], mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(before, encoding="utf-8")

    db = Database(mode=mode)
    config_analysis(db, str(path))
    workspace_config_analysis(db, str(tmp_path))
    path.write_text(after, encoding="utf-8")

    warm = config_analysis(db, str(path))
    warm_workspace = workspace_config_analysis(db, str(tmp_path))
    fresh = config_analysis(Database(mode=mode), str(path))

    assert _sections_of(fresh) == expected, f"{label} | fresh {_sections_of(fresh)}"
    assert _sections_of(warm) == expected, (
        f"{label} | warm {_sections_of(warm)} | expected {expected}"
    )
    assert warm_workspace is not None
    assert _sections_of(warm_workspace) == expected, (
        f"{label} | workspace {_sections_of(warm_workspace)} | expected {expected}"
    )
    assert warm == fresh, f"{label} | {before!r} -> {after!r} | warm {_sections_of(warm)}"


# The four queries that re-read the text and re-derive a projection of it.
_PAYLOAD_QUERIES = (
    "config_sections_payload",
    "config_dependencies_payload",
    "config_tool_configs_payload",
    "config_diagnostics_payload",
)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_formatting_only_edit_recomputes_the_payloads_and_backdates_the_analysis(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_MINIMAL_TOML, encoding="utf-8")

    db = Database(mode=mode)
    first = config_analysis(db, str(path))

    db.reset_statistics()
    path.write_text("# a comment\n" + _MINIMAL_TOML, encoding="utf-8")
    second = config_analysis(db, str(path))

    assert first == second, "a comment-only edit moved the analysis"

    # `query_profile()` records executions only and `reset_statistics()` has just
    # cleared it, so a query that was reused has no row at all -- there is no row
    # carrying a zero to look for. Labels also carry an argument-hash suffix, so a
    # lookup by bare query name never matches; match by substring instead.
    executed = [profile.query_label for profile in db.query_profile()]
    for name in _PAYLOAD_QUERIES:
        assert any(name in label for label in executed), (
            f"{name} did not re-run | executed {executed}"
        )
    assert not [label for label in executed if "config_analysis_payload" in label], (
        f"config_analysis_payload re-ran instead of backdating | executed {executed}"
    )

    statistics = db.statistics()
    assert statistics.query_executions > 0, f"nothing re-ran | {statistics}"
    assert statistics.query_backdates > 0, f"nothing backdated | {statistics}"


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
# Nesting limit
# ---------------------------------------------------------------------------


def _nested_tables(levels: int, name: str = "configuration") -> str:
    """A document `levels` containers deep, all tables, with a leaf key at the bottom.

    Depth counts the document's implicit top-level table, so `levels` written header
    lines would be `levels + 1`; this writes `levels - 1` of them. The leaf key
    belongs to the deepest table alone — every table above it holds nothing but its
    one subsection — and it is what takes the cached payload to depth 4 rather than
    3, because a section with no keys carries an empty `keys` tuple with nothing
    inside it to count.
    """
    headers = "\n".join("[" + ".".join([name] * d) + "]" for d in range(1, levels))
    return headers + "\nleaf = 1\n"


def _nested_inline_tables(levels: int) -> str:
    return "a = " + "{ q = " * (levels - 1) + "1" + " }" * (levels - 1) + "\n"


def _nested_arrays(levels: int) -> str:
    return "a = " + "[" * (levels - 1) + "1" + "]" * (levels - 1) + "\n"


def _nested_mixed(shape: str) -> str:
    """`shape` is one character per written level: `d` for a table, `l` for an array."""
    inner = "1"
    for character in reversed(shape):
        inner = "{ q = " + inner + " }" if character == "d" else "[" + inner + "]"
    return "a = " + inner + "\n"


@pytest.mark.parametrize(
    ("text", "depth"),
    [
        # The top-level table is always there, even when nothing is written.
        ("", 1),
        ("# just a comment\n", 1),
        ("a = 1\n", 1),
        ("[a]\n", 2),
        ("[a.b]\n", 3),
        ("a.b.c = 1\n", 3),
        ("a = { q = 1 }\n", 2),
        ("a = { q = { r = 1 } }\n", 3),
        ("a = [1]\n", 2),
        ("a = [[1]]\n", 3),
        ("a = [{ q = 1 }]\n", 3),
        # An array of tables is a list wrapping a table: two containers per header.
        ("[[a]]\n", 3),
        ("[[a.b]]\n", 4),
        # Deepest branch wins, not the last one.
        ("[a.b.c]\nx = 1\n[z]\ny = 2\n", 4),
        # Brackets and braces that are not structure.
        ("a = 'literal [ { value'\n", 1),
        ('a = "basic [[[ {{{ value"\n', 1),
        ("# [x.y.z]\na = 1\n", 1),
    ],
)
def test_structure_depth_counts_containers_including_the_implicit_root(
    text: str, depth: int
) -> None:
    assert _structure_depth(_load_toml(text)) == depth


def test_config_analysis_accepts_a_document_at_the_nesting_cap(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_nested_tables(_MAX_TOML_DEPTH), encoding="utf-8")

    result = config_analysis(Database(), str(path))

    assert result.diagnostics == ()
    assert len(result.sections) == _MAX_TOML_DEPTH
    assert result.sections[-1].name.count(".") == _MAX_TOML_DEPTH - 2


@pytest.mark.parametrize(
    "build",
    [_nested_tables, _nested_inline_tables, _nested_arrays],
    ids=["tables", "inline-tables", "arrays"],
)
def test_toml_nesting_past_the_cap_is_rejected_with_a_diagnostic(
    build: Callable[[int], str], tmp_path: Path
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(build(_MAX_TOML_DEPTH + 1), encoding="utf-8")

    result = config_analysis(Database(), str(path))

    assert result.sections == ()
    assert result.dependencies == ()
    assert result.diagnostics == (
        (
            "toml-decode-error",
            f"TOML nesting exceeds the supported limit of {_MAX_TOML_DEPTH} levels",
        ),
    )


def test_toml_json_and_xml_report_a_nesting_limit_in_the_same_words(tmp_path: Path) -> None:
    toml_path = tmp_path / "pyproject.toml"
    toml_path.write_text(_nested_tables(_MAX_TOML_DEPTH + 1), encoding="utf-8")
    json_levels = json_config._MAX_JSON_DEPTH
    json_path = tmp_path / "package.json"
    json_path.write_text(
        '{"a": ' * (json_levels + 1) + "1" + "}" * (json_levels + 1),
        encoding="utf-8",
    )
    xml_levels = xml_config._MAX_XML_DEPTH
    xml_path = tmp_path / "pom.xml"
    xml_path.write_text(
        "<root>" + "<level>" * xml_levels + "leaf" + "</level>" * xml_levels + "</root>",
        encoding="utf-8",
    )

    # A user who hits a depth limit in one format should get the same answer in the
    # others, down to the wording.
    limit_template = "{format} nesting exceeds the supported limit of {limit} levels"
    stack_template = "{format} parsing exhausted the interpreter stack"

    assert config_analysis(Database(), str(toml_path)).diagnostics == (
        ("toml-decode-error", limit_template.format(format="TOML", limit=_MAX_TOML_DEPTH)),
    )
    assert json_config.json_analysis(Database(), str(json_path)).diagnostics == (
        ("json-decode-error", limit_template.format(format="JSON", limit=json_levels)),
    )
    assert xml_config.xml_analysis(Database(), str(xml_path)).diagnostics == (
        ("xml-parse-error", limit_template.format(format="XML", limit=xml_levels)),
    )
    toml_stack_message = toml_config._STACK_EXHAUSTED_DIAGNOSTIC
    json_stack_message = json_config._STACK_EXHAUSTED_DIAGNOSTIC
    xml_stack_message = xml_config._STACK_EXHAUSTED_DIAGNOSTIC
    assert toml_stack_message == stack_template.format(format="TOML")
    assert json_stack_message == stack_template.format(format="JSON")
    assert xml_stack_message == stack_template.format(format="XML")


def _in_a_deep_stacked_thread(work: Callable[[], None]) -> None:
    """Run `work` with a large stack and a raised recursion limit.

    `tomllib` descends once per inline-table and once per array level, so a document
    at the nesting cap costs hundreds of frames. Running with budget to spare is what
    makes the outcome these tests observe a property of the document rather than of
    the stack: a spent stack and the depth check are both reported as
    `toml-decode-error`, and only a run that cannot run out of stack tells them
    apart.
    """
    original_limit = sys.getrecursionlimit()
    original_stack = threading.stack_size(64 * 1024 * 1024)
    sys.setrecursionlimit(5000)
    try:
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
    finally:
        sys.setrecursionlimit(original_limit)
        threading.stack_size(original_stack)


def _snapshot_depth(value: object) -> int:
    """Report the container nesting of a cached value without recursing into it."""
    deepest = 0
    pending: list[tuple[object, int]] = [(value, 1)]

    while pending:
        current, depth = pending.pop()
        children: list[object]
        if isinstance(current, tuple | list):
            children = list(current)
        elif isinstance(current, dict):
            children = list(current.values())
        else:
            continue
        if depth > deepest:
            deepest = depth
        pending.extend((child, depth + 1) for child in children)

    return deepest


def test_a_deep_document_neither_deepens_the_cache_nor_escapes_the_analysis(
    tmp_path: Path,
) -> None:
    # What gets cached is a flat tuple of `(name, keys, subsections)` triples, so
    # its nesting is a property of that shape and not of the document's: the same
    # document written two orders of magnitude deeper caches exactly as deep. That
    # is what keeps every accepted document inside the kernel's snapshot limit,
    # however deep it nests, and it is why the cap is free to sit where the ~1 MiB
    # payload budget puts it.
    shallow = tmp_path / "shallow.toml"
    shallow.write_text(_nested_tables(3), encoding="utf-8")
    deep = tmp_path / "deep.toml"
    deep.write_text(_nested_tables(_MAX_TOML_DEPTH), encoding="utf-8")

    db = Database()
    depths: dict[tuple[str, str], int] = {}
    for document in (shallow, deep):
        depths[(document.stem, "sections")] = _snapshot_depth(
            db.get(config_sections_payload, str(document))
        )
        depths[(document.stem, "analysis")] = _snapshot_depth(
            db.get(config_analysis_payload, str(document))
        )

    assert depths == {
        ("shallow", "sections"): 4,
        ("deep", "sections"): 4,
        ("shallow", "analysis"): 5,
        ("deep", "analysis"): 5,
    }, f"document depths 3 and {_MAX_TOML_DEPTH} | {depths}"
    assert max(depths.values()) < _MAX_SNAPSHOT_DEPTH, f"{depths}"

    path = tmp_path / "pyproject.toml"
    path.write_text(_nested_tables(_MAX_TOML_DEPTH + 1), encoding="utf-8")
    observed: list[Any] = []

    def _recompute() -> None:
        db = Database()
        try:
            config_analysis(db, str(path))
            # The document is parsed again only on recomputation, so the edit is
            # what drives it back through the depth check.
            path.write_text(_nested_tables(_MAX_TOML_DEPTH + 1) + "\n", encoding="utf-8")
            observed.append(config_analysis(db, str(path)).diagnostics)
        except Exception as exc:  # pragma: no cover - the escape this test locks out
            observed.append(exc)

    _in_a_deep_stacked_thread(_recompute)

    assert observed == [
        (
            (
                "toml-decode-error",
                f"TOML nesting exceeds the supported limit of {_MAX_TOML_DEPTH} levels",
            ),
        )
    ]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_every_container_shape_analyses_cleanly_at_the_cap_and_reports_past_it(
    mode: str, tmp_path: Path
) -> None:
    # Header tables, inline tables and arrays reach the depth check by different
    # routes through `tomllib` -- the first iteratively, the other two by
    # recursion -- so each is walked twice: at the cap, where it must analyse with
    # no diagnostic, and one level past it, where it must come back as a
    # diagnostic rather than as an exception. Each document is also walked with
    # and without a following comment-only edit, because a recomputation is what
    # sends it through the parser a second time.
    #
    # The whole sweep runs on a deep stack. `tomllib` descends once per inline-table
    # and array level, so the two recursive shapes cost hundreds of frames at this
    # cap; running them with budget to spare keeps what this test measures a
    # property of the documents rather than of whatever stack the platform happened
    # to give the test runner.
    builders: tuple[tuple[str, Callable[[int], str]], ...] = (
        ("tables", _nested_tables),
        ("inline tables", _nested_inline_tables),
        ("arrays", _nested_arrays),
    )
    past_the_cap = (
        (
            "toml-decode-error",
            f"TOML nesting exceeds the supported limit of {_MAX_TOML_DEPTH} levels",
        ),
    )

    anomalies: list[str] = []
    cases: list[str] = []
    path = tmp_path / "pyproject.toml"

    def _sweep() -> None:
        for shape, build in builders:
            for depth_label, levels, expected in (
                ("at the cap", _MAX_TOML_DEPTH, ()),
                ("past the cap", _MAX_TOML_DEPTH + 1, past_the_cap),
            ):
                for edited in (False, True):
                    case = f"{shape} {depth_label} edited={edited}"
                    cases.append(case)
                    path.write_text(build(levels), encoding="utf-8")
                    db = Database(mode=mode)
                    try:
                        config_analysis(db, str(path))
                        if edited:
                            path.write_text(build(levels) + "# tail\n", encoding="utf-8")
                        warm = config_analysis(db, str(path))
                        fresh = config_analysis(Database(mode=mode), str(path))
                    except Exception as exc:  # pragma: no cover - the escape this locks out
                        anomalies.append(f"{case}: escaped {type(exc).__name__}: {exc}")
                        continue
                    if warm.diagnostics != expected:
                        anomalies.append(f"{case}: diagnostics {warm.diagnostics}")
                    if warm != fresh:
                        anomalies.append(
                            f"{case}: warm {len(warm.sections)} sections "
                            f"!= fresh {len(fresh.sections)}"
                        )

    _in_a_deep_stacked_thread(_sweep)

    assert len(cases) == 12, f"{cases}"
    assert anomalies == [], " | ".join(anomalies)


def test_every_mixed_container_shape_parses_at_the_cap() -> None:
    # The sweep above builds documents that are all tables, all inline tables or
    # all arrays. `tomllib` descends once per inline-table and once per array
    # level, so a document that alternates the two is the deepest recursion this
    # module can be asked for, and the cap raise doubled it. These three mixtures
    # are what the sweep does not build.
    shapes = {
        "one table per array": ("dl" * _MAX_TOML_DEPTH)[: _MAX_TOML_DEPTH - 1],
        "one array per table": ("ld" * _MAX_TOML_DEPTH)[: _MAX_TOML_DEPTH - 1],
        "two tables per array": ("ddl" * _MAX_TOML_DEPTH)[: _MAX_TOML_DEPTH - 1],
    }
    observed: dict[str, int] = {}
    failures: list[str] = []

    def _parse() -> None:
        for label, shape in shapes.items():
            try:
                observed[label] = _structure_depth(_load_toml(_nested_mixed(shape)))
            except Exception as exc:  # pragma: no cover - the escape this locks out
                failures.append(f"{label}: {type(exc).__name__}: {exc}")

    _in_a_deep_stacked_thread(_parse)

    assert failures == [], " | ".join(failures)
    assert observed == dict.fromkeys(shapes, _MAX_TOML_DEPTH), f"{observed}"


def test_over_deep_toml_reports_the_same_result_at_every_caller_stack_depth(
    tmp_path: Path,
) -> None:
    # `tomllib` builds header-nested tables iteratively, so this document reaches
    # the depth check whatever the caller has already spent.
    path = tmp_path / "pyproject.toml"
    path.write_text(_nested_tables(_MAX_TOML_DEPTH + 101), encoding="utf-8")

    observed: list[tuple[int, tuple[tuple[str, str], ...]]] = []

    def _analyse_at_depth(remaining: int) -> tuple[int, tuple[tuple[str, str], ...]]:
        if remaining:
            return _analyse_at_depth(remaining - 1)
        result = config_analysis(Database(), str(path))
        return (len(result.sections), result.diagnostics)

    def _probe() -> None:
        observed.extend(_analyse_at_depth(pad) for pad in (0, 400, 800))

    # A fresh thread starts at the bottom of its own Python stack, so the limit set
    # here is the whole budget the run gets.
    original = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        thread = threading.Thread(target=_probe)
        thread.start()
        thread.join()
    finally:
        sys.setrecursionlimit(original)

    assert len(observed) == 3
    assert set(observed) == {
        (
            0,
            (
                (
                    "toml-decode-error",
                    f"TOML nesting exceeds the supported limit of {_MAX_TOML_DEPTH} levels",
                ),
            ),
        )
    }


def test_the_section_walk_does_not_consume_the_interpreter_recursion_budget() -> None:
    parsed = _load_toml(_nested_tables(_MAX_TOML_DEPTH))
    walked: list[int] = []

    def _walk() -> None:
        walked.append(len(_walk_sections(parsed, "")))

    # The document is parsed outside the lowered limit; what is measured here is the
    # walk alone, an order of magnitude below the document's own nesting.
    original = sys.getrecursionlimit()
    sys.setrecursionlimit(60)
    try:
        thread = threading.Thread(target=_walk)
        thread.start()
        thread.join()
    finally:
        sys.setrecursionlimit(original)

    assert walked == [_MAX_TOML_DEPTH]


def test_the_depth_scan_does_not_consume_the_interpreter_recursion_budget() -> None:
    parsed = _load_toml(_nested_tables(_MAX_TOML_DEPTH))
    measured: list[int] = []

    def _measure() -> None:
        measured.append(_structure_depth(parsed))

    original = sys.getrecursionlimit()
    sys.setrecursionlimit(60)
    try:
        thread = threading.Thread(target=_measure)
        thread.start()
        thread.join()
    finally:
        sys.setrecursionlimit(original)

    assert measured == [_MAX_TOML_DEPTH]


# ---------------------------------------------------------------------------
# Amplification budget
# ---------------------------------------------------------------------------


# Every section re-emits the dot path of all its ancestors, once as its own name
# and again in its parent's `subsections`, so the cached payload grows
# quadratically in nesting depth and linearly in table-name length. `_MAX_TOML_DEPTH`
# holds the worst case the budget covers — a name of `_BUDGETED_NAME_LENGTH`
# characters at the cap — under this ceiling, the same ceiling `json_config` and
# `xml_config` use.
_SECTIONS_PAYLOAD_BUDGET = 1024 * 1024
_BUDGETED_NAME_LENGTH = 20


def test_sections_payload_at_the_cap_stays_within_the_amplification_budget() -> None:
    text = _nested_tables(_MAX_TOML_DEPTH, name="k" * _BUDGETED_NAME_LENGTH)
    sections = _walk_sections(_load_toml(text), "")

    assert len(sections) == _MAX_TOML_DEPTH
    assert len(repr(tuple(sections))) < _SECTIONS_PAYLOAD_BUDGET


# ---------------------------------------------------------------------------
# Stack exhaustion
# ---------------------------------------------------------------------------


def test_stack_exhaustion_diagnostic_does_not_vary_with_the_recursion_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("[project]\nname = 'x'\n", encoding="utf-8")

    # CPython names whichever frame ran out of budget, which is a property of the
    # call site rather than of the file. A cached payload must not carry it.
    messages = (
        "maximum recursion depth exceeded",
        "maximum recursion depth exceeded while calling a Python object",
        "maximum recursion depth exceeded in comparison",
    )

    observed = set()
    for message in messages:

        def _exhaust_the_stack(_text: str, _message: str = message) -> Any:
            raise RecursionError(_message)

        monkeypatch.setattr(toml_config, "_load_toml", _exhaust_the_stack)
        observed.add(config_analysis(Database(), str(path)).diagnostics)

    assert observed == {(("toml-decode-error", "TOML parsing exhausted the interpreter stack"),)}


def test_nesting_limit_diagnostic_matches_fresh_recomputation(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"

    steps: tuple[tuple[str, str], ...] = (
        ("shallow", _MINIMAL_TOML),
        ("at the cap", _nested_tables(_MAX_TOML_DEPTH)),
        ("past the cap", _nested_tables(_MAX_TOML_DEPTH + 1)),
        ("past the cap in inline tables", _nested_inline_tables(_MAX_TOML_DEPTH + 1)),
        ("past the cap in arrays", _nested_arrays(_MAX_TOML_DEPTH + 1)),
        ("shallow again", _MINIMAL_TOML),
    )

    incremental = Database()
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database()
        assert config_analysis(incremental, str(path)) == config_analysis(fresh, str(path))


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
