from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database, FileSystemArtifactStore
from pyinc.integrations import json_config, xml_config
from pyinc.integrations.json_config import (
    _MAX_JSON_DEPTH,
    JsonAnalysis,
    _load_json,
    _text_nesting_depth,
    _walk_sections,
    json_analysis,
    json_analysis_payload,
    json_sections_payload,
    workspace_json_analysis,
)
from pyinc.value import _MAX_SNAPSHOT_DEPTH

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


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"key": 1, "key": 2}', "duplicate JSON object key"),
        ('{"value": NaN}', "non-finite JSON number"),
        ('{"value": Infinity}', "non-finite JSON number"),
        ('{"value": -Infinity}', "non-finite JSON number"),
        ('{"value": 1e999}', "non-finite JSON number"),
    ],
)
def test_json_analysis_rejects_nonstandard_or_ambiguous_json(
    text: str,
    message: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")

    result = json_analysis(Database(mode="strict"), path)

    assert result.sections == ()
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "json-decode-error"
    assert message in result.diagnostics[0][1]


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
# Incremental answers and backdating
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


# Reorder edits. The order an object's keys were written in survives into the
# public string, which renders them as they were parsed, but not into any sorted
# projection of the document. `expected` is what the document on the right
# actually analyses to, so a warm answer still describing the document on the
# left fails against a value rather than against a marker. The ladder walks the
# same reorder one, two, three and four containers down.
_REORDERED_EDITS: tuple[tuple[str, str, str, tuple[Any, ...]], ...] = (
    (
        "a dependency list",
        '{"deps": [{"name": "a", "version": "1"}]}',
        '{"deps": [{"version": "1", "name": "a"}]}',
        (("<root>", (("<root>", "deps", "array", "[{'version': '1', 'name': 'a'}]"),), ()),),
    ),
    (
        "an object one container down",
        '{"a": [{"x": 1, "y": 2}]}',
        '{"a": [{"y": 2, "x": 1}]}',
        (("<root>", (("<root>", "a", "array", "[{'y': 2, 'x': 1}]"),), ()),),
    ),
    (
        "an object two containers down",
        '{"a": [[{"x": 1, "y": 2}]]}',
        '{"a": [[{"y": 2, "x": 1}]]}',
        (("<root>", (("<root>", "a", "array", "[[{'y': 2, 'x': 1}]]"),), ()),),
    ),
    (
        "an object three containers down",
        '{"a": [{"b": [{"x": 1, "y": 2}]}]}',
        '{"a": [{"b": [{"y": 2, "x": 1}]}]}',
        (("<root>", (("<root>", "a", "array", "[{'b': [{'y': 2, 'x': 1}]}]"),), ()),),
    ),
    (
        "an object four containers down",
        '{"a": [{"b": [[{"x": 1, "y": 2}]]}]}',
        '{"a": [{"b": [[{"y": 2, "x": 1}]]}]}',
        (("<root>", (("<root>", "a", "array", "[{'b': [[{'y': 2, 'x': 1}]]}]"),), ()),),
    ),
)

_REORDER_IDS = ("deps", "depth-1", "depth-2", "depth-3", "depth-4")


def _sections_of(result: JsonAnalysis) -> tuple[Any, ...]:
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
    ("label", "before", "after", "expected"), _REORDERED_EDITS, ids=_REORDER_IDS
)
def test_a_reorder_edit_is_answered_with_the_text_that_was_read(
    label: str, before: str, after: str, expected: tuple[Any, ...], mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "package.json"
    path.write_text(before, encoding="utf-8")

    db = Database(mode=mode)
    json_analysis(db, str(path))
    workspace_json_analysis(db, str(tmp_path))
    path.write_text(after, encoding="utf-8")

    warm = json_analysis(db, str(path))
    warm_workspace = workspace_json_analysis(db, str(tmp_path))
    fresh = json_analysis(Database(mode=mode), str(path))

    assert _sections_of(fresh) == expected, f"{label} | fresh {_sections_of(fresh)}"
    assert _sections_of(warm) == expected, (
        f"{label} | warm {_sections_of(warm)} | expected {expected}"
    )
    assert warm_workspace is not None
    assert _sections_of(warm_workspace) == expected, (
        f"{label} | workspace {_sections_of(warm_workspace)} | expected {expected}"
    )
    assert warm == fresh, f"{label} | {before!r} -> {after!r} | warm {_sections_of(warm)}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reorder_edit_moves_the_reported_string_value(mode: str, tmp_path: Path) -> None:
    # The narrowest statement of the same defect: one key, one public field.
    path = tmp_path / "package.json"
    path.write_text('{"x": [{"b": 1, "a": 2}]}', encoding="utf-8")

    db = Database(mode=mode)
    json_analysis(db, str(path))
    path.write_text('{"x": [{"a": 2, "b": 1}]}', encoding="utf-8")

    warm = json_analysis(db, str(path)).sections[0].keys[0]
    fresh = json_analysis(Database(mode=mode), str(path)).sections[0].keys[0]

    assert fresh.string_value == "[{'a': 2, 'b': 1}]", f"fresh {fresh.string_value}"
    assert warm.string_value == "[{'a': 2, 'b': 1}]", f"warm {warm.string_value}"
    assert warm == fresh, f"warm {warm} | fresh {fresh}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reorder_edit_survives_a_checkpoint(mode: str, tmp_path: Path) -> None:
    # Both the edit and a drive that lets the stale answer form have to happen
    # before the save. Saving first and editing after does not reproduce: on
    # reload the resource probe mismatches, the read executes on the new bytes,
    # and no earlier answer is left to serve -- the row would then be green
    # whether or not the defect is present.
    label, before, after, expected = _REORDERED_EDITS[0]
    path = tmp_path / "package.json"
    path.write_text(before, encoding="utf-8")

    store_dir = tmp_path / "store"
    saver = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    json_analysis(saver, str(path))

    path.write_text(after, encoding="utf-8")
    json_analysis(saver, str(path))
    key = saver.save_checkpoint()

    reloaded = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    reloaded.load_checkpoint(key)

    # Values only: a reloaded record reports `executed` or `reused` either way,
    # so no recompute marker can tell a restored answer from a stale one.
    warm = json_analysis(reloaded, str(path))
    fresh = json_analysis(Database(mode=mode), str(path))

    assert _sections_of(warm) == expected, (
        f"{label} | reloaded {_sections_of(warm)} | expected {expected}"
    )
    assert warm == fresh, f"{label} | {before!r} -> {after!r} | reloaded {_sections_of(warm)}"


# The two queries that re-read the text and re-derive a projection of it.
_PAYLOAD_QUERIES = ("json_sections_payload", "json_diagnostics_payload")


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_formatting_only_edit_recomputes_the_payloads_and_leaves_the_analysis_reused(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    path.write_text(_MINIMAL_JSON, encoding="utf-8")

    db = Database(mode=mode)
    first = json_analysis(db, str(path))

    db.reset_statistics()
    path.write_text(json.dumps(json.loads(_MINIMAL_JSON), indent=4), encoding="utf-8")
    second = json_analysis(db, str(path))

    assert first == second, "a reformat moved the analysis"

    # `query_profile()` records executions only and `reset_statistics()` has just
    # cleared it, so a query that was reused has no row at all -- there is no row
    # carrying a zero to look for. Labels also carry an argument-hash suffix, so a
    # lookup by bare query name never matches; match by substring instead.
    executed = [profile.query_label for profile in db.query_profile()]
    for name in _PAYLOAD_QUERIES:
        assert any(name in label for label in executed), (
            f"{name} did not re-run | executed {executed}"
        )
    assert not [label for label in executed if "json_analysis_payload" in label], (
        f"json_analysis_payload re-ran instead of staying reused | executed {executed}"
    )

    statistics = db.statistics()
    assert statistics.query_executions > 0, f"nothing re-ran | {statistics}"
    assert statistics.query_backdates > 0, f"nothing backdated | {statistics}"


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
# Nesting limit
# ---------------------------------------------------------------------------


def _nested_json(containers: int, key: str = "configuration") -> str:
    """A document `containers` objects deep — `containers - 1` wrappers plus a leaf."""
    return f'{{"{key}": ' * (containers - 1) + '{"leaf": 1}' + "}" * (containers - 1)


# Alternating object/array nesting far past the cap: both bracket kinds count
# toward the depth, and the text scan rejects this before the scanner sees it.
_OVER_DEEP_JSON = '{"a":[' * 2000 + "1" + "]}" * 2000


@pytest.mark.parametrize(
    ("text", "depth"),
    [
        ("", 0),
        ("42", 0),
        ("{}", 1),
        ("[[[]]]", 3),
        ('{"a": {"b": [1]}}', 3),
        # Brackets inside string literals are not structure.
        ('{"a": "{{{[[["}', 1),
        ('{"a": "\\\\"}', 1),
        ('{"a": "\\""}', 1),
        # The escaped character has to go with its backslash: dropping only the
        # `u00e9` would leave `\"` and swallow the quote that ends the key.
        ('{"\\u00e9": {"x": 1}}', 2),
    ],
)
def test_text_nesting_depth_counts_only_structure(text: str, depth: int) -> None:
    assert _text_nesting_depth(text) == depth


def test_json_analysis_accepts_a_document_at_the_nesting_cap(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text(_nested_json(_MAX_JSON_DEPTH), encoding="utf-8")

    result = json_analysis(Database(), str(path))

    assert result.diagnostics == ()
    assert len(result.sections) == _MAX_JSON_DEPTH
    assert result.sections[-1].name.count(".") == _MAX_JSON_DEPTH - 2


def test_json_nesting_past_the_cap_is_rejected_with_a_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "deeper.json"
    path.write_text(_nested_json(_MAX_JSON_DEPTH + 1), encoding="utf-8")

    result = json_analysis(Database(), str(path))

    assert result.sections == ()
    assert result.diagnostics == (
        (
            "json-decode-error",
            f"JSON nesting exceeds the supported limit of {_MAX_JSON_DEPTH} levels",
        ),
    )


def test_json_and_xml_report_a_nesting_limit_in_the_same_words(tmp_path: Path) -> None:
    json_path = tmp_path / "deep.json"
    json_path.write_text(_nested_json(_MAX_JSON_DEPTH + 1), encoding="utf-8")
    levels = xml_config._MAX_XML_DEPTH
    xml_path = tmp_path / "deep.xml"
    xml_path.write_text(
        "<root>" + "<level>" * levels + "leaf" + "</level>" * levels + "</root>",
        encoding="utf-8",
    )

    # A user who hits a depth limit in one format should get the same answer in the
    # other, down to the wording.
    limit_template = "{format} nesting exceeds the supported limit of {limit} levels"
    stack_template = "{format} parsing exhausted the interpreter stack"

    assert json_analysis(Database(), str(json_path)).diagnostics == (
        ("json-decode-error", limit_template.format(format="JSON", limit=_MAX_JSON_DEPTH)),
    )
    assert xml_config.xml_analysis(Database(), str(xml_path)).diagnostics == (
        ("xml-parse-error", limit_template.format(format="XML", limit=levels)),
    )
    json_stack_message = json_config._STACK_EXHAUSTED_DIAGNOSTIC
    xml_stack_message = xml_config._STACK_EXHAUSTED_DIAGNOSTIC
    assert json_stack_message == stack_template.format(format="JSON")
    assert xml_stack_message == stack_template.format(format="XML")


def test_over_deep_json_reports_the_same_result_at_every_caller_stack_depth(
    tmp_path: Path,
) -> None:
    path = tmp_path / "over-deep.json"
    path.write_text(_nested_json(_MAX_JSON_DEPTH + 101), encoding="utf-8")

    observed: list[tuple[int, tuple[tuple[str, str], ...]]] = []

    def _analyse_at_depth(remaining: int) -> tuple[int, tuple[tuple[str, str], ...]]:
        if remaining:
            return _analyse_at_depth(remaining - 1)
        result = json_analysis(Database(), str(path))
        return (len(result.sections), result.diagnostics)

    def _probe() -> None:
        observed.extend(_analyse_at_depth(pad) for pad in (0, 400, 800))

    # A fresh thread starts at the bottom of its own Python stack, so the limit set
    # here is the whole budget the run gets. Without the cap the scanner descends
    # once per level and this document is accepted from a shallow caller but
    # exhausts the stack from a deep one — the same file, two cached payloads.
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
                    "json-decode-error",
                    f"JSON nesting exceeds the supported limit of {_MAX_JSON_DEPTH} levels",
                ),
            ),
        )
    }


def test_the_section_walk_does_not_consume_the_interpreter_recursion_budget() -> None:
    parsed = _load_json(_nested_json(_MAX_JSON_DEPTH))
    walked: list[int] = []

    def _walk() -> None:
        walked.append(len(_walk_sections(parsed, "")))

    # The document is parsed outside the lowered limit; what is measured here is the
    # walk alone, two orders of magnitude below the document's own nesting.
    original = sys.getrecursionlimit()
    sys.setrecursionlimit(120)
    try:
        thread = threading.Thread(target=_walk)
        thread.start()
        thread.join()
    finally:
        sys.setrecursionlimit(original)

    assert walked == [_MAX_JSON_DEPTH]


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
    shallow = tmp_path / "shallow.json"
    shallow.write_text(_nested_json(3), encoding="utf-8")
    deep = tmp_path / "deep.json"
    deep.write_text(_nested_json(_MAX_JSON_DEPTH), encoding="utf-8")

    db = Database()
    depths: dict[tuple[str, str], int] = {}
    for document in (shallow, deep):
        depths[(document.stem, "sections")] = _snapshot_depth(
            db.get(json_sections_payload, str(document))
        )
        depths[(document.stem, "analysis")] = _snapshot_depth(
            db.get(json_analysis_payload, str(document))
        )

    assert depths == {
        ("shallow", "sections"): 4,
        ("deep", "sections"): 4,
        ("shallow", "analysis"): 5,
        ("deep", "analysis"): 5,
    }, f"document depths 3 and {_MAX_JSON_DEPTH} | {depths}"
    assert max(depths.values()) < _MAX_SNAPSHOT_DEPTH, f"{depths}"

    path = tmp_path / "over-deep.json"
    path.write_text(_nested_json(_MAX_JSON_DEPTH + 1), encoding="utf-8")
    observed: list[Any] = []

    def _recompute() -> None:
        db = Database()
        try:
            json_analysis(db, str(path))
            # The document is scanned again only on recomputation, so the edit is
            # what drives it back through the depth check.
            path.write_text(_nested_json(_MAX_JSON_DEPTH + 1) + " ", encoding="utf-8")
            observed.append(json_analysis(db, str(path)).diagnostics)
        except Exception as exc:
            observed.append(exc)

    # A large stack and a raised limit, so the diagnostic observed below is the
    # cap's own and not stack exhaustion. Both are reported as
    # `json-decode-error`, and only running with budget to spare tells them apart.
    original_limit = sys.getrecursionlimit()
    original_stack = threading.stack_size(64 * 1024 * 1024)
    sys.setrecursionlimit(5000)
    try:
        thread = threading.Thread(target=_recompute)
        thread.start()
        thread.join()
    finally:
        sys.setrecursionlimit(original_limit)
        threading.stack_size(original_stack)

    assert observed == [
        (
            (
                "json-decode-error",
                f"JSON nesting exceeds the supported limit of {_MAX_JSON_DEPTH} levels",
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Lone surrogates
# ---------------------------------------------------------------------------
#
# RFC 8259 permits `\uD800`-style escapes and `json.loads` decodes them, but a
# lone surrogate is not a Unicode scalar value and so cannot cross a cached
# boundary. Whatever the integration reports for such a document, it has to
# report it identically on a first read, after an edit, and from a database
# that never saw the file.


_SURROGATE_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("value", '{"a": "\\ud800"}'),
    ("nested value", '{"a": {"b": ["\\udfff"]}}'),
    ("key", '{"\\ud800": 1}'),
    ("nested key", '{"a": {"\\udfff": 1}}'),
)


@pytest.mark.parametrize(("label", "document"), _SURROGATE_DOCUMENTS)
def test_lone_surrogate_documents_analyze_identically_warm_and_fresh(
    label: str, document: str, tmp_path: Path
) -> None:
    assert json.loads(document) is not None

    path = tmp_path / "config.json"
    path.write_text(document, encoding="utf-8")
    first = json_analysis(Database(), str(path))

    incremental = Database()
    path.write_text('{"a": 1}', encoding="utf-8")
    json_analysis(incremental, str(path))
    # The analysis is redone only on recomputation, so the edit is what drives the
    # surrogate document back through it.
    path.write_text(document, encoding="utf-8")

    assert json_analysis(incremental, str(path)) == first
    assert json_analysis(Database(), str(path)) == first


def test_lone_surrogate_object_key_is_reported_as_a_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"\\ud800": 1}', encoding="utf-8")

    analysis = json_analysis(Database(), str(path))
    assert analysis.sections == ()
    assert [code for code, _message in analysis.diagnostics] == ["json-decode-error"]
    assert "surrogate" in analysis.diagnostics[0][1]


def test_lone_surrogate_value_is_analyzed_through_its_escaped_repr(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"a": "\\ud800"}', encoding="utf-8")

    analysis = json_analysis(Database(), str(path))
    assert analysis.diagnostics == ()
    assert [(key.key, key.string_value) for key in analysis.sections[0].keys] == [
        ("a", "'\\ud800'")
    ]


# ---------------------------------------------------------------------------
# Amplification budget
# ---------------------------------------------------------------------------


# Every section re-emits the dot path of all its ancestors, once as its own name
# and again in its parent's `subsections`, so the cached payload grows
# quadratically in nesting depth and linearly in key length. `_MAX_JSON_DEPTH`
# holds the worst case the budget covers — a key of `_BUDGETED_KEY_LENGTH`
# characters at the cap — under this ceiling, the same ceiling `xml_config` uses.
_SECTIONS_PAYLOAD_BUDGET = 1024 * 1024
_BUDGETED_KEY_LENGTH = 20


def test_sections_payload_at_the_cap_stays_within_the_amplification_budget() -> None:
    text = _nested_json(_MAX_JSON_DEPTH, key="k" * _BUDGETED_KEY_LENGTH)
    sections = _walk_sections(_load_json(text), "")

    assert len(sections) == _MAX_JSON_DEPTH
    assert len(repr(tuple(sections))) < _SECTIONS_PAYLOAD_BUDGET


# The cap is not a size bound, and the two cells below are the two ways past it.
# Both measure the payload the way the cell above does and against the same
# constant: `_walk_sections` over the parsed document, which is exactly the tuple
# `json_sections_payload` caches. Both documents are accepted with no
# diagnostics, so the budget is a property of the cap's own rationale rather than
# of everything the integration takes.
#
# Width first. This document is two levels deep, two orders of magnitude inside
# `_MAX_JSON_DEPTH`, and its payload runs over the budget on sibling count alone.
_WIDE_SIBLING_COUNT = 20_000

# Then key length, at the cap. The payload scales linearly in key length on top of
# the quadratic depth term, so a key long enough past the length the cap rationale
# measures takes a document at the cap over the same budget.
_OVER_BUDGET_KEY_LENGTH = 36


def _wide_shallow_json() -> str:
    """One object holding `_WIDE_SIBLING_COUNT` sibling objects, one scalar each."""
    siblings = {
        f"s{index:0{_BUDGETED_KEY_LENGTH - 1}d}": {"leaf": 1}
        for index in range(_WIDE_SIBLING_COUNT)
    }
    return json.dumps(siblings)


def test_a_wide_shallow_document_is_accepted_and_caches_over_the_budget(
    tmp_path: Path,
) -> None:
    text = _wide_shallow_json()
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")

    analysis = json_analysis(Database(), str(path))
    sections = _walk_sections(_load_json(text), "")

    assert _text_nesting_depth(text) == 2
    assert analysis.diagnostics == ()
    assert len(analysis.sections) == _WIDE_SIBLING_COUNT + 1
    assert len(repr(tuple(sections))) > _SECTIONS_PAYLOAD_BUDGET


def test_a_document_at_the_cap_with_long_keys_is_accepted_and_caches_over_the_budget(
    tmp_path: Path,
) -> None:
    text = _nested_json(_MAX_JSON_DEPTH, key="k" * _OVER_BUDGET_KEY_LENGTH)
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")

    analysis = json_analysis(Database(), str(path))
    sections = _walk_sections(_load_json(text), "")

    assert analysis.diagnostics == ()
    assert len(analysis.sections) == _MAX_JSON_DEPTH
    assert len(repr(tuple(sections))) > _SECTIONS_PAYLOAD_BUDGET


# ---------------------------------------------------------------------------
# Stack exhaustion
# ---------------------------------------------------------------------------


def test_stack_exhaustion_diagnostic_does_not_vary_with_the_recursion_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}', encoding="utf-8")

    # CPython names whichever frame ran out of budget, which is a property of the
    # call site rather than of the file. A cached payload must not carry it.
    messages = (
        "maximum recursion depth exceeded",
        "maximum recursion depth exceeded while decoding a JSON object from a unicode string",
        "maximum recursion depth exceeded while decoding a JSON array from a unicode string",
    )

    observed = set()
    for message in messages:

        def _exhaust_the_stack(_text: str, _message: str = message) -> Any:
            raise RecursionError(_message)

        monkeypatch.setattr(json_config, "_load_json", _exhaust_the_stack)
        observed.add(json_analysis(Database(), str(path)).diagnostics)

    assert observed == {(("json-decode-error", "JSON parsing exhausted the interpreter stack"),)}


def test_nesting_limit_diagnostic_matches_fresh_recomputation(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    steps: tuple[tuple[str, str], ...] = (
        ("shallow", '{"a": 1}'),
        ("at the cap", _nested_json(_MAX_JSON_DEPTH)),
        ("past the cap", _nested_json(_MAX_JSON_DEPTH + 1)),
        ("far past the cap", _OVER_DEEP_JSON),
        ("shallow again", '{"a": 1}'),
    )

    incremental = Database()
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database()
        assert json_analysis(incremental, str(path)) == json_analysis(fresh, str(path))


# ---------------------------------------------------------------------------
# From-scratch oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_json_analysis_matches_fresh_recomputation_over_changes(mode: str, tmp_path: Path) -> None:
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
