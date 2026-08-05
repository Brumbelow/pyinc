from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from pyinc import Database, InMemoryArtifactStore, query
from pyinc.integrations.python_source import (
    file_analysis,
    import_statements_for_file,
    source_ranges_for_file,
    source_text,
)
from pyinc.integrations.scope_resolution import scope_tree, scope_tree_payload

Mode = Literal["strict", "checked", "fast"]
_MODES: tuple[Mode, ...] = ("strict", "checked", "fast")


@query(key="python-source-exact-text-observation-v1")
def _exact_text_observation(db: Database, path: str) -> tuple[str, int]:
    text = source_text(db, path)
    return text, len(text)


def _write_exact(path: Path, source: str) -> None:
    path.write_bytes(source.encode("utf-8"))


def _root_end(db: Database, path: Path) -> tuple[int, int]:
    root = next(scope for scope in scope_tree(db, path).scopes if scope.id == "module")
    return root.range.end.line, root.range.end.character


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("initial", "updated"),
    (
        pytest.param("value = 1\n", "value = 1\n# trailing\n", id="trailing-comment"),
        pytest.param("value = 1\n", "\nvalue = 1\n", id="leading-whitespace"),
        pytest.param("value = 1", "value = 1   ", id="trailing-whitespace"),
        pytest.param("value = 1\n", "value = 1\r\n", id="line-ending"),
        pytest.param("value = 1", "value = 1\n", id="final-newline"),
    ),
)
def test_exact_source_edits_execute_arbitrary_raw_consumers_and_match_fresh(
    mode: Mode,
    initial: str,
    updated: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "module.py"
    _write_exact(path, initial)
    warm_db = Database(mode=mode)
    assert warm_db.get(_exact_text_observation, str(path)) == (initial, len(initial))

    _write_exact(path, updated)
    warm = warm_db.get(_exact_text_observation, str(path))
    fresh = Database(mode=mode).get(_exact_text_observation, str(path))

    assert warm == fresh == (updated, len(updated))
    assert warm_db.inspect(source_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(_exact_text_observation, str(path)).last_recompute == "executed"


@pytest.mark.parametrize("mode", _MODES)
def test_equal_complete_python_payloads_backdate_across_spelling_edits(
    mode: Mode, tmp_path: Path
) -> None:
    path = tmp_path / "module.py"
    initial = "import os\n"
    equivalent = "import os  # comment\r\n"
    _write_exact(path, initial)
    warm_db = Database(mode=mode)

    first_imports = warm_db.get(import_statements_for_file, str(path))
    first_ranges = warm_db.get(source_ranges_for_file, str(path))
    first_scope = warm_db.get(scope_tree_payload, str(path))
    first_public_scope = scope_tree(warm_db, path)

    _write_exact(path, equivalent)
    warm_imports = warm_db.get(import_statements_for_file, str(path))
    warm_ranges = warm_db.get(source_ranges_for_file, str(path))
    warm_scope = warm_db.get(scope_tree_payload, str(path))
    warm_public_scope = scope_tree(warm_db, path)
    fresh_db = Database(mode=mode)

    assert warm_imports == fresh_db.get(import_statements_for_file, str(path)) == first_imports
    assert warm_ranges == fresh_db.get(source_ranges_for_file, str(path)) == first_ranges
    assert warm_scope == fresh_db.get(scope_tree_payload, str(path)) == first_scope
    assert warm_public_scope == scope_tree(fresh_db, path) == first_public_scope
    assert warm_db.inspect(source_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(import_statements_for_file, str(path)).last_recompute == "backdated"
    assert warm_db.inspect(source_ranges_for_file, str(path)).last_recompute == "backdated"
    assert warm_db.inspect(scope_tree_payload, str(path)).last_recompute == "backdated"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("initial", "updated", "ranges_change", "expected_root_end"),
    (
        pytest.param(
            "def f():\n    return 1\n",
            "def f():\n    return 1\n# trailing\n",
            False,
            (3, 0),
            id="trailing-comment-resizes-document",
        ),
        pytest.param(
            "def f():\n    return 1\n",
            "\n\ndef f():\n    return 1\n",
            True,
            (4, 0),
            id="leading-lines-move-ranges",
        ),
        pytest.param(
            "def f():\n    return 1",
            "def f():\n    return 1   ",
            False,
            (1, 15),
            id="trailing-whitespace-resizes-document",
        ),
        pytest.param(
            "def f():\n    return 1",
            "def f():\n    return 1\n",
            False,
            (2, 0),
            id="final-newline-resizes-document",
        ),
    ),
)
def test_python_ranges_and_scope_geometry_match_fresh_after_exact_edits(
    mode: Mode,
    initial: str,
    updated: str,
    ranges_change: bool,
    expected_root_end: tuple[int, int],
    tmp_path: Path,
) -> None:
    path = tmp_path / "module.py"
    _write_exact(path, initial)
    warm_db = Database(mode=mode)
    first_ranges = warm_db.get(source_ranges_for_file, str(path))
    first_scope = warm_db.get(scope_tree_payload, str(path))
    first_analysis = file_analysis(warm_db, path)

    _write_exact(path, updated)
    warm_ranges = warm_db.get(source_ranges_for_file, str(path))
    warm_scope = warm_db.get(scope_tree_payload, str(path))
    warm_analysis = file_analysis(warm_db, path)
    fresh_db = Database(mode=mode)
    fresh_ranges = fresh_db.get(source_ranges_for_file, str(path))
    fresh_scope = fresh_db.get(scope_tree_payload, str(path))
    fresh_analysis = file_analysis(fresh_db, path)

    assert warm_ranges == fresh_ranges
    assert warm_scope == fresh_scope
    assert warm_analysis == fresh_analysis
    assert (warm_ranges != first_ranges) is ranges_change
    assert (warm_analysis != first_analysis) is ranges_change
    assert warm_scope != first_scope
    assert warm_db.inspect(source_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(source_ranges_for_file, str(path)).last_recompute == (
        "executed" if ranges_change else "backdated"
    )
    assert warm_db.inspect(scope_tree_payload, str(path)).last_recompute == "executed"
    assert _root_end(warm_db, path) == _root_end(fresh_db, path) == expected_root_end


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("updated", "ranges_change", "expected_root_end"),
    (
        pytest.param(
            "def value():\n    return 1\n# trailing\n",
            False,
            (3, 0),
            id="trailing-comment",
        ),
        pytest.param(
            "\n\ndef value():\n    return 1\n",
            True,
            (4, 0),
            id="leading-lines",
        ),
    ),
)
def test_checkpoint_reload_revalidates_exact_source_ranges_and_scope_geometry(
    mode: Mode,
    updated: str,
    ranges_change: bool,
    expected_root_end: tuple[int, int],
    tmp_path: Path,
) -> None:
    path = tmp_path / "module.py"
    initial = "def value():\n    return 1\n"
    _write_exact(path, initial)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(_exact_text_observation, str(path)) == (initial, len(initial))
    initial_ranges = writer.get(source_ranges_for_file, str(path))
    initial_scope = writer.get(scope_tree_payload, str(path))
    initial_analysis = file_analysis(writer, path)
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    _write_exact(path, updated)

    warm_text = reader.get(_exact_text_observation, str(path))
    warm_ranges = reader.get(source_ranges_for_file, str(path))
    warm_scope = reader.get(scope_tree_payload, str(path))
    warm_analysis = file_analysis(reader, path)
    fresh_db = Database(mode=mode)

    assert (
        warm_text
        == fresh_db.get(_exact_text_observation, str(path))
        == (
            updated,
            len(updated),
        )
    )
    assert warm_ranges == fresh_db.get(source_ranges_for_file, str(path))
    assert warm_scope == fresh_db.get(scope_tree_payload, str(path))
    assert warm_analysis == file_analysis(fresh_db, path)
    assert (warm_ranges != initial_ranges) is ranges_change
    assert warm_scope != initial_scope
    assert (warm_analysis != initial_analysis) is ranges_change
    assert _root_end(reader, path) == _root_end(fresh_db, path) == expected_root_end
    assert reader.inspect(source_text, str(path)).last_recompute == "executed"
    assert reader.inspect(_exact_text_observation, str(path)).last_recompute == "executed"
    if ranges_change:
        assert reader.inspect(source_ranges_for_file, str(path)).last_recompute == "executed"
    assert reader.inspect(scope_tree_payload, str(path)).last_recompute == "executed"
