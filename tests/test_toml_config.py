from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations import json_config, toml_config, xml_config
from pyinc.integrations.toml_config import (
    _MAX_TOML_DEPTH,
    ConfigAnalysis,
    _load_toml,
    _structure_depth,
    _walk_sections,
    config_analysis,
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
# Nesting limit
# ---------------------------------------------------------------------------


def _nested_tables(levels: int, name: str = "configuration") -> str:
    """A document `levels` containers deep, all tables, with a leaf key at the bottom.

    Depth counts the document's implicit top-level table, so `levels` written header
    lines would be `levels + 1`; this writes `levels - 1` of them. The leaf key
    matters: `_toml_cutoff_value` renders an empty table as `()`, one snapshot level
    rather than two, so a document that bottoms out in an empty table is a level
    cheaper to freeze than the worst case at the same depth.
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
    """Run `work` with enough stack and budget to reach the kernel's snapshot limit.

    Without this the parse or the cutoff's `freeze` runs out of interpreter budget
    first, and the run would never get far enough to show which of the two limits
    is doing the rejecting.
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


def test_the_nesting_cap_keeps_every_accepted_document_snapshot_safe(tmp_path: Path) -> None:
    # `_toml_cutoff_value` rewrites every table as a tuple of `(key, value)` pairs,
    # so a table costs two snapshot levels where an array costs one. An all-table
    # document at the cap therefore lands on exactly the kernel's limit.
    assert 2 * _MAX_TOML_DEPTH <= _MAX_SNAPSHOT_DEPTH

    path = tmp_path / "pyproject.toml"
    path.write_text(_nested_tables(_MAX_TOML_DEPTH + 1), encoding="utf-8")
    observed: list[Any] = []

    def _recompute() -> None:
        db = Database()
        try:
            config_analysis(db, str(path))
            # The cutoff runs only on recomputation, so the edit is what reaches it.
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


def test_every_container_shape_at_the_cap_survives_the_cutoff() -> None:
    # Tables cost two snapshot levels and arrays one, so the all-table shape is the
    # worst case and every mixture of the two sits below it. The cutoff must hand
    # back a semantic token for all of them, not degrade to the raw text.
    shapes = [
        "d" * (_MAX_TOML_DEPTH - 1),
        "l" * (_MAX_TOML_DEPTH - 1),
        ("dl" * _MAX_TOML_DEPTH)[: _MAX_TOML_DEPTH - 1],
        ("ld" * _MAX_TOML_DEPTH)[: _MAX_TOML_DEPTH - 1],
        ("ddl" * _MAX_TOML_DEPTH)[: _MAX_TOML_DEPTH - 1],
    ]
    for index in range(60):
        # Deterministic, but spread across the whole space of table/array mixtures
        # rather than clustered at one end of it.
        state = index * 2_654_435_761 + 1
        characters = []
        for _ in range(_MAX_TOML_DEPTH - 1):
            state = (state * 6_364_136_223_846_793_005 + 1_442_695_040_888_963_407) % 2**64
            characters.append("d" if (state >> 33) & 1 else "l")
        shapes.append("".join(characters))

    observed: list[tuple[str, str]] = []

    def _tokens() -> None:
        for shape in shapes:
            text = _nested_mixed(shape)
            assert _structure_depth(_load_toml(text)) == _MAX_TOML_DEPTH
            observed.append((shape, toml_config._config_cutoff_token(text)[0]))

    _in_a_deep_stacked_thread(_tokens)

    assert len(observed) == len(shapes)
    assert {kind for _shape, kind in observed} == {"parsed"}


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


def test_a_freeze_failure_in_the_cutoff_degrades_to_the_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cap makes this unreachable, so the defensive clause is checked directly:
    # an unforeseen failure must miss a cutoff, never escape `config_analysis`.
    def _refuse(_value: Any) -> Any:
        raise ValueError("unforeseen")

    monkeypatch.setattr(toml_config, "freeze", _refuse)
    assert toml_config._config_cutoff_token("a = 1\n") == ("raw", "a = 1\n")


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
