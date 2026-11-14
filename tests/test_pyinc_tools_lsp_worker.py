"""How the language server delivers diagnostics to the client.

The server publishes diagnostics on its own after every edit and refresh. A
client that also pulled them would show each one twice, so the server does
not advertise pull diagnostics. It still answers a pull request from a client
that sends one anyway.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import PollingWorkspaceWatcher, WorkspaceSession

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


class _WatchedServer:
    """A server whose watcher polls only when the test calls `poll`.

    The watcher is the one `initialize` builds, with its debounce at zero. Its
    thread is never started. `poll` runs one poll and hands what it found to
    the server's callback, as the watcher thread does.
    """

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.analyses = 0
        self._on_change: Callable[[tuple[str, ...]], None] | None = None
        original = WorkspaceSession.analyze_workspace

        def counting_analyze_workspace(session: WorkspaceSession) -> Any:
            self.analyses += 1
            return original(session)

        def start_without_thread(
            watcher: PollingWorkspaceWatcher,
            on_change: Callable[[tuple[str, ...]], None],
            **_options: object,
        ) -> None:
            self._on_change = on_change

        monkeypatch.setattr(WorkspaceSession, "analyze_workspace", counting_analyze_workspace)
        monkeypatch.setattr(PollingWorkspaceWatcher, "start", start_without_thread)
        self.server = LanguageServer(
            in_stream=io.BytesIO(), out_stream=io.BytesIO(), default_root=str(root)
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

    def poll(self) -> tuple[str, ...]:
        watcher = self.server._watcher
        assert watcher is not None and self._on_change is not None
        ready = watcher.poll()
        if ready:
            self._on_change(ready)
        return ready

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
    before_save = harness.analyses

    path.write_text("value = 2\n", encoding="utf-8")
    harness.notify("textDocument/didSave", {"textDocument": {"uri": uri}})

    assert harness.poll() == ()
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
    assert harness.analyses == 1
    mirrored = Path(session.mirror_root, "other.py")
    assert mirrored.read_text(encoding="utf-8") == "other = 2\n"
