from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest

import pyinc_tools.cli as cli


def _decode_json_stream(value: str) -> list[object]:
    decoder = json.JSONDecoder()
    decoded: list[object] = []
    offset = 0
    while offset < len(value):
        offset += len(value[offset:]) - len(value[offset:].lstrip())
        if offset == len(value):
            break
        item, offset = decoder.raw_decode(value, offset)
        decoded.append(item)
    return decoded


@dataclass(frozen=True)
class _AnalysisResult:
    kind: str
    path: str | None = None


class _Session:
    def __init__(self, root: str, *, analysis_error: Exception | None = None) -> None:
        self.root = root
        self.analysis_error = analysis_error
        self.entered = False
        self.exited = False
        self.analyzed_paths: list[str] = []

    def __enter__(self) -> _Session:
        self.entered = True
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.exited = True

    def analyze_workspace(self) -> _AnalysisResult:
        if self.analysis_error is not None:
            raise self.analysis_error
        return _AnalysisResult("workspace")

    def analyze_file(self, path: str) -> _AnalysisResult:
        if self.analysis_error is not None:
            raise self.analysis_error
        self.analyzed_paths.append(path)
        return _AnalysisResult("file", path)


class _Watcher:
    instances: list[_Watcher] = []
    change: tuple[str, ...] = ()

    def __init__(self, session: _Session, *, debounce_ms: int) -> None:
        self.session = session
        self.debounce_ms = debounce_ms
        self.interval_s: float | None = None
        self.entered = False
        self.exited = False
        type(self).instances.append(self)

    def __enter__(self) -> _Watcher:
        self.entered = True
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.exited = True

    def start(
        self,
        on_change: Callable[[tuple[str, ...]], None],
        *,
        interval_s: float,
    ) -> None:
        self.interval_s = interval_s
        if self.change:
            on_change(self.change)


class _Event:
    interrupt = False

    def wait(self) -> None:
        if self.interrupt:
            raise KeyboardInterrupt


@pytest.fixture()
def session_factory(monkeypatch: pytest.MonkeyPatch) -> list[_Session]:
    sessions: list[_Session] = []

    def build(root: str) -> _Session:
        session = _Session(root)
        sessions.append(session)
        return session

    monkeypatch.setattr(cli, "WorkspaceSession", build)
    return sessions


def test_main_analyzes_workspace_and_file(
    session_factory: list[_Session], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["analyze", "/workspace", "--indent", "0"]) == cli.EXIT_SUCCESS
    workspace_payload = json.loads(capsys.readouterr().out)
    assert workspace_payload == {"kind": "workspace", "path": None}

    assert (
        cli.main(["analyze", "/workspace", "--path", "pkg/mod.py", "--indent", "0"])
        == cli.EXIT_SUCCESS
    )
    file_payload = json.loads(capsys.readouterr().out)
    assert file_payload == {"kind": "file", "path": "pkg/mod.py"}
    assert session_factory[1].analyzed_paths == ["pkg/mod.py"]
    assert all(session.entered and session.exited for session in session_factory)


@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("not a workspace")])
def test_main_reports_workspace_construction_errors(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_root: str) -> _Session:
        raise error

    monkeypatch.setattr(cli, "WorkspaceSession", fail)

    assert cli.main(["analyze", "/workspace"]) == cli.EXIT_ERROR
    assert capsys.readouterr().err == f"pyinc-tools: {error}\n"


@pytest.mark.parametrize("error", [OSError("vanished"), ValueError("outside root")])
def test_main_reports_analysis_errors_and_closes_session(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _Session("/workspace", analysis_error=error)
    monkeypatch.setattr(cli, "WorkspaceSession", lambda _root: session)

    assert cli.main(["analyze", "/workspace"]) == cli.EXIT_ERROR
    assert capsys.readouterr().err == f"pyinc-tools: {error}\n"
    assert session.entered and session.exited


@pytest.mark.parametrize(
    ("extra_args", "expected_interval", "interrupt"),
    [
        (["--poll-interval-ms", "125"], 0.125, True),
        (["--debounce-ms", "20"], 0.05, False),
        (["--debounce-ms", "400"], 0.2, True),
    ],
)
def test_main_watch_emits_changes_and_selects_poll_interval(
    extra_args: list[str],
    expected_interval: float,
    interrupt: bool,
    session_factory: list[_Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _Watcher.instances.clear()
    _Watcher.change = ("/workspace/a.py", "/workspace/b.py")
    _Event.interrupt = interrupt
    monkeypatch.setattr(cli, "PollingWorkspaceWatcher", _Watcher)
    monkeypatch.setattr(threading, "Event", _Event)

    arguments = [
        "analyze",
        "/workspace",
        "--path",
        "pkg/mod.py",
        "--watch",
        "--indent",
        "0",
        *extra_args,
    ]
    assert cli.main(arguments) == cli.EXIT_SUCCESS

    payloads = _decode_json_stream(capsys.readouterr().out)
    assert payloads == [
        {"kind": "file", "path": "pkg/mod.py"},
        {
            "analysis": {"kind": "file", "path": "pkg/mod.py"},
            "changed_paths": ["/workspace/a.py", "/workspace/b.py"],
        },
    ]
    watcher = _Watcher.instances[-1]
    assert watcher.interval_s == expected_interval
    assert watcher.entered and watcher.exited
    assert session_factory[-1].analyzed_paths == ["pkg/mod.py", "pkg/mod.py"]


def test_main_dispatches_lsp_and_uses_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str | None] = []

    class Server:
        def __init__(self, *, default_root: str | None) -> None:
            observed.append(default_root)

        def serve(self) -> int:
            return 17

    monkeypatch.setattr(cli, "LanguageServer", Server)
    monkeypatch.setattr(sys, "argv", ["pyinc-tools", "lsp", "--root", "/workspace"])

    assert cli.main() == 17
    assert observed == ["/workspace"]


@pytest.mark.parametrize(
    "arguments",
    [[], ["--version"], ["analyze", "/workspace", "--debounce-ms", "invalid"]],
)
def test_main_argparse_exits_for_help_version_and_invalid_values(
    arguments: Sequence[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code in {0, cli.EXIT_USAGE}
    output = capsys.readouterr()
    assert output.out or output.err


def test_emit_json_sorts_keys_and_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, bool]] = []

    def record(value: str, *, flush: bool) -> None:
        observed.append((value, flush))

    monkeypatch.setattr("builtins.print", record)

    cli._emit_json({"z": 1, "a": 2}, indent=0)

    assert observed == [('{\n"a": 2,\n"z": 1\n}', True)]
