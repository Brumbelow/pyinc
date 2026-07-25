from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest

import pyinc_tools.cli as cli
from pyinc.integrations import SourcePosition, SourceRange
from pyinc_tools import AnalysisDiagnostic


def _diagnostic(
    *,
    path: str = "/workspace/a.py",
    code: str = "missing-import",
    message: str = "boom",
    severity: str = "error",
    line: int | None = 0,
    character: int = 0,
) -> AnalysisDiagnostic:
    source_range = (
        None
        if line is None
        else SourceRange(
            SourcePosition(line, character),
            SourcePosition(line, character + 1),
        )
    )
    return AnalysisDiagnostic(
        path=path,
        code=code,
        message=message,
        severity=severity,  # type: ignore[arg-type]
        source="test",
        range=source_range,
    )


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


@dataclass(frozen=True)
class _DiagnosticResult:
    """Stands in for a real analysis result in the diagnostics-aware paths.

    Kept separate from `_AnalysisResult` on purpose: adding a `diagnostics`
    field there would change every `asdict` payload the default-output tests
    assert on.
    """

    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


class _DiagnosticSession(_Session):
    def __init__(self, root: str, diagnostics: tuple[AnalysisDiagnostic, ...]) -> None:
        super().__init__(root)
        self.diagnostics = diagnostics

    def analyze_workspace(self) -> _DiagnosticResult:  # type: ignore[override]
        return _DiagnosticResult(self.diagnostics)

    def analyze_file(self, path: str) -> _DiagnosticResult:  # type: ignore[override]
        self.analyzed_paths.append(path)
        return _DiagnosticResult(self.diagnostics)


def _install_diagnostic_session(
    monkeypatch: pytest.MonkeyPatch, diagnostics: tuple[AnalysisDiagnostic, ...]
) -> list[_DiagnosticSession]:
    sessions: list[_DiagnosticSession] = []

    def build(root: str) -> _DiagnosticSession:
        session = _DiagnosticSession(root, diagnostics)
        sessions.append(session)
        return session

    monkeypatch.setattr(cli, "WorkspaceSession", build)
    return sessions


def test_default_invocation_output_is_byte_identical(
    session_factory: list[_Session], capsys: pytest.CaptureFixture[str]
) -> None:
    """The new flags must not perturb the pre-existing default output."""

    assert cli.main(["analyze", "/workspace"]) == cli.EXIT_SUCCESS
    assert capsys.readouterr().out == '{\n  "kind": "workspace",\n  "path": null\n}\n'


@pytest.mark.parametrize(
    ("line", "character", "expected_location"),
    [
        (3, 7, "/workspace/a.py:4:8"),
        (0, 0, "/workspace/a.py:1:1"),
        (None, 0, "/workspace/a.py"),
    ],
)
def test_text_format_renders_one_line_per_diagnostic(
    line: int | None,
    character: int,
    expected_location: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostic = _diagnostic(line=line, character=character)
    _install_diagnostic_session(monkeypatch, (diagnostic,))

    assert cli.main(["analyze", "/workspace", "--format", "text"]) == cli.EXIT_SUCCESS
    assert capsys.readouterr().out == f"{expected_location}: error missing-import boom\n"


def test_text_format_sorts_by_location_with_rangeless_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    diagnostics = (
        _diagnostic(path="/workspace/b.py", code="second", line=1),
        _diagnostic(path="/workspace/a.py", code="third", line=9),
        _diagnostic(path="/workspace/a.py", code="first", line=None),
    )
    _install_diagnostic_session(monkeypatch, diagnostics)

    assert cli.main(["analyze", "/workspace", "--format", "text"]) == cli.EXIT_SUCCESS
    codes = [line.split(" ")[2] for line in capsys.readouterr().out.splitlines()]
    assert codes == ["first", "third", "second"]


def test_text_format_ignores_indent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_diagnostic_session(monkeypatch, (_diagnostic(),))

    assert cli.main(["analyze", "/workspace", "--format", "text", "--indent", "0"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["analyze", "/workspace", "--format", "text", "--indent", "8"]) == 0
    assert capsys.readouterr().out == first


def test_diagnostics_only_emits_sorted_json_array(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    diagnostics = (
        _diagnostic(code="second", line=5),
        _diagnostic(code="first", line=1),
    )
    _install_diagnostic_session(monkeypatch, diagnostics)

    assert cli.main(["analyze", "/workspace", "--diagnostics-only", "--indent", "0"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in payload] == ["first", "second"]
    assert payload[0]["range"]["start"] == {"line": 1, "character": 0}


def test_no_diagnostics_produces_empty_text_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_diagnostic_session(monkeypatch, ())

    assert cli.main(["analyze", "/workspace", "--format", "text", "--fail-on", "hint"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("fail_on", "severities", "expected"),
    [
        ("none", ("error",), cli.EXIT_SUCCESS),
        ("error", (), cli.EXIT_SUCCESS),
        ("error", ("error",), cli.EXIT_DIAGNOSTICS),
        ("error", ("warning", "hint"), cli.EXIT_SUCCESS),
        ("warning", ("warning",), cli.EXIT_DIAGNOSTICS),
        ("warning", ("error",), cli.EXIT_DIAGNOSTICS),
        ("warning", ("hint",), cli.EXIT_SUCCESS),
        ("information", ("information",), cli.EXIT_DIAGNOSTICS),
        ("information", ("hint",), cli.EXIT_SUCCESS),
        ("hint", ("hint",), cli.EXIT_DIAGNOSTICS),
    ],
)
def test_fail_on_threshold_matrix(
    fail_on: str,
    severities: tuple[str, ...],
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics = tuple(_diagnostic(severity=severity) for severity in severities)
    _install_diagnostic_session(monkeypatch, diagnostics)

    assert cli.main(["analyze", "/workspace", "--fail-on", fail_on, "--indent", "0"]) == expected
    capsys.readouterr()


def test_failing_gate_still_emits_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_diagnostic_session(monkeypatch, (_diagnostic(),))

    exit_code = cli.main(["analyze", "/workspace", "--format", "text", "--fail-on", "error"])
    assert exit_code == cli.EXIT_DIAGNOSTICS
    assert "missing-import" in capsys.readouterr().out


def test_watch_with_fail_on_is_a_usage_error(
    session_factory: list[_Session], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.main(["analyze", "/workspace", "--watch", "--fail-on", "error"])

    assert exit_code == cli.EXIT_USAGE
    assert capsys.readouterr().err == (
        "pyinc-tools: --fail-on cannot be combined with --watch\n"
    )
    assert session_factory == []


def test_watch_text_format_emits_changed_header(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _Watcher.instances.clear()
    _Watcher.change = ("/workspace/a.py",)
    _Event.interrupt = True
    _install_diagnostic_session(monkeypatch, (_diagnostic(),))
    monkeypatch.setattr(cli, "PollingWorkspaceWatcher", _Watcher)
    monkeypatch.setattr(threading, "Event", _Event)

    assert cli.main(["analyze", "/workspace", "--watch", "--format", "text"]) == cli.EXIT_SUCCESS
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "/workspace/a.py:1:1: error missing-import boom"
    assert lines[1] == "# changed: /workspace/a.py"
    assert lines[2] == "/workspace/a.py:1:1: error missing-import boom"


def test_watch_json_diagnostics_only_keeps_wrapper_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _Watcher.instances.clear()
    _Watcher.change = ("/workspace/a.py",)
    _Event.interrupt = True
    _install_diagnostic_session(monkeypatch, (_diagnostic(),))
    monkeypatch.setattr(cli, "PollingWorkspaceWatcher", _Watcher)
    monkeypatch.setattr(threading, "Event", _Event)

    arguments = ["analyze", "/workspace", "--watch", "--diagnostics-only", "--indent", "0"]
    assert cli.main(arguments) == cli.EXIT_SUCCESS
    payloads = _decode_json_stream(capsys.readouterr().out)
    assert isinstance(payloads[0], list)
    event = payloads[1]
    assert isinstance(event, dict)
    assert event["changed_paths"] == ["/workspace/a.py"]
    assert isinstance(event["analysis"], list)


def test_emit_json_sorts_keys_and_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, bool]] = []

    def record(value: str, *, flush: bool) -> None:
        observed.append((value, flush))

    monkeypatch.setattr("builtins.print", record)

    cli._emit_json({"z": 1, "a": 2}, indent=0)

    assert observed == [('{\n"a": 2,\n"z": 1\n}', True)]
