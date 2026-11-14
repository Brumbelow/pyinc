"""A request repeated with nothing changed runs no query body again.

A record marked untracked is never trusted, so every direct reader of one runs
again on each request. The only reads the integrations mark that way are the
live `sys.path` list, which the integration contract declares untracked, and
the `.pth` exec lines that `_pth_directives_payload` marks. Everything else
these requests compute is a function of tracked file reads and must be reused.

The families are excluded by query name. `db.query_profile()` names the query
behind every body run since `db.reset_statistics()`, executed or backdated. The
families are the two untracked reads and the two queries that read them
directly, which re-run and then backdate, so nothing above them runs again.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pyinc import Database
from pyinc.integrations.python_source import workspace_analysis
from pyinc.integrations.symbol_resolution import (
    resolve_qualified_name,
    workspace_symbol_index,
)

_SYS_PATH_AND_PTH_FAMILIES = tuple(
    f"pyinc.integrations.deep_module_resolution:{name}["
    for name in (
        "_raw_sys_path_entries",
        "_pth_directives_payload",
        "_effective_search_paths_payload",
        "_all_pth_directives_payload",
    )
)


def _runs_outside_the_families(db: Database, request: Callable[[], object]) -> dict[str, int]:
    db.reset_statistics()
    request()
    stats = db.statistics()
    runs = {profile.query_label: profile.execution_count for profile in db.query_profile()}
    # The profile accounts for every body run the counters saw.
    assert sum(runs.values()) == stats.query_executions + stats.query_backdates
    return {
        label: count
        for label, count in runs.items()
        if not label.startswith(_SYS_PATH_AND_PTH_FAMILIES)
    }


def _write_workspace(root: Path) -> None:
    root.mkdir()
    (root / "flags.py").write_text(
        "import sys\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from pathlib import Path\n"
        "if sys.version_info >= (3, 12):\n"
        "    FAST = True\n"
        "else:\n"
        "    FAST = False\n",
        encoding="utf-8",
    )
    (root / "loop.py").write_text(
        "NAMES = []\nfor _name in ('a', 'b'):\n    NAMES.append(_name)\n",
        encoding="utf-8",
    )
    (root / "provider.py").write_text(
        "shown = 1\nextra = 2\n__all__ = ['shown']\n__all__ += ['extra']\n",
        encoding="utf-8",
    )
    (root / "reexport.py").write_text("from loop import *\n", encoding="utf-8")
    (root / "consumer.py").write_text(
        "import json\n"
        "import pytest\n"
        "import loop\n"
        "from flags import FAST\n"
        "from provider import *\n"
        "from reexport import *\n"
        "def use() -> object:\n"
        "    return (json, pytest, loop, FAST, shown)\n",
        encoding="utf-8",
    )


def test_a_no_change_workspace_analysis_runs_no_query_again(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_workspace(root)
    db = Database(mode="strict")

    def request() -> tuple[object, object]:
        return workspace_analysis(db, root), workspace_symbol_index(db, root)

    first = request()
    assert _runs_outside_the_families(db, request) == {}
    assert request() == first
    fresh = Database(mode="strict")
    assert (workspace_analysis(fresh, root), workspace_symbol_index(fresh, root)) == first


def test_a_no_change_ambiguous_symbol_resolution_runs_no_query_again(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_workspace(root)
    (root / "left.py").write_text("from right import thing\n", encoding="utf-8")
    (root / "right.py").write_text("from left import thing\n", encoding="utf-8")
    (root / "one.py").write_text("value = 1\n", encoding="utf-8")
    (root / "two.py").write_text("value = 2\n", encoding="utf-8")
    (root / "both.py").write_text("from one import *\nfrom two import *\n", encoding="utf-8")
    lookups = (
        (root / "left.py", "thing"),
        (root / "both.py", "value"),
        (root / "consumer.py", "not_exported"),
    )
    db = Database(mode="strict")

    def request() -> tuple[str, ...]:
        return tuple(
            resolve_qualified_name(db, root, path, name).resolution for path, name in lookups
        )

    first = request()
    assert first == ("ambiguous", "ambiguous", "ambiguous")
    assert _runs_outside_the_families(db, request) == {}
    assert request() == first
