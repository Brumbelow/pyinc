from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, NoReturn

import pytest

import pyinc_tools.cli as cli
from pyinc.integrations import SourcePosition, SourceRange
from pyinc_tools import AnalysisDiagnostic
from pyinc_tools._workspace import DEFAULT_MAX_WORKSPACE_FILES


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
        self.options: dict[str, object] = {}

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

    def _normalize_real_path(self, path: str) -> str:
        return str(Path(self.root, path))


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

    def build(root: str, **options: object) -> _Session:
        session = _Session(root)
        session.options = options
        sessions.append(session)
        return session

    monkeypatch.setattr(cli, "WorkspaceSession", build)
    return sessions


def _workspace_with_module(root: Path) -> str:
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    return str(root)


def test_main_analyzes_workspace_and_file(
    session_factory: list[_Session], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = _workspace_with_module(tmp_path)
    assert cli.main(["analyze", root, "--indent", "0"]) == cli.EXIT_SUCCESS
    workspace_payload = json.loads(capsys.readouterr().out)
    assert workspace_payload == {"kind": "workspace", "path": None}

    assert cli.main(["analyze", root, "--path", "pkg/mod.py", "--indent", "0"]) == cli.EXIT_SUCCESS
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
    def fail(_root: str, **_options: object) -> _Session:
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
    monkeypatch.setattr(cli, "WorkspaceSession", lambda _root, **_options: session)

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
    tmp_path: Path,
) -> None:
    _Watcher.instances.clear()
    _Watcher.change = ("/workspace/a.py", "/workspace/b.py")
    _Event.interrupt = interrupt
    monkeypatch.setattr(cli, "PollingWorkspaceWatcher", _Watcher)
    monkeypatch.setattr(threading, "Event", _Event)

    arguments = [
        "analyze",
        _workspace_with_module(tmp_path),
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

    Kept apart from `_AnalysisResult`, because a `diagnostics` field there
    would change every `asdict` payload the default-output tests assert on.
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

    def build(root: str, **_options: object) -> _DiagnosticSession:
        session = _DiagnosticSession(root, diagnostics)
        sessions.append(session)
        return session

    monkeypatch.setattr(cli, "WorkspaceSession", build)
    return sessions


def test_default_invocation_output_is_byte_identical(
    session_factory: list[_Session], capsys: pytest.CaptureFixture[str]
) -> None:
    """The new flags leave the existing default output unchanged."""

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


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--poll-interval-ms", "0"),
        ("--poll-interval-ms", "-5"),
        ("--debounce-ms", "-1"),
        ("--indent", "-1"),
    ],
)
def test_numbers_below_their_minimum_are_usage_errors(
    flag: str,
    value: str,
    session_factory: list[_Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["analyze", "/workspace", flag, value])

    assert raised.value.code == cli.EXIT_USAGE
    error = capsys.readouterr().err
    assert f"argument {flag}: must be at least" in error
    assert session_factory == []


def test_numbers_at_their_minimum_are_accepted() -> None:
    args = cli._build_parser().parse_args(
        ["analyze", "/workspace", "--poll-interval-ms", "1", "--debounce-ms", "0", "--indent", "0"]
    )

    assert (args.poll_interval_ms, args.debounce_ms, args.indent) == (1, 0, 0)


@pytest.mark.parametrize("name", ["missing.py", "pkg"])
def test_path_that_is_not_an_existing_file_exits_1_and_names_it(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _workspace_with_module(root)

    assert cli.main(["analyze", str(root), "--path", name]) == cli.EXIT_ERROR
    output = capsys.readouterr()
    assert output.out == ""
    expected = root.resolve() / name
    assert output.err == f"pyinc-tools: {expected} is not an existing file.\n"


def test_max_files_sets_the_workspace_file_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("pass\n", encoding="utf-8")

    assert cli.main(["analyze", str(root), "--max-files", "2"]) == cli.EXIT_ERROR
    output = capsys.readouterr()
    assert output.out == ""
    assert "more than 2 " in output.err
    assert str(root.resolve()) in output.err
    assert "--max-files" in output.err

    assert cli.main(["analyze", str(root), "--max-files", "3"]) == cli.EXIT_SUCCESS


def test_max_files_defaults_to_the_session_limit(session_factory: list[_Session]) -> None:
    assert cli.main(["analyze", "/workspace"]) == cli.EXIT_SUCCESS
    assert cli.main(["analyze", "/workspace", "--max-files", "5"]) == cli.EXIT_SUCCESS

    assert [session.options for session in session_factory] == [
        {"max_files": DEFAULT_MAX_WORKSPACE_FILES},
        {"max_files": 5},
    ]


@pytest.mark.parametrize(
    ("value", "reason"), [("0", "must be at least 1"), ("x", "invalid int value")]
)
def test_max_files_below_one_or_not_a_number_is_a_usage_error(
    value: str,
    reason: str,
    session_factory: list[_Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["analyze", "/workspace", "--max-files", value])

    assert raised.value.code == cli.EXIT_USAGE
    assert f"argument --max-files: {reason}" in capsys.readouterr().err
    assert session_factory == []


class _ClosedPipeStdout:
    """Stands in for stdout on a pipe whose reader has gone.

    `fileno` names a file the test owns, so the handler can point it at the
    null device without touching the stdout pytest captures.
    """

    def __init__(self, fd: int, *, closed: bool) -> None:
        self.fd = fd
        self.closed_by_reader = closed
        self.written: list[str] = []

    def write(self, text: str) -> int:
        if self.closed_by_reader:
            raise BrokenPipeError(32, "Broken pipe")
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        if self.closed_by_reader:
            raise BrokenPipeError(32, "Broken pipe")

    def fileno(self) -> int:
        return self.fd


@pytest.fixture()
def stdout_fd(tmp_path: Path) -> Iterator[int]:
    fd = os.open(tmp_path / "stdout", os.O_WRONLY | os.O_CREAT)
    try:
        yield fd
    finally:
        os.close(fd)


def _points_at_null_device(fd: int) -> bool:
    return os.path.samestat(os.fstat(fd), os.stat(os.devnull))


def test_closed_pipe_exits_1_quietly(
    session_factory: list[_Session],
    stdout_fd: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdout", _ClosedPipeStdout(stdout_fd, closed=True))

    assert cli.main(["analyze", "/workspace"]) == cli.EXIT_ERROR
    assert capsys.readouterr().err == ""
    assert _points_at_null_device(stdout_fd)
    assert session_factory[0].exited


def test_closed_pipe_ends_watch_mode_quietly(
    session_factory: list[_Session],
    stdout_fd: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdout = _ClosedPipeStdout(stdout_fd, closed=False)

    class ReaderClosesWatcher(_Watcher):
        def start(
            self,
            on_change: Callable[[tuple[str, ...]], None],
            *,
            interval_s: float,
        ) -> None:
            stdout.closed_by_reader = True
            super().start(on_change, interval_s=interval_s)

    _Watcher.instances.clear()
    _Watcher.change = ("/workspace/a.py",)
    monkeypatch.setattr(cli, "PollingWorkspaceWatcher", ReaderClosesWatcher)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert cli.main(["analyze", "/workspace", "--watch", "--indent", "0"]) == cli.EXIT_ERROR
    assert capsys.readouterr().err == ""
    assert json.loads("".join(stdout.written)) == {"kind": "workspace", "path": None}
    assert _points_at_null_device(stdout_fd)
    assert _Watcher.instances[-1].exited


@pytest.mark.skipif(os.name == "nt", reason="POSIX pipe semantics")
def test_closed_pipe_prints_nothing_to_stderr(tmp_path: Path) -> None:
    root = _workspace_with_module(tmp_path)
    read_fd, write_fd = os.pipe()
    # With the read end closed before the process starts, its first write
    # into the pipe fails, however little it prints.
    os.close(read_fd)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pyinc_tools", "analyze", root],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        os.close(write_fd)

    assert completed.stderr == b""
    assert completed.returncode == cli.EXIT_ERROR


# The machine running the suite can be slow and shared, so the bound is loose.
# It only exists to turn a hung server into a failure.
_LSP_PROCESS_TIMEOUT_S = 120.0


def _lsp_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def _read_lsp_frames(stream: IO[bytes], frames: queue.Queue[dict[str, Any] | None]) -> None:
    # Framing is parsed here on its own, so a framing bug in the server cannot
    # cancel out against the same bug in the reader.
    try:
        while True:
            length: int | None = None
            while True:
                line = stream.readline()
                if not line:
                    return
                if line == b"\r\n":
                    break
                name, _, value = line.decode("ascii").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value)
            if length is None:
                return
            frames.put(json.loads(stream.read(length)))
    finally:
        frames.put(None)


class _LspProcess:
    """The server run as ``python -m pyinc_tools lsp`` on binary pipes."""

    def __init__(self, root: Path, temp_dir: Path, stderr_path: Path) -> None:
        environment = dict(os.environ)
        # Run the source this test imported, whatever the child's sys.path holds.
        source_root = str(Path(cli.__file__).resolve().parents[1])
        python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            os.pathsep.join((source_root, python_path)) if python_path else source_root
        )
        # The mirror goes to the temp directory, so the test can see it.
        for name in ("TMPDIR", "TMP", "TEMP"):
            environment[name] = str(temp_dir)
        self.temp_dir = temp_dir
        self._stderr_path = stderr_path
        self._stderr = stderr_path.open("wb")
        self._deadline = time.monotonic() + _LSP_PROCESS_TIMEOUT_S
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pyinc_tools", "lsp", "--root", str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=environment,
        )
        assert self.process.stdout is not None
        self._frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._reader = threading.Thread(
            target=_read_lsp_frames, args=(self.process.stdout, self._frames), daemon=True
        )
        self._reader.start()

    def _remaining_s(self) -> float:
        return self._deadline - time.monotonic()

    def _fail(self, reason: str) -> NoReturn:
        self.process.kill()
        self.process.wait()
        self._stderr.flush()
        stderr = self._stderr_path.read_text(encoding="utf-8", errors="replace")
        pytest.fail(f"{reason}\nserver stderr:\n{stderr}")

    def send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(_lsp_frame(payload))
            self.process.stdin.flush()
        except BrokenPipeError:
            self._fail(f"the server closed stdin before it read {payload['method']}")

    def request(self, request_id: int, method: str, params: object) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            remaining = self._remaining_s()
            if remaining <= 0:
                self._fail(f"no answer to {method} in {_LSP_PROCESS_TIMEOUT_S} s")
            try:
                frame = self._frames.get(timeout=remaining)
            except queue.Empty:
                self._fail(f"no answer to {method} in {_LSP_PROCESS_TIMEOUT_S} s")
            if frame is None:
                self._fail(f"the server closed stdout before it answered {method}")
            # Diagnostics notifications can come at any point between answers.
            if "method" not in frame and frame.get("id") == request_id:
                return frame

    def notify(self, method: str, params: object) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def wait(self) -> int:
        try:
            return self.process.wait(timeout=max(self._remaining_s(), 0.0))
        except subprocess.TimeoutExpired:
            self._fail(f"the server did not exit in {_LSP_PROCESS_TIMEOUT_S} s")

    def mirrors(self) -> list[Path]:
        return sorted(self.temp_dir.glob("pyinc-tools-*"))

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        # The child is gone, so the reader meets the end of its stdout.
        self._reader.join()
        if self.process.stdin is not None:
            with contextlib.suppress(BrokenPipeError):
                self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr.close()


@pytest.fixture()
def lsp_process(tmp_path: Path) -> Iterator[_LspProcess]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "helpers.py").write_text("def helper(x: int) -> int:\n    return x\n", encoding="utf-8")
    (root / "use.py").write_text("value = 0\n", encoding="utf-8")
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    server = _LspProcess(root, temp_dir, tmp_path / "stderr.txt")
    try:
        yield server
    finally:
        server.close()


_INITIALIZE_PARAMS: dict[str, object] = {"processId": None, "rootUri": None, "capabilities": {}}


def test_lsp_stdio_process_round_trip(lsp_process: _LspProcess, tmp_path: Path) -> None:
    initialize = lsp_process.request(1, "initialize", _INITIALIZE_PARAMS)
    assert initialize["result"]["capabilities"]["hoverProvider"] is True
    lsp_process.notify("initialized", {})
    # The open buffer differs from the file on disk, so the answer shows the
    # server read the didOpen text from the pipe.
    use_uri = (tmp_path / "workspace" / "use.py").as_uri()
    lsp_process.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": use_uri,
                "languageId": "python",
                "version": 1,
                "text": "from helpers import helper\n\nvalue = helper(1)\n",
            }
        },
    )
    hover = lsp_process.request(
        2,
        "textDocument/hover",
        {"textDocument": {"uri": use_uri}, "position": {"line": 2, "character": 10}},
    )
    assert hover["result"]["contents"]["kind"] == "markdown"
    assert "def helper(x: int) -> int" in hover["result"]["contents"]["value"]
    assert lsp_process.request(3, "shutdown", None) == {"jsonrpc": "2.0", "id": 3, "result": None}
    lsp_process.notify("exit", None)

    assert lsp_process.wait() == 0
    assert lsp_process.mirrors() == []


@pytest.mark.skipif(os.name == "nt", reason="TerminateProcess cannot be caught")
def test_lsp_sigterm_removes_the_mirror(lsp_process: _LspProcess) -> None:
    lsp_process.request(1, "initialize", _INITIALIZE_PARAMS)
    assert len(lsp_process.mirrors()) == 1

    lsp_process.process.send_signal(signal.SIGTERM)

    assert lsp_process.wait() == 128 + signal.SIGTERM
    assert lsp_process.mirrors() == []
