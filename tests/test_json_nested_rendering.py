from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyinc import Database, InMemoryArtifactStore
from pyinc.integrations import json_config
from pyinc.integrations.json_config import (
    json_analysis_payload,
    json_file_text,
    json_sections_payload,
)

_MODES = ("strict", "checked", "fast")


def _reverse_object_order(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reverse_object_order(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_object_order(item) for item in value]
    return value


def _nested_array_document() -> tuple[dict[str, Any], dict[str, Any]]:
    original: dict[str, Any] = {
        "items": [
            {
                "alpha": 1,
                "nested": {
                    "first": [{"left": True, "right": None}],
                    "second": 2,
                },
            }
        ]
    }
    return original, _reverse_object_order(original)


@pytest.mark.parametrize("mode", _MODES)
def test_nested_object_reorder_has_canonical_public_rendering(
    mode: str,
    tmp_path: Path,
) -> None:
    original, reordered = _nested_array_document()
    path = tmp_path / "nested.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    warm_db = Database(mode=mode)
    before = warm_db.get(json_analysis_payload, str(path))

    path.write_text(json.dumps(reordered, indent=2), encoding="utf-8")
    warm = warm_db.get(json_analysis_payload, str(path))
    fresh = Database(mode=mode).get(json_analysis_payload, str(path))

    assert json_config._json_cutoff_token(json.dumps(original)) == (
        json_config._json_cutoff_token(json.dumps(reordered))
    )
    assert warm == fresh == before
    root_keys = dict((key[1], key[3]) for key in warm[1][0][1])
    assert root_keys["items"] == (
        "[[('alpha', 1), ('nested', [('first', [[('left', True), "
        "('right', None)]]), ('second', 2)])]]"
    )
    assert warm_db.inspect(json_file_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(json_sections_payload, str(path)).last_recompute == "backdated"
    assert warm_db.inspect(json_analysis_payload, str(path)).last_decision == "reused"


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_reorder_matches_fresh_at_arbitrary_depth(
    mode: str,
    tmp_path: Path,
) -> None:
    original, reordered = _nested_array_document()
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    initial = writer.get(json_analysis_payload, str(path))
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(json_analysis_payload, str(path)) == initial

    path.write_text(json.dumps(reordered), encoding="utf-8")
    warm = reader.get(json_analysis_payload, str(path))
    fresh = Database(mode=mode).get(json_analysis_payload, str(path))

    assert warm == fresh == initial
    assert reader.inspect(json_file_text, str(path)).last_recompute == "executed"


_JSON_VALUES = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-100, 100), st.text(max_size=8)),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=6), children, max_size=4),
    ),
    max_leaves=30,
)


@settings(max_examples=100, deadline=None)
@given(_JSON_VALUES)
def test_equal_tokens_imply_equal_complete_nested_public_payload(value: Any) -> None:
    original = {"value": value}
    reordered = _reverse_object_order(original)
    original_text = json.dumps(original)
    reordered_text = json.dumps(reordered, indent=2)

    assert json_config._json_cutoff_token(original_text) == (
        json_config._json_cutoff_token(reordered_text)
    )
    assert tuple(json_config._walk_sections(original, "")) == tuple(
        json_config._walk_sections(reordered, "")
    )
