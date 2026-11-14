"""How the language server delivers diagnostics to the client.

The server publishes diagnostics on its own after every edit and refresh. A
client that also pulled them would show each one twice, so the server does
not advertise pull diagnostics. It still answers a pull request from a client
that sends one anyway.

One worker thread runs the analyses and sends the publishes. The tests below
hold an analysis on events and wait for the worker to go idle. None of them
sleeps.
"""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pyinc.integrations import SourcePosition, SourceRange
from pyinc_tools._jsonrpc import read_message
from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import AnalysisDiagnostic, PollingWorkspaceWatcher, WorkspaceSession

# Capabilities of a client that can pull diagnostics, for the document and the
# workspace, and also accepts pushed ones.
_PULL_CAPABLE_CLIENT = {
    "textDocument": {
        "diagnostic": {"dynamicRegistration": False, "relatedDocumentSupport": False},
        "publishDiagnostics": {"relatedInformation": True},
    },
    "workspace": {"diagnostics": {"refreshSupport": True}},
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "user.py").write_text("import totally_unknown_module\n", encoding="utf-8")
    return root


@pytest.fixture
def server(workspace: Path) -> Iterator[LanguageServer]:
    language_server = LanguageServer(default_root=str(workspace))
    yield language_server
    if language_server._session is not None:
        language_server._session.close()


def _initialize(server: LanguageServer, root: Path) -> dict[str, object]:
    result = server._handle_request(
        "initialize",
        {
            "rootUri": root.as_uri(),
            "capabilities": _PULL_CAPABLE_CLIENT,
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    )
    assert isinstance(result, dict)
    return result


def test_initialize_does_not_advertise_pull_diagnostics(
    server: LanguageServer, workspace: Path
) -> None:
    capabilities = _initialize(server, workspace)["capabilities"]
    assert isinstance(capabilities, dict)
    assert "diagnosticProvider" not in capabilities


def test_document_pull_still_gets_a_full_report(server: LanguageServer, workspace: Path) -> None:
    _initialize(server, workspace)
    uri = (workspace / "user.py").as_uri()
    report = server._handle_request("textDocument/diagnostic", {"textDocument": {"uri": uri}})
    assert report["kind"] == "full"
    assert isinstance(report["resultId"], str) and report["resultId"]
    assert [item["code"] for item in report["items"]] == ["missing-import"]

    again = server._handle_request(
        "textDocument/diagnostic",
        {"textDocument": {"uri": uri}, "previousResultId": report["resultId"]},
    )
    assert again == {"kind": "unchanged", "resultId": report["resultId"]}


def test_workspace_pull_still_gets_a_report_per_file(
    server: LanguageServer, workspace: Path
) -> None:
    _initialize(server, workspace)
    uri = (workspace / "user.py").as_uri()
    result = server._handle_request("workspace/diagnostic", {})
    reports = result["items"]
    assert [(entry["kind"], entry["uri"], entry["version"]) for entry in reports] == [
        ("full", uri, None)
    ]
    assert isinstance(reports[0]["resultId"], str) and reports[0]["resultId"]
    assert [item["code"] for item in reports[0]["items"]] == ["missing-import"]


#: Only a thread that never arrives reaches it. A cold analysis on a loaded
#: machine takes seconds, and every wait below may span one.
_TIMEOUT = 120.0


class _HeldAnalysis:
    """One workspace analysis that stops until the test releases it.

    With ``holding_session_lock`` it stops before analyzing, inside the
    session lock, as a real analysis holds it. Without it, it stops after
    analyzing, with the session lock free and its result not yet published.
    It never stops on the thread that sends the test's notifications, because
    only that thread can release it.
    """

    def __init__(self, *, holding_session_lock: bool) -> None:
        self.holding_session_lock = holding_session_lock
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def run(
        self,
        session: WorkspaceSession,
        analyze: Callable[[WorkspaceSession], Any],
        *,
        stop: bool,
    ) -> Any:
        try:
            if self.holding_session_lock:
                with session._state_lock:
                    self._stop_here(stop)
                    return analyze(session)
            result = analyze(session)
            self._stop_here(stop)
            return result
        finally:
            self.finished.set()

    def _stop_here(self, stop: bool) -> None:
        self.entered.set()
        if stop and not self.release.wait(_TIMEOUT):
            raise AssertionError("the test never released the held analysis")


class _WatchedServer:
    """A server whose watcher polls only when the test calls `poll`.

    The watcher is the one `initialize` builds, with its debounce at zero. Its
    thread is never started. `poll` runs one poll and hands what it found to
    the server's callback, as the watcher thread does. Every workspace
    analysis is counted, with the name of the thread it ran on, and `hold`
    stops the next one.
    """

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.analyses = 0
        self.analysis_threads: list[str] = []
        self.out = io.BytesIO()
        self._held: _HeldAnalysis | None = None
        self._test_thread = threading.current_thread()
        self._on_change: Callable[[tuple[str, ...]], None] | None = None
        original = WorkspaceSession.analyze_workspace

        def counting_analyze_workspace(session: WorkspaceSession) -> Any:
            self.analyses += 1
            self.analysis_threads.append(threading.current_thread().name)
            held, self._held = self._held, None
            if held is None:
                return original(session)
            stop = threading.current_thread() is not self._test_thread
            return held.run(session, original, stop=stop)

        def start_without_thread(
            watcher: PollingWorkspaceWatcher,
            on_change: Callable[[tuple[str, ...]], None],
            **_options: object,
        ) -> None:
            self._on_change = on_change

        monkeypatch.setattr(WorkspaceSession, "analyze_workspace", counting_analyze_workspace)
        monkeypatch.setattr(PollingWorkspaceWatcher, "start", start_without_thread)
        self.server = LanguageServer(
            in_stream=io.BytesIO(), out_stream=self.out, default_root=str(root)
        )
        self.server._handle_request(
            "initialize",
            {
                "rootUri": root.as_uri(),
                "initializationOptions": {"pyinc.watcher.debounceMs": 0},
            },
        )
        assert self._on_change is not None

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.server._handle_message({"jsonrpc": "2.0", "method": method, "params": params})

    def open(self, path: Path, text: str) -> None:
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.as_uri(),
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )

    def change(self, path: Path, text: str) -> None:
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": path.as_uri(), "version": 2},
                "contentChanges": [{"text": text}],
            },
        )

    def poll(self) -> tuple[str, ...]:
        watcher = self.server._watcher
        assert watcher is not None and self._on_change is not None
        ready = watcher.poll()
        if ready:
            self._on_change(ready)
        return ready

    def hold(self, *, holding_session_lock: bool) -> _HeldAnalysis:
        """Stop the next workspace analysis until the test releases it."""
        held = _HeldAnalysis(holding_session_lock=holding_session_lock)
        self._held = held
        return held

    def drain(self) -> None:
        """Wait until every publish the notifications so far scheduled is sent."""
        assert self.server.wait_idle(_TIMEOUT), "the diagnostics worker did not go idle"

    def messages(self) -> list[dict[str, Any]]:
        replay = io.BytesIO(self.out.getvalue())
        messages: list[dict[str, Any]] = []
        while (message := read_message(replay)) is not None:
            messages.append(message)
        return messages

    def published(self) -> list[tuple[str, list[str]]]:
        """Each publishDiagnostics sent so far, as its URI and its messages."""
        return [
            (
                message["params"]["uri"],
                [item["message"] for item in message["params"]["diagnostics"]],
            )
            for message in self.messages()
            if message.get("method") == "textDocument/publishDiagnostics"
        ]

    def close(self) -> None:
        self.server._teardown_session()


@pytest.fixture
def watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_WatchedServer, Path]]:
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "other.py").write_text("other = 1\n", encoding="utf-8")
    harness = _WatchedServer(root, monkeypatch)
    yield harness, root
    harness.close()


def test_save_runs_one_workspace_analysis(watched: tuple[_WatchedServer, Path]) -> None:
    """didSave syncs the saved file, so the watcher has nothing left to refresh.

    The watcher used to see the saved file's new hash on its next poll and
    refresh it again. That ran a second analysis of an unchanged workspace.
    """

    harness, root = watched
    path = root / "app.py"
    uri = path.as_uri()
    harness.notify(
        "textDocument/didOpen",
        {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": "value = 1\n"}},
    )
    harness.notify(
        "textDocument/didChange",
        {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "value = 2\n"}]},
    )
    harness.drain()
    before_save = harness.analyses

    path.write_text("value = 2\n", encoding="utf-8")
    harness.notify("textDocument/didSave", {"textDocument": {"uri": uri}})

    assert harness.poll() == ()
    harness.drain()
    assert harness.analyses == before_save + 1


def test_disk_edit_of_a_closed_file_is_still_refreshed(
    watched: tuple[_WatchedServer, Path],
) -> None:
    harness, root = watched
    path = root / "other.py"
    session = harness.server._session
    assert session is not None
    path.write_text("other = 2\n", encoding="utf-8")

    assert harness.poll() == (str(path),)
    harness.drain()
    assert harness.analyses == 1
    mirrored = Path(session.mirror_root, "other.py")
    assert mirrored.read_text(encoding="utf-8") == "other = 2\n"


def test_unchanged_did_change_runs_no_analysis(
    watched: tuple[_WatchedServer, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A didChange that repeats the client's last text writes and analyzes nothing."""

    harness, root = watched
    path = root / "app.py"
    writes: list[str] = []
    original_set_overlay = WorkspaceSession.set_overlay

    def counting_set_overlay(
        session: WorkspaceSession, file_path: str | os.PathLike[str], text: str
    ) -> str:
        writes.append(text)
        return original_set_overlay(session, file_path, text)

    monkeypatch.setattr(WorkspaceSession, "set_overlay", counting_set_overlay)
    harness.open(path, "value = 2\n")
    harness.drain()
    after_open = harness.analyses
    assert writes == ["value = 2\n"]

    harness.change(path, "value = 2\n")
    harness.drain()

    assert harness.analyses == after_open
    assert writes == ["value = 2\n"]


def test_rapid_changes_during_an_analysis_run_one_more(
    watched: tuple[_WatchedServer, Path],
) -> None:
    """Ten edits made while an analysis is held leave one more analysis due.

    Each didChange used to run its own workspace analysis before the server
    read the next message. Now each one writes the overlay and returns. The
    held analysis then publishes what it found, and the one more analysis
    publishes the last text.
    """

    harness, root = watched
    path = root / "app.py"
    held = harness.hold(holding_session_lock=False)
    harness.open(path, "import missing_0\n")
    assert held.entered.wait(_TIMEOUT)
    try:
        for index in range(1, 11):
            harness.change(path, f"import missing_{index}\n")
    finally:
        held.release.set()
    harness.drain()

    assert harness.analyses <= 2
    uri = path.as_uri()
    last = [messages for published_uri, messages in harness.published() if published_uri == uri][-1]
    assert len(last) == 1 and "'missing_10'" in last[0]


def test_shutdown_during_an_analysis_is_answered_and_nothing_is_published_after(
    watched: tuple[_WatchedServer, Path],
) -> None:
    """shutdown is answered while an analysis holds the session lock.

    That analysis finds a diagnostic. Its publish comes after the answer, so
    it is dropped. exit then waits for the worker.
    """

    harness, root = watched
    path = root / "app.py"
    held = harness.hold(holding_session_lock=True)
    harness.open(path, "import missing_0\n")
    assert held.entered.wait(_TIMEOUT)
    answered = threading.Event()

    def shut_down() -> None:
        harness.server._handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}
        )
        answered.set()

    requester = threading.Thread(target=shut_down)
    requester.start()
    try:
        assert answered.wait(_TIMEOUT)
        assert not held.finished.is_set()
    finally:
        held.release.set()
        requester.join(_TIMEOUT)
    assert held.finished.wait(_TIMEOUT)

    assert (
        harness.server._handle_message({"jsonrpc": "2.0", "method": "exit", "params": {}}) is False
    )
    assert harness.server.wait_idle(0)
    messages = harness.messages()
    answer = messages.index({"jsonrpc": "2.0", "id": 2, "result": None})
    assert [
        message
        for message in messages[answer:]
        if message.get("method") == "textDocument/publishDiagnostics"
    ] == []


def test_exit_and_teardown_wait_for_a_busy_worker_once(
    watched: tuple[_WatchedServer, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """exit joins a busy worker, and the teardown after it does not join it again.

    The worker's join is replaced by one that returns at once, as a join that
    times out does, so the worker is still running when each caller looks.
    """

    harness, root = watched
    held = harness.hold(holding_session_lock=False)
    harness.open(root / "app.py", "import missing_0\n")
    assert held.entered.wait(_TIMEOUT)
    worker = harness.server._worker_thread
    assert worker is not None
    joins: list[float | None] = []

    def timed_out_join(timeout: float | None = None) -> None:
        joins.append(timeout)

    monkeypatch.setattr(worker, "join", timed_out_join)
    try:
        assert (
            harness.server._handle_message({"jsonrpc": "2.0", "method": "exit", "params": {}})
            is False
        )
        assert joins == [5.0]
        harness.server._teardown_session()
    finally:
        held.release.set()
    harness.drain()

    assert joins == [5.0]
    timeouts = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "diagnostics worker did not stop within timeout" in line
    ]
    assert len(timeouts) == 1


def test_watcher_and_did_change_publish_in_the_order_their_analyses_ran(
    watched: tuple[_WatchedServer, Path],
) -> None:
    """A watcher refresh and an edit both reach the client, oldest first.

    The watcher callback schedules a publish and returns, so its analysis runs
    on the diagnostics worker. An edit made while that analysis is held is
    analyzed by the next run, and its publish follows.
    """

    harness, root = watched
    app = root / "app.py"
    other = root / "other.py"
    harness.open(app, "value = 1\n")
    harness.drain()
    before = harness.analyses

    held = harness.hold(holding_session_lock=False)
    other.write_text("import missing_b\n", encoding="utf-8")
    assert harness.poll() == (str(other),)
    assert held.entered.wait(_TIMEOUT)
    try:
        harness.change(app, "import missing_c\n")
    finally:
        held.release.set()
    harness.drain()

    assert harness.analyses - before <= 2
    assert set(harness.analysis_threads) == {"pyinc-tools-diagnostics"}
    published = harness.published()
    assert [uri for uri, _messages in published] == [other.as_uri(), app.as_uri()]
    assert "'missing_b'" in published[0][1][0]
    assert "'missing_c'" in published[1][1][0]


def test_watched_files_notification_analyzes_on_the_worker(
    watched: tuple[_WatchedServer, Path],
) -> None:
    """didChangeWatchedFiles refreshes the paths and returns before the analysis.

    It used to analyze the whole workspace on the request thread, so the
    request loop read no other message until that analysis ended.
    """

    harness, root = watched
    other = root / "other.py"
    other.write_text("import missing_w\n", encoding="utf-8")
    held = harness.hold(holding_session_lock=False)
    try:
        harness.notify(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": other.as_uri(), "type": 2}]},
        )
        assert held.entered.wait(_TIMEOUT)
        assert not held.finished.is_set()
    finally:
        held.release.set()
    harness.drain()

    assert harness.analysis_threads == ["pyinc-tools-diagnostics"]
    published = harness.published()
    assert [uri for uri, _messages in published] == [other.as_uri()]
    assert "'missing_w'" in published[0][1][0]


def test_a_failed_publish_prints_its_traceback_and_the_worker_keeps_publishing(
    watched: tuple[_WatchedServer, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness, root = watched
    path = root / "app.py"
    session = harness.server._session
    assert session is not None
    analyze = session.analyze_workspace
    calls = 0

    def fail_first_analysis() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("mirror unreadable")
        return analyze()

    monkeypatch.setattr(session, "analyze_workspace", fail_first_analysis)
    harness.open(path, "import missing_0\n")
    harness.drain()

    err = capsys.readouterr().err
    assert "Traceback (most recent call last):" in err
    assert "OSError: mirror unreadable" in err
    assert harness.published() == []

    harness.change(path, "import missing_1\n")
    harness.drain()

    published = harness.published()
    assert [uri for uri, _messages in published] == [path.as_uri()]
    assert "'missing_1'" in published[0][1][0]


def _publishes(harness: _WatchedServer, path: Path) -> list[list[dict[str, Any]]]:
    """The diagnostics of each publish for ``path``, oldest first."""
    return [
        message["params"]["diagnostics"]
        for message in harness.messages()
        if message.get("method") == "textDocument/publishDiagnostics"
        and message["params"]["uri"] == path.as_uri()
    ]


def test_a_block_left_open_at_the_end_of_the_file_publishes_its_syntax_error(
    watched: tuple[_WatchedServer, Path],
) -> None:
    """The syntax error replaces the diagnostics the file had before the edit.

    The parser reports a block with no body at the end of its line. Its range
    ended one column past the line, so the server skipped the file's
    notification and the client kept the missing import.
    """

    harness, root = watched
    path = root / "app.py"
    harness.open(path, "import missing_0\n")
    harness.drain()
    assert [item["code"] for item in _publishes(harness, path)[-1]] == ["missing-import"]

    harness.change(path, "x = 1\nif x:")
    harness.drain()

    end_of_line = {"line": 1, "character": 5}
    assert [(item["code"], item["range"]) for item in _publishes(harness, path)[-1]] == [
        ("syntax-error", {"start": end_of_line, "end": end_of_line})
    ]


def test_a_range_past_the_end_of_the_document_is_published_inside_it(
    watched: tuple[_WatchedServer, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, root = watched
    path = root / "app.py"
    session = harness.server._session
    assert session is not None
    analyze = session.analyze_workspace
    past_the_end = AnalysisDiagnostic(
        path=str(path),
        code="past-the-end",
        message="a range the document does not hold",
        severity="error",
        source="pyinc",
        range=SourceRange(SourcePosition(0, 40), SourcePosition(5, 2)),
    )

    def analysis_with_a_range_past_the_end() -> Any:
        return replace(analyze(), diagnostics=(past_the_end,))

    monkeypatch.setattr(session, "analyze_workspace", analysis_with_a_range_past_the_end)
    harness.open(path, "value = 1\n")
    harness.drain()

    assert [(item["code"], item["range"]) for item in _publishes(harness, path)[-1]] == [
        (
            "past-the-end",
            {"start": {"line": 0, "character": 9}, "end": {"line": 1, "character": 0}},
        )
    ]


def test_a_range_that_fits_again_after_a_moved_publish_is_sent(
    watched: tuple[_WatchedServer, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server compares the next analysis with the range it sent.

    The first publish moves the range to the start of the empty second line.
    After the edit the same analysis range fits the text. The server used to
    compare it with the range before the move, find no change and skip the
    publish, so the client kept the moved range.
    """

    harness, root = watched
    path = root / "app.py"
    session = harness.server._session
    assert session is not None
    analyze = session.analyze_workspace
    second_line = AnalysisDiagnostic(
        path=str(path),
        code="second-line",
        message="a range on the second line",
        severity="error",
        source="pyinc",
        range=SourceRange(SourcePosition(1, 0), SourcePosition(1, 16)),
    )

    def analysis_with_a_range_on_the_second_line() -> Any:
        return replace(analyze(), diagnostics=(second_line,))

    monkeypatch.setattr(session, "analyze_workspace", analysis_with_a_range_on_the_second_line)
    harness.open(path, "value = 1\n")
    harness.drain()
    start_of_line = {"line": 1, "character": 0}
    assert [item["range"] for item in _publishes(harness, path)[-1]] == [
        {"start": start_of_line, "end": start_of_line}
    ]

    harness.change(path, "value = 1\nimport missing_0\n")
    harness.drain()

    assert [item["range"] for item in _publishes(harness, path)[-1]] == [
        {"start": start_of_line, "end": {"line": 1, "character": 16}}
    ]


def test_a_publish_overtaken_by_an_edit_is_replaced_by_the_edits_publish(
    watched: tuple[_WatchedServer, Path],
) -> None:
    """An analysis that ran before an edit is sent, and the edit's publish follows it.

    The held analysis found the missing import on the second line. The edit
    leaves that line empty, so the range is moved to its start. The analysis
    of the edit finds nothing, and its publish clears the file.
    """

    harness, root = watched
    path = root / "app.py"
    held = harness.hold(holding_session_lock=False)
    harness.open(path, "value = 1\nimport missing_0\n")
    assert held.entered.wait(_TIMEOUT)
    try:
        harness.change(path, "value = 1\n")
    finally:
        held.release.set()
    harness.drain()

    start_of_line = {"line": 1, "character": 0}
    publishes = _publishes(harness, path)
    assert [[(item["code"], item["range"]) for item in items] for items in publishes] == [
        [("missing-import", {"start": start_of_line, "end": start_of_line})],
        [],
    ]
