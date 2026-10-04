"""A warm workspace request must not redo per-file work for unchanged files.

The counters here are call counts of the real functions, collected with a trace
hook rather than by monkeypatching: pyinc's query layer fingerprints every
callable a query transitively reaches and folds in any mutable state it closes
over, so a counting stand-in would become part of a query's identity and change
it on every increment (see `tests/test_source_ranges_caching.py`).
"""

from __future__ import annotations

import contextvars
import gc
import sys
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import pytest
from _rendezvous import Rendezvous, run_in_threads

import pyinc_tools.session as session_module
from pyinc import Database
from pyinc.integrations import _decoding, request_scope
from pyinc.integrations.python_source import workspace_analysis
from pyinc.integrations.scope_resolution import _decode_scope_tree, scope_tree
from pyinc.integrations.symbol_resolution import (
    _module_symbol_table,
    _placed_module_symbol_table,
    find_references,
    module_symbol_table,
)
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


def test_entrypoints_answer_once_per_session_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    with WorkspaceSession(tmp_path) as session:
        session.analyze_workspace()
        counter = _count_calls(monkeypatch, _module_symbol_table)
        session.analyze_workspace()
        # Three modules. The request builds each file's result and resolves its
        # imports, which asks for the same tables again; within one request they
        # are answered once each.
        assert counter["n"] == 3


def test_entrypoints_outside_a_session_still_see_edits(tmp_path: Path) -> None:
    """The per-request memo must not exist for a caller driving the layer directly."""

    _write_workspace(tmp_path)
    db = Database(mode="strict")
    alpha = tmp_path / "alpha.py"
    before = module_symbol_table(db, tmp_path, alpha)
    assert sorted(symbol.qualified_name for symbol in before.symbols) == ["one", "two"]
    assert len(scope_tree(db, alpha).bindings) == 2

    alpha.write_text(
        "def one():\n    return 1\n\n\ndef two():\n    return 2\n\n\ndef three():\n    return 3\n",
        encoding="utf-8",
    )
    after = module_symbol_table(db, tmp_path, alpha)
    assert sorted(symbol.qualified_name for symbol in after.symbols) == ["one", "three", "two"]
    assert len(scope_tree(db, alpha).bindings) == 3


def test_a_context_copied_inside_a_request_scope_does_not_answer_from_its_memo(
    tmp_path: Path,
) -> None:
    """The memo is the declaring thread's, for as long as its scope is open.

    A copied context still holds the scope: every `threading.Thread` started on
    a free-threaded 3.14 build copies its starter's, as `asyncio.to_thread` does
    on every build. The copy is made by hand here so the cell holds everywhere.
    """

    _write_workspace(tmp_path)
    db = Database(mode="strict")
    alpha = tmp_path / "alpha.py"
    three = "def one():\n    return 1\n\n\ndef two():\n    return 2\n\n\ndef three():\n    return 3\n"

    def bindings_in_thread(context: contextvars.Context) -> int:
        box: list[int] = []
        worker = threading.Thread(
            target=context.run,
            args=(lambda: box.append(len(scope_tree(db, alpha).bindings)),),
        )
        worker.start()
        worker.join()
        return box[0]

    with request_scope(db):
        assert len(scope_tree(db, alpha).bindings) == 2
        carried = contextvars.copy_context()
        alpha.write_text(three, encoding="utf-8")
        # The scope's promise is its own thread's: another thread holding the
        # context computes for itself and sees the edit ...
        assert bindings_in_thread(carried) == 3
        # ... while the declaring thread answers from the memo, as it promised.
        assert len(scope_tree(db, alpha).bindings) == 2

    alpha.write_text(three + "\n\ndef four():\n    return 4\n", encoding="utf-8")
    # Closed, the scope answers nobody -- not even its own thread through a copy.
    assert len(carried.run(scope_tree, db, alpha).bindings) == 4


def test_decode_memo_is_skipped_when_payload_identity_is_unstable(tmp_path: Path) -> None:
    """Off `strict`, `db.get` thaws a fresh object per call, so every entry would miss."""

    _write_workspace(tmp_path)
    _decoding._CACHES.clear()
    for mode in ("fast", "checked"):
        db = Database(mode=mode)
        workspace_analysis(db, tmp_path)
        assert _decoding._CACHES.get(db) is None, mode
    strict_db = Database(mode="strict")
    workspace_analysis(strict_db, tmp_path)
    assert _decoding._CACHES.get(strict_db)


def test_a_mid_request_mirror_rewrite_drops_the_memo(tmp_path: Path) -> None:
    """Completion repairs the file in the mirror mid-method; the memo must follow."""

    _write_workspace(tmp_path)
    (tmp_path / "delta.py").write_text(
        "from alpha import one\n\n\ndef five():\n    value = one()\n    return value\n",
        encoding="utf-8",
    )
    with WorkspaceSession(tmp_path) as session:
        assert _codes(session, "delta.py") == []
        # A caret line that does not parse: completion writes a repaired buffer
        # into the mirror, queries against it, and restores the original bytes.
        session.set_overlay(
            "delta.py",
            "from alpha import one\n\n\ndef five():\n    value = one()\n    return value.\n",
        )
        session.completions_at("delta.py", 5, 17)
        # Reads after the restore must describe the file as it stands, not the
        # repaired buffer an entrypoint saw a moment earlier in the same request.
        session.clear_overlay("delta.py")
        assert _codes(session, "delta.py") == []
        result = session.analyze_file("delta.py")
        assert result.symbols is not None
        assert [symbol.qualified_name for symbol in result.symbols.symbols] == ["one", "five"]


class _WeakrefablePayload(list[str]):
    """Plain tuples and lists take no weak references; a list subclass does."""


def test_decode_memo_entries_die_with_their_database() -> None:
    db = Database(mode="strict")
    payload = _WeakrefablePayload(["payload"])
    decoded_value = _decoding.decoded(
        db, "kind", (payload,), lambda: _WeakrefablePayload(["decoded"])
    )
    assert decoded_value == ["decoded"]
    payload_ref = weakref.ref(payload)
    decoded_ref = weakref.ref(decoded_value)
    del payload, decoded_value, db
    gc.collect()
    assert payload_ref() is None
    assert decoded_ref() is None


def test_decode_memo_still_hits_within_a_live_database() -> None:
    db = Database(mode="strict")
    payload = _WeakrefablePayload(["payload"])
    calls = {"count": 0}

    def decode() -> str:
        calls["count"] += 1
        return "value"

    first = _decoding.decoded(db, "kind", (payload,), decode)
    second = _decoding.decoded(db, "kind", (payload,), decode)
    assert first == second == "value"
    assert calls["count"] == 1


class _RendezvousCaches(weakref.WeakKeyDictionary[Any, Any]):
    """The memo's per-database map, holding a lookup at the race's window.

    It answers from before the hold, as a thread preempted there would.
    """

    def __init__(self, rendezvous: Rendezvous) -> None:
        super().__init__()
        self._rendezvous = rendezvous

    def get(self, key: Any, default: Any = None) -> Any:
        value = super().get(key, default)
        self._rendezvous.point()
        return value


def test_decode_memo_keeps_both_threads_entries_for_a_new_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads that find no memo for a database must end up sharing one.

    Unguarded, both found none and each installed its own, and the second
    install dropped the entry the first thread stored, so the next call decoded
    again. Driving one `Database` from two threads directly, without a
    `WorkspaceSession` to serialize them, is enough to lose it.
    """

    rendezvous = Rendezvous()
    monkeypatch.setattr(_decoding, "_CACHES", _RendezvousCaches(rendezvous))
    monkeypatch.setattr(_decoding, "_CACHES_LOCK", rendezvous.lock(), raising=False)
    db = Database(mode="strict")
    payload = _WeakrefablePayload(["payload"])
    calls = {"alpha": 0, "beta": 0}

    def decode(kind: str) -> Callable[[], str]:
        def run() -> str:
            calls[kind] += 1
            return kind

        return run

    run_in_threads(
        lambda: _decoding.decoded(db, "alpha", (payload,), decode("alpha")),
        lambda: _decoding.decoded(db, "beta", (payload,), decode("beta")),
    )

    assert _decoding.decoded(db, "alpha", (payload,), decode("alpha")) == "alpha"
    assert _decoding.decoded(db, "beta", (payload,), decode("beta")) == "beta"
    assert calls == {"alpha": 1, "beta": 1}


def test_decode_memo_hands_racing_threads_the_decode_it_stored() -> None:
    """Two threads that decode one payload at once get back one object.

    The decode runs outside the memo's lock, so both may run it; whichever
    stored first is what both answer with, and what the memo keeps.
    """

    rendezvous = Rendezvous()
    db = Database(mode="strict")
    payload = _WeakrefablePayload(["payload"])

    def decode() -> list[str]:
        rendezvous.point()
        return ["decoded"]

    first, second = run_in_threads(
        lambda: _decoding.decoded(db, "kind", (payload,), decode),
        lambda: _decoding.decoded(db, "kind", (payload,), decode),
    )

    assert first is second
    assert _decoding.decoded(db, "kind", (payload,), decode) is first
