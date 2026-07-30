"""A warm workspace request must not redo per-file work for unchanged files.

The counters here are call counts of the real functions, collected with a trace
hook rather than by monkeypatching: pyinc's query layer fingerprints every
callable a query transitively reaches and folds in any mutable state it closes
over, so a counting stand-in would become part of a query's identity and change
it on every increment (see `tests/test_source_ranges_caching.py`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

import pyinc_tools.session as session_module
from pyinc.integrations.python_source import workspace_analysis
from pyinc.integrations.scope_resolution import _decode_scope_tree
from pyinc.integrations.symbol_resolution import _placed_module_symbol_table, find_references
from pyinc_tools import WorkspaceSession

_TraceFunc = Callable[[FrameType, str, Any], Any]


class _SysTrace:
    """Adapts sys.settrace to monkeypatch's setattr/undo protocol."""

    @property
    def current(self) -> Any:
        return sys.gettrace()

    @current.setter
    def current(self, value: _TraceFunc | None) -> None:
        sys.settrace(value)


_sys_trace = _SysTrace()


def _count_calls(monkeypatch: pytest.MonkeyPatch, *functions: Any) -> dict[str, int]:
    counter = {"n": 0}
    codes = {function.__code__ for function in functions}

    def tracer(frame: FrameType, event: str, arg: Any) -> _TraceFunc:
        if event == "call" and frame.f_code in codes:
            counter["n"] += 1
        return tracer

    monkeypatch.setattr(_sys_trace, "current", tracer)
    return counter


def _write_workspace(root: Path) -> None:
    (root / "alpha.py").write_text(
        "def one():\n    return 1\n\n\ndef two():\n    return 2\n",
        encoding="utf-8",
    )
    (root / "beta.py").write_text(
        "from alpha import one\n\n\ndef three():\n    return one()\n",
        encoding="utf-8",
    )
    (root / "gamma.py").write_text(
        "from alpha import two\n\n\ndef four():\n    return two()\n",
        encoding="utf-8",
    )


def _codes(session: WorkspaceSession, name: str) -> list[str]:
    result = session.analyze_workspace()
    return sorted(
        diagnostic.code
        for diagnostic in result.diagnostics
        if Path(diagnostic.path).name == name
    )


def test_unchanged_rerequest_decodes_no_file_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        session.analyze_workspace()
        counter = _count_calls(monkeypatch, _decode_scope_tree, _placed_module_symbol_table)
        session.analyze_workspace()
        assert counter["n"] == 0


def test_edit_decodes_only_the_edited_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        session.analyze_workspace()
        (tmp_path / "beta.py").write_text(
            "from alpha import one\n\n\ndef three():\n    return one() + 1\n",
            encoding="utf-8",
        )
        session.refresh_paths(("beta.py",))
        counter = _count_calls(monkeypatch, _decode_scope_tree, _placed_module_symbol_table)
        session.analyze_workspace()
        # beta's scope tree and symbol table are the only payloads that moved,
        # so they are the only two decodes. alpha and gamma keep theirs.
        assert counter["n"] == 2


def test_overlay_edit_decodes_only_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        session.analyze_workspace()
        session.set_overlay(
            "gamma.py",
            "from alpha import two\n\n\ndef four():\n    return two() + 1\n",
        )
        counter = _count_calls(monkeypatch, _decode_scope_tree, _placed_module_symbol_table)
        session.analyze_workspace()
        assert counter["n"] == 2


def _count_workspace_analysis_fetches(
    root: Path, importers: int, monkeypatch: pytest.MonkeyPatch
) -> int:
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpha.py").write_text(
        "def one():\n    return 1\n\n\ndef two():\n    return 2\n", encoding="utf-8"
    )
    for index in range(importers):
        (root / f"mod{index}.py").write_text(
            f"from alpha import one\n\n\ndef use{index}():\n    return one()\n",
            encoding="utf-8",
        )
    with WorkspaceSession(root) as session:
        session.analyze_workspace()
        counter = _count_calls(monkeypatch, workspace_analysis)
        session.analyze_workspace()
        return counter["n"]


def test_workspace_analysis_fetches_do_not_scale_with_the_file_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every file that imports a workspace name has to know which of its own
    # names other modules re-export. That used to walk the workspace analysis
    # once per file; now the request walks it once for all of them.
    small = _count_workspace_analysis_fetches(tmp_path / "small", 2, monkeypatch)
    large = _count_workspace_analysis_fetches(tmp_path / "large", 12, monkeypatch)
    assert small == large


def test_unused_import_check_does_not_scan_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        # beta imports `one` and stops using it: the unused-import walk runs.
        session.set_overlay("beta.py", "from alpha import one\n\n\ndef three():\n    return 3\n")
        session.analyze_workspace()
        counter = _count_calls(monkeypatch, find_references)
        assert _codes(session, "beta.py") == ["unused-import"]
        # The answer only ever depended on beta's own occurrences.
        assert counter["n"] == 0


def test_cached_results_still_track_cross_file_changes(tmp_path: Path) -> None:
    """Caching must not hide a diagnostic that another file's edit creates."""

    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        assert _codes(session, "beta.py") == []
        # Dropping `one` from alpha breaks beta's import even though beta itself
        # was never touched.
        session.set_overlay("alpha.py", "def two():\n    return 2\n")
        assert _codes(session, "beta.py") == ["unresolved-symbol"]
        session.set_overlay("alpha.py", "def one():\n    return 1\n\n\ndef two():\n    return 2\n")
        assert _codes(session, "beta.py") == []


def test_cached_results_track_reexport_changes(tmp_path: Path) -> None:
    """`unused-import` depends on whether another module re-imports the name."""

    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        # beta imports `one` but stops using it: unused, and nobody re-exports it.
        session.set_overlay("beta.py", "from alpha import one\n\n\ndef three():\n    return 3\n")
        assert _codes(session, "beta.py") == ["unused-import"]
        # gamma now re-imports `one` from beta, so beta's binding is a re-export.
        session.set_overlay("gamma.py", "from beta import one\n\n\ndef four():\n    return one()\n")
        assert _codes(session, "beta.py") == []
        session.set_overlay("gamma.py", "from alpha import two\n\n\ndef four():\n    return two()\n")
        assert _codes(session, "beta.py") == ["unused-import"]


def test_workspace_result_matches_a_cold_session(tmp_path: Path) -> None:
    """A warm cached request returns what a fresh session computes from scratch."""

    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        session.analyze_workspace()
        (tmp_path / "beta.py").write_text(
            "from alpha import one\n\n\ndef three():\n    return 3\n",
            encoding="utf-8",
        )
        session.refresh_paths(("beta.py",))
        warm = session.analyze_workspace()
    with WorkspaceSession(tmp_path) as fresh_session:
        cold = fresh_session.analyze_workspace()

    def normalize(
        result: session_module.WorkspaceAnalysisResult,
    ) -> tuple[Any, ...]:
        return (
            tuple(sorted((d.path, d.code, d.message, d.severity) for d in result.diagnostics)),
            tuple(sorted(file_result.path for file_result in result.files)),
            tuple(
                sorted(
                    (symbol.qualified_name, symbol.range.start.line)
                    for file_result in result.files
                    if file_result.symbols is not None
                    for symbol in file_result.symbols.symbols
                )
            ),
        )

    assert normalize(warm) == normalize(cold)
