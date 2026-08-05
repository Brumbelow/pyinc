from __future__ import annotations

import math
import struct
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyinc import Database, InMemoryArtifactStore, query
from pyinc.integrations import toml_config
from pyinc.integrations.toml_config import (
    ConfigAnalysis,
    config_analysis,
    config_analysis_payload,
    config_file_text,
    config_sections_payload,
    workspace_config_analysis,
)

_TABLE_VALUE = "value = { a = 1 }\n"
_ARRAY_LOOKALIKE = 'value = [["a", 1]]\n'
_DATETIME_VALUE = "value = 1979-05-27T07:32:00Z\n"
_DATETIME_ARRAY_LOOKALIKE = 'value = ["datetime", "1979-05-27T07:32:00+00:00"]\n'
_NESTED_TABLE_BA = "value = [{ outer = { b = 2, a = 1 } }]\n"
_NESTED_TABLE_AB = "value = [{ outer = { a = 1, b = 2 } }]\n"


@query(key="toml-tagged-projection-deep-public-strings")
def _deep_public_strings(db: Database, path: str) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        key
        for _section_name, keys, _subsections in config_sections_payload(db, path)
        for key in keys
    )


def _observe(
    db: Database,
    path: Path,
) -> tuple[str, tuple[tuple[str, str, str, str], ...], object, ConfigAnalysis, ConfigAnalysis]:
    direct = config_analysis(db, path)
    workspace = workspace_config_analysis(db, path.parent)
    assert workspace is not None
    return (
        db.get(config_file_text, str(path)),
        db.get(_deep_public_strings, str(path)),
        db.get(config_analysis_payload, str(path)),
        direct,
        workspace,
    )


def _key(result: ConfigAnalysis, name: str) -> tuple[str, str]:
    for section in result.sections:
        for key in section.keys:
            if key.key == name:
                return key.value_type, key.string_value
    raise AssertionError(f"missing key {name!r}")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (_TABLE_VALUE, _ARRAY_LOOKALIKE),
        (_DATETIME_VALUE, _DATETIME_ARRAY_LOOKALIKE),
        ("value = 1\n", "value = 1.0\n"),
        ("value = true\n", "value = 1\n"),
        ("value = 0.0\n", "value = -0.0\n"),
    ],
    ids=["table-array", "datetime-array", "integer-float", "boolean-integer", "float-zero"],
)
def test_toml_projection_distinguishes_typed_lookalikes(left: str, right: str) -> None:
    left_token = toml_config._config_cutoff_token(left)
    right_token = toml_config._config_cutoff_token(right)

    assert left_token[0] == right_token[0] == "parsed"
    assert left_token != right_token


def test_toml_projection_tags_every_scalar_and_container_kind() -> None:
    positive_nan = float("nan")
    negative_nan = math.copysign(positive_nan, -1.0)
    values: tuple[object, ...] = (
        False,
        0,
        0.0,
        -0.0,
        float("inf"),
        float("-inf"),
        positive_nan,
        negative_nan,
        "0",
        date(1979, 5, 27),
        time(7, 32),
        datetime(1979, 5, 27, 7, 32, tzinfo=UTC),
        ["date", "1979-05-27"],
        {"date": "1979-05-27"},
    )

    projections = tuple(toml_config._toml_cutoff_value(value) for value in values)

    assert len(set(projections)) == len(projections)
    assert (
        toml_config._toml_value_to_string(
            [0.0, -0.0, positive_nan, negative_nan, float("inf"), float("-inf")]
        )
        == "[0.0, -0.0, nan, -nan, inf, -inf]"
    )


def test_nested_inline_table_order_has_one_token_and_one_public_string(tmp_path: Path) -> None:
    left = toml_config._load_toml(_NESTED_TABLE_BA)
    right = toml_config._load_toml(_NESTED_TABLE_AB)

    assert toml_config._toml_cutoff_value(left) == toml_config._toml_cutoff_value(right)
    assert toml_config._toml_value_to_string(left["value"]) == toml_config._toml_value_to_string(
        right["value"]
    )

    path = tmp_path / "pyproject.toml"
    path.write_text(_NESTED_TABLE_BA, encoding="utf-8")
    first = config_analysis(Database(), path)
    path.write_text(_NESTED_TABLE_AB, encoding="utf-8")
    second = config_analysis(Database(), path)

    assert (
        _key(first, "value")
        == _key(second, "value")
        == (
            "array",
            "[{'outer': {'a': 1, 'b': 2}}]",
        )
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_toml_adversarial_transitions_match_fresh_at_every_consumer(
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    warm = Database(mode=mode)
    previous_nested: (
        tuple[
            str,
            tuple[tuple[str, str, str, str], ...],
            object,
            ConfigAnalysis,
            ConfigAnalysis,
        ]
        | None
    ) = None

    for text in (
        _TABLE_VALUE,
        _ARRAY_LOOKALIKE,
        _DATETIME_VALUE,
        _DATETIME_ARRAY_LOOKALIKE,
        _NESTED_TABLE_BA,
        _NESTED_TABLE_AB,
        _TABLE_VALUE,
    ):
        path.write_text(text, encoding="utf-8")
        warm_observation = _observe(warm, path)
        fresh_observation = _observe(Database(mode=mode), path)

        assert warm_observation == fresh_observation
        assert warm_observation[0] == text
        if text == _NESTED_TABLE_BA:
            previous_nested = warm_observation
        elif text == _NESTED_TABLE_AB:
            assert previous_nested is not None
            assert warm_observation[1:] == previous_nested[1:]
            assert warm_observation[0] != previous_nested[0]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (_TABLE_VALUE, _ARRAY_LOOKALIKE),
        (_DATETIME_VALUE, _DATETIME_ARRAY_LOOKALIKE),
        (_NESTED_TABLE_BA, _NESTED_TABLE_AB),
    ],
    ids=["table-array", "datetime-array", "nested-table-order"],
)
def test_toml_same_mode_checkpoint_reload_matches_fresh(
    mode: str,
    before: str,
    after: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    store = InMemoryArtifactStore()
    path.write_text(before, encoding="utf-8")
    writer = Database(mode=mode, store=store)
    _observe(writer, path)
    checkpoint = writer.save_checkpoint()

    path.write_text(after, encoding="utf-8")
    loaded = Database(mode=mode, store=store)
    loaded.load_checkpoint(checkpoint)

    loaded_observation = _observe(loaded, path)
    fresh_observation = _observe(Database(mode=mode), path)
    assert loaded_observation == fresh_observation
    assert loaded_observation[0] == after


_TOML_TEXT = st.text(
    alphabet=st.characters(codec="utf-8"),
    max_size=6,
)
_TOML_SCALARS = st.one_of(
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(width=64, allow_nan=True, allow_infinity=True),
    _TOML_TEXT,
    st.dates(),
    st.times(timezones=st.none()).map(lambda value: value.replace(fold=0)),
    st.datetimes(timezones=st.one_of(st.none(), st.just(UTC))).map(
        lambda value: value.replace(fold=0)
    ),
)


def _extend_toml_values(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    return st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_TOML_TEXT, children, max_size=4),
    )


_TOML_VALUES = st.recursive(
    _TOML_SCALARS,
    _extend_toml_values,
    max_leaves=20,
)


def _reverse_table_order(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reverse_table_order(value[key]) for key in reversed(tuple(value))}
    if isinstance(value, list):
        return [_reverse_table_order(item) for item in value]
    return value


def _typed_toml_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        return struct.pack(">d", left) == struct.pack(">d", right)
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _typed_toml_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _typed_toml_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


@settings(max_examples=150, deadline=None)
@given(value=_TOML_VALUES)
def test_toml_projection_and_rendering_ignore_mapping_order_at_arbitrary_depth(
    value: Any,
) -> None:
    reordered = _reverse_table_order(value)

    assert toml_config._toml_cutoff_value(value) == toml_config._toml_cutoff_value(reordered)
    assert toml_config._toml_value_to_string(value) == toml_config._toml_value_to_string(reordered)


@settings(max_examples=200, deadline=None)
@given(left=_TOML_VALUES, right=_TOML_VALUES)
def test_equal_toml_projections_imply_typed_values_and_public_strings_are_equal(
    left: Any,
    right: Any,
) -> None:
    if toml_config._toml_cutoff_value(left) == toml_config._toml_cutoff_value(right):
        assert _typed_toml_equal(left, right)
        assert toml_config._toml_value_to_string(left) == toml_config._toml_value_to_string(right)
