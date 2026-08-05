from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pyinc import Database, InMemoryArtifactStore
from pyinc.integrations import notebook as notebook_module
from pyinc.integrations.notebook import (
    notebook_analysis_payload,
    notebook_cells_payload,
    notebook_diagnostics_payload,
    notebook_text,
)

_MODES = ("strict", "checked", "fast")


def _write_notebook(path: Path, payload: dict[str, Any], *, indent: int | None = None) -> None:
    path.write_text(json.dumps(payload, indent=indent), encoding="utf-8")


@pytest.mark.parametrize("mode", _MODES)
def test_missing_and_empty_cells_have_distinct_tokens_and_warm_results(
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "presence.ipynb"
    missing = {"metadata": {"kernelspec": {"name": "python3"}}}
    empty = {"cells": [], "metadata": {"kernelspec": {"name": "python3"}}}
    assert notebook_module._notebook_cutoff_token(json.dumps(missing)) != (
        notebook_module._notebook_cutoff_token(json.dumps(empty))
    )

    _write_notebook(path, missing)
    warm_db = Database(mode=mode)
    before = warm_db.get(notebook_analysis_payload, str(path))
    assert before[-1] == (("notebook-shape-error", "missing 'cells' field", None),)

    _write_notebook(path, empty)
    warm = warm_db.get(notebook_analysis_payload, str(path))
    fresh = Database(mode=mode).get(notebook_analysis_payload, str(path))

    assert warm == fresh
    assert warm != before
    assert warm[-1] == ()
    assert warm_db.inspect(notebook_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(notebook_diagnostics_payload, str(path)).last_recompute == "executed"


def _formerly_colliding_notebooks() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "cells": [
                None,
                {"cell_type": "code", "source": "invalid-cell"},
            ],
            "metadata": {},
        },
        {
            "cells": [
                {"cell_type": "invalid-cell", "source": "code"},
                None,
            ],
            "metadata": {},
        },
    )


@pytest.mark.parametrize("mode", _MODES)
def test_each_cell_has_one_tagged_position_token(mode: str, tmp_path: Path) -> None:
    first, second = _formerly_colliding_notebooks()
    first_text = json.dumps(first)
    second_text = json.dumps(second)
    first_token = notebook_module._notebook_cutoff_token(first_text)
    second_token = notebook_module._notebook_cutoff_token(second_text)

    assert first_token != second_token
    assert first_token[-1] == (
        "cells",
        "present",
        (
            ("invalid-cell",),
            (
                "cell",
                ("cell-type", "code"),
                ("source", "invalid-cell"),
            ),
        ),
    )

    path = tmp_path / "positions.ipynb"
    _write_notebook(path, first)
    warm_db = Database(mode=mode)
    before = warm_db.get(notebook_analysis_payload, str(path))

    _write_notebook(path, second)
    warm = warm_db.get(notebook_analysis_payload, str(path))
    fresh = Database(mode=mode).get(notebook_analysis_payload, str(path))

    assert warm == fresh
    assert warm != before
    assert warm_db.inspect(notebook_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(notebook_cells_payload, str(path)).last_recompute == "executed"
    assert warm_db.inspect(notebook_diagnostics_payload, str(path)).last_recompute == "executed"


@pytest.mark.parametrize("mode", _MODES)
def test_output_only_edit_backdates_complete_payload_after_exact_raw_change(
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "output.ipynb"
    payload: dict[str, Any] = {
        "cells": [
            {
                "cell_type": "code",
                "source": "value = 1\n",
                "outputs": [],
                "execution_count": None,
            }
        ],
        "metadata": {},
    }
    _write_notebook(path, payload)
    warm_db = Database(mode=mode)
    before = warm_db.get(notebook_analysis_payload, str(path))
    before_token = notebook_module._notebook_cutoff_token(path.read_text(encoding="utf-8"))

    payload["cells"][0]["outputs"] = [
        {"output_type": "stream", "name": "stdout", "text": "noise\n"}
    ]
    payload["cells"][0]["execution_count"] = 7
    _write_notebook(path, payload, indent=2)
    after_token = notebook_module._notebook_cutoff_token(path.read_text(encoding="utf-8"))

    warm = warm_db.get(notebook_analysis_payload, str(path))
    fresh = Database(mode=mode).get(notebook_analysis_payload, str(path))

    assert before_token == after_token
    assert warm == fresh == before
    assert warm_db.inspect(notebook_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(notebook_cells_payload, str(path)).last_recompute == "backdated"
    assert warm_db.inspect(notebook_analysis_payload, str(path)).last_decision == "reused"


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_cannot_restore_across_a_former_cell_token_collision(
    mode: str,
    tmp_path: Path,
) -> None:
    first, second = _formerly_colliding_notebooks()
    path = tmp_path / "checkpoint.ipynb"
    _write_notebook(path, first)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    initial = writer.get(notebook_analysis_payload, str(path))
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(notebook_analysis_payload, str(path)) == initial

    _write_notebook(path, second)
    warm = reader.get(notebook_analysis_payload, str(path))
    fresh = Database(mode=mode).get(notebook_analysis_payload, str(path))

    assert warm == fresh
    assert warm != initial
    assert reader.inspect(notebook_text, str(path)).last_recompute == "executed"
