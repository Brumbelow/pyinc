"""Source-range decoration must tokenize once per file and cache across requests."""

import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from pyinc import Database
from pyinc._python_lexing import identifier_tokens
from pyinc.integrations.python_source import file_analysis, workspace_analysis

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


def _install_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    # Swapping in a counting replacement for identifier_tokens won't work:
    # pyinc's static capture analysis fingerprints each statically reachable
    # callable, including a monkeypatched one, and folds in directly discovered
    # mutable state it closes over (see "Static capture analysis" in
    # docs/kernel-contract.md). That makes the counter's value part of
    # source_ranges_for_file's identity and changes it on every increment,
    # defeating the caching this test verifies. A trace hook counts real calls
    # to the original, untouched function without pyinc ever seeing it.
    counter = {"n": 0}
    target_code = identifier_tokens.__code__

    def tracer(frame: FrameType, event: str, arg: Any) -> _TraceFunc:
        if event == "call" and frame.f_code is target_code:
            counter["n"] += 1
        return tracer

    monkeypatch.setattr(_sys_trace, "current", tracer)
    return counter


def _write_workspace(root: Path) -> None:
    (root / "alpha.py").write_text(
        "def one():\n    return 1\n\n\ndef two():\n    return 2\n\n\ndef three():\n    return 3\n",
        encoding="utf-8",
    )
    (root / "beta.py").write_text(
        "import alpha\n\n\ndef four():\n    return alpha.one()\n", encoding="utf-8"
    )


def test_single_tokenization_per_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    counter = _install_counter(monkeypatch)
    db = Database(mode="strict")
    workspace_analysis(db, tmp_path)
    # One tokenization per file, NOT one per definition (alpha has 3 defs).
    assert counter["n"] == 2


def test_unchanged_rerequest_tokenizes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    counter = _install_counter(monkeypatch)
    db = Database(mode="strict")
    workspace_analysis(db, tmp_path)
    counter["n"] = 0
    workspace_analysis(db, tmp_path)
    assert counter["n"] == 0


def test_edit_retokenizes_only_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    counter = _install_counter(monkeypatch)
    db = Database(mode="strict")
    workspace_analysis(db, tmp_path)
    counter["n"] = 0
    (tmp_path / "beta.py").write_text(
        "import alpha\n\n\ndef four():\n    return alpha.two()\n", encoding="utf-8"
    )
    workspace_analysis(db, tmp_path)
    assert counter["n"] == 1


def test_ranges_still_precise_after_caching(tmp_path: Path) -> None:
    _write_workspace(tmp_path)
    db = Database(mode="strict")
    result = file_analysis(db, tmp_path / "alpha.py")
    one = next(d for d in result.definitions if d.name == "one")
    # "def one():" -- the name range covers exactly the identifier.
    assert (one.range.start.line, one.range.start.character) == (0, 4)
    assert (one.range.end.line, one.range.end.character) == (0, 7)


def test_syntax_error_range_override_preserved(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    db = Database(mode="strict")
    result = file_analysis(db, tmp_path / "broken.py")
    assert result.diagnostics
    assert result.diagnostics[0].range is not None
