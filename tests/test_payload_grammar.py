"""Payloads the layer-3 entrypoints read must be tuples of primitives.

Those entrypoints index `db.get(...)` directly instead of thawing it first. That
is only correct while every payload freezes to plain tuples: `freeze` leaves
tuples and primitives alone, but a payload aliased as a `dict`, `list`, or `set`
would come back from a strict-mode `db.get` as a `FrozenDict`/`FrozenList`/
`FrozenSet` and reach a decoder that indexes it as a tuple. Nothing in the type
system says a payload alias may not grow such a field, so it is asserted here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pyinc import Database
from pyinc.core import Query
from pyinc.integrations.python_source import (
    directory_analysis_payload,
    file_analysis_payload,
    module_analysis_payload,
    source_ranges_for_file,
    workspace_analysis_payload,
    workspace_python_files,
)
from pyinc.integrations.scope_resolution import scope_tree_payload
from pyinc.integrations.symbol_resolution import (
    _resolve_symbol_payload,
    module_symbol_table_for_module,
    resolved_class_model_payload,
    workspace_symbol_index_payload,
)

_PRIMITIVES = (str, int, bool, float, bytes, type(None))


def _write_workspace(root: Path) -> None:
    (root / "alpha.py").write_text(
        "class Box:\n    x = 1\n\n\ndef one():\n    return 1\n", encoding="utf-8"
    )
    (root / "beta.py").write_text(
        "from alpha import one\n\n\ndef two():\n    return one()\n", encoding="utf-8"
    )


def _offending_types(value: Any, trail: str) -> list[str]:
    if type(value) is tuple:
        found: list[str] = []
        for index, item in enumerate(value):
            found.extend(_offending_types(item, f"{trail}[{index}]"))
        return found
    if type(value) in _PRIMITIVES:
        return []
    return [f"{trail}: {type(value).__module__}.{type(value).__qualname__}"]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    _write_workspace(tmp_path)
    return tmp_path


def _cases(root: Path) -> list[tuple[Query[Any, Any], tuple[Any, ...]]]:
    alpha = str(root / "alpha.py")
    beta = str(root / "beta.py")
    return [
        (file_analysis_payload, (alpha,)),
        (directory_analysis_payload, (str(root),)),
        (module_analysis_payload, (str(root), alpha)),
        (workspace_analysis_payload, (str(root),)),
        (workspace_python_files, (str(root),)),
        (source_ranges_for_file, (alpha,)),
        (scope_tree_payload, (alpha,)),
        (module_symbol_table_for_module, (str(root), alpha)),
        (workspace_symbol_index_payload, (str(root),)),
        (_resolve_symbol_payload, (str(root), beta, "one")),
        (resolved_class_model_payload, (str(root), alpha, "Box")),
    ]


def test_every_read_payload_is_tuples_of_primitives(workspace: Path) -> None:
    db = Database(mode="strict")
    offenders: dict[str, list[str]] = {}
    for query, args in _cases(workspace):
        snapshot = db.get(query, *args)
        found = _offending_types(snapshot, query.key)
        if found:
            offenders[query.key] = found
    assert offenders == {}


def test_the_scan_would_catch_a_non_tuple_payload() -> None:
    # Without this the test above passes trivially if the walk is ever broken.
    assert _offending_types(("a", 1, (True, None)), "root") == []
    assert _offending_types(("a", {"k": "v"}), "root") == ["root[1]: builtins.dict"]
    assert _offending_types(["a"], "root") == ["root: builtins.list"]
