"""How the language server delivers diagnostics to the client.

The server publishes diagnostics on its own after every edit and refresh. A
client that also pulled them would show each one twice, so the server does
not advertise pull diagnostics. It still answers a pull request from a client
that sends one anyway.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pyinc_tools.lsp import LanguageServer

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
