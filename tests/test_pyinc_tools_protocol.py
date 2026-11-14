from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from pyinc_tools._jsonrpc import read_message
from pyinc_tools.lsp import LanguageServer

_ORIGIN = {"line": 0, "character": 0}

# Every request that names one document. textDocument/diagnostic answers an
# empty report instead of null and is checked on its own.
_DOCUMENT_REQUESTS = (
    "textDocument/documentSymbol",
    "textDocument/hover",
    "textDocument/completion",
    "textDocument/definition",
    "textDocument/declaration",
    "textDocument/typeDefinition",
    "textDocument/references",
    "textDocument/documentHighlight",
    "textDocument/linkedEditingRange",
    "textDocument/prepareRename",
    "textDocument/rename",
    "textDocument/codeAction",
    "textDocument/signatureHelp",
    "textDocument/foldingRange",
    "textDocument/selectionRange",
    "textDocument/documentLink",
    "textDocument/codeLens",
    "textDocument/prepareCallHierarchy",
    "textDocument/prepareTypeHierarchy",
    "textDocument/inlayHint",
    "textDocument/semanticTokens/full",
    "textDocument/semanticTokens/range",
)


def _frame(payload: Any) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _initialize(root: Path) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "rootUri": root.as_uri(),
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    }


def _request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _document_params(uri: str) -> dict[str, Any]:
    return {
        "textDocument": {"uri": uri},
        "position": _ORIGIN,
        "positions": [_ORIGIN],
        "range": {"start": _ORIGIN, "end": _ORIGIN},
        "context": {"includeDeclaration": True, "diagnostics": []},
        "newName": "renamed",
    }


def _serve(messages: list[dict[str, Any]]) -> tuple[int, dict[Any, dict[str, Any]]]:
    frames = b"".join(_frame(message) for message in messages)
    frames += _frame({"jsonrpc": "2.0", "id": "shutdown", "method": "shutdown"})
    frames += _frame({"jsonrpc": "2.0", "method": "exit"})
    output = io.BytesIO()
    status = LanguageServer(in_stream=io.BytesIO(frames), out_stream=output).serve()
    output.seek(0)
    replies: dict[Any, dict[str, Any]] = {}
    while (reply := read_message(output)) is not None:
        if "id" in reply:
            replies[reply["id"]] = reply
    return status, replies


@pytest.mark.parametrize("where", ["missing file", "existing file", "untitled"])
def test_requests_for_a_document_outside_the_root_answer_null(
    where: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "mod.py").write_text("x = 1\n", encoding="utf-8")
    if where == "missing file":
        uri = (tmp_path / "elsewhere" / "x.py").as_uri()
    elif where == "existing file":
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        uri = outside.as_uri()
    else:
        uri = "untitled:Untitled-1"
    requests = [
        _request(request_id, method, _document_params(uri))
        for request_id, method in enumerate(_DOCUMENT_REQUESTS, start=1)
    ]
    diagnostic_id = len(requests) + 1
    requests.append(
        _request(diagnostic_id, "textDocument/diagnostic", {"textDocument": {"uri": uri}})
    )

    status, replies = _serve([_initialize(root), *requests])

    assert status == 0
    for request_id, method in enumerate(_DOCUMENT_REQUESTS, start=1):
        assert replies[request_id] == {"jsonrpc": "2.0", "id": request_id, "result": None}, method
    report = replies[diagnostic_id]["result"]
    assert report["kind"] == "full"
    assert report["items"] == []
    assert replies["shutdown"]["result"] is None
    assert capsys.readouterr().err == ""


def test_watched_files_batch_refreshes_the_in_root_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "first.py"
    second = root / "second.py"
    first.write_text("x = 1\n", encoding="utf-8")
    second.write_text("y = 1\n", encoding="utf-8")
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=io.BytesIO())
    server._handle_request(
        "initialize",
        {"rootUri": root.as_uri(), "initializationOptions": {"pyinc.watcher.enabled": False}},
    )
    try:
        session = server._session
        assert session is not None
        mirror = Path(session.mirror_root)
        first.write_text("x = 2\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        changes = [
            {"uri": (tmp_path / "outside.py").as_uri(), "type": 2},
            {"uri": first.as_uri(), "type": 2},
            {"uri": "untitled:Untitled-1", "type": 2},
            {"uri": 7, "type": 2},
            "not a change",
            {"uri": second.as_uri(), "type": 2},
        ]

        assert server._handle_message(
            {
                "jsonrpc": "2.0",
                "method": "workspace/didChangeWatchedFiles",
                "params": {"changes": changes},
            }
        )

        assert (mirror / "first.py").read_text(encoding="utf-8") == "x = 2\n"
        assert (mirror / "second.py").read_text(encoding="utf-8") == "y = 2\n"
    finally:
        server._teardown_session()
    assert capsys.readouterr().err == ""


def test_completion_reports_whether_the_list_was_cut(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    module = root / "names.py"
    names = "".join(f"n{index:03d} = {index}\n" for index in range(250))
    module.write_text(names + "n\nn1\n", encoding="utf-8")
    uri = module.as_uri()

    status, replies = _serve(
        [
            _initialize(root),
            _request(
                1,
                "textDocument/completion",
                {"textDocument": {"uri": uri}, "position": {"line": 250, "character": 1}},
            ),
            _request(
                2,
                "textDocument/completion",
                {"textDocument": {"uri": uri}, "position": {"line": 251, "character": 2}},
            ),
        ]
    )

    assert status == 0
    cut = replies[1]["result"]
    assert cut["isIncomplete"] is True
    assert len(cut["items"]) == 200
    whole = replies[2]["result"]
    assert whole["isIncomplete"] is False
    assert {item["label"] for item in whole["items"]} == {f"n{index}" for index in range(100, 200)}
