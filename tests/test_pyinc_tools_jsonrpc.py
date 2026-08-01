from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import pyinc_tools._jsonrpc as jsonrpc_module
from pyinc_tools._document import InvalidParams, convert_payload_positions
from pyinc_tools._jsonrpc import InvalidRequest, ParseError, read_message, write_message
from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import WorkspaceSession


class _FlushTrackingBytesIO(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed = False

    def flush(self) -> None:
        self.flushed = True
        super().flush()


def _frame(payload: Any) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def test_read_message_reads_one_framed_payload_at_a_time() -> None:
    first = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    second = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
    stream = io.BytesIO(_frame(first) + _frame(second))

    assert read_message(stream) == first
    assert read_message(stream) == second
    assert read_message(stream) is None


def test_read_message_accepts_case_insensitive_headers_and_utf8_body() -> None:
    payload = {"jsonrpc": "2.0", "id": "café", "result": "😀"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream = io.BytesIO(
        b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
        + f"content-length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )

    assert read_message(stream) == payload


def test_read_message_rejects_missing_content_length() -> None:
    with pytest.raises(ParseError, match="missing Content-Length header"):
        read_message(io.BytesIO(b"Content-Type: application/json\r\n\r\n{}"))


@pytest.mark.parametrize("value", [b"nope", b"-1", b"+1", b"1_0"])
def test_read_message_rejects_invalid_content_length(value: bytes) -> None:
    with pytest.raises(ParseError, match="non-negative integer"):
        read_message(io.BytesIO(b"Content-Length: " + value + b"\r\n\r\n{}"))


def test_read_message_rejects_integer_conversion_bomb_as_parse_error() -> None:
    value = b"9" * 5_000
    with pytest.raises(ParseError, match="exceeds"):
        read_message(io.BytesIO(b"Content-Length: " + value + b"\r\n\r\n"))


def test_read_message_rejects_oversized_body_before_reading_it() -> None:
    length = jsonrpc_module._MAX_CONTENT_LENGTH + 1
    with pytest.raises(ParseError, match="exceeds"):
        read_message(io.BytesIO(f"Content-Length: {length}\r\n\r\n".encode("ascii")))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"Not-A-Header\r\n\r\n", "malformed JSON-RPC header"),
        (b"Content-Length: 2\r\n", "truncated JSON-RPC headers"),
        (
            b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            "duplicate JSON-RPC header",
        ),
        (b"Content-Length: 4\r\n\r\n{}", "truncated JSON-RPC body"),
    ],
)
def test_read_message_rejects_malformed_or_truncated_frames(payload: bytes, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        read_message(io.BytesIO(payload))


@pytest.mark.parametrize("body", [b"\xff", b"{", b'{"value":NaN}'])
def test_read_message_rejects_invalid_json(body: bytes) -> None:
    stream = io.BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    with pytest.raises(ParseError, match="invalid UTF-8 JSON-RPC body"):
        read_message(stream)


def test_read_message_rejects_deep_json_as_parse_error() -> None:
    body = b'{"jsonrpc":"2.0","method":"x","params":' + b"[" * 200_000
    body += b"0" + b"]" * 200_000 + b"}"
    stream = io.BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    with pytest.raises(ParseError, match="invalid UTF-8 JSON-RPC body"):
        read_message(stream)


def test_read_message_rejects_non_object_json() -> None:
    with pytest.raises(InvalidRequest, match="must be an object"):
        read_message(io.BytesIO(_frame([])))


def test_write_message_emits_compact_framing_and_flushes() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    stream = _FlushTrackingBytesIO()

    write_message(stream, payload)

    assert stream.getvalue() == _frame(payload)
    assert stream.flushed


@pytest.mark.parametrize(
    "position",
    [
        {"line": -1, "character": 0},
        {"line": 0, "character": -1},
        {"line": 0, "character": 1},
        {"line": 0, "character": 4},
        {"line": 3, "character": 0},
        {"line": True, "character": 0},
        {"line": 0, "character": False},
    ],
)
def test_position_conversion_rejects_invalid_utf16_coordinates(
    position: dict[str, int],
) -> None:
    uri = "file:///workspace/mod.py"
    with pytest.raises(InvalidParams):
        convert_payload_positions(
            position,
            encoding="utf-16",
            to_client=False,
            source_for_uri=lambda _uri: "😀x\n",
            uri=uri,
        )


@pytest.mark.parametrize(
    "folding_range",
    [
        {"startLine": 0},
        {"startCharacter": 0},
        {"startLine": True, "startCharacter": 0},
        {"startLine": 0, "startCharacter": False},
        {"startLine": 0, "startCharacter": 1},
    ],
)
def test_folding_position_conversion_rejects_invalid_coordinates(
    folding_range: dict[str, Any],
) -> None:
    with pytest.raises(InvalidParams):
        convert_payload_positions(
            folding_range,
            encoding="utf-16",
            to_client=False,
            source_for_uri=lambda _uri: "😀x\n",
            uri="file:///workspace/mod.py",
        )


def _initialize_server(server: LanguageServer, root: Path) -> None:
    server._handle_request(
        "initialize",
        {
            "rootUri": root.as_uri(),
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    )


def test_language_server_reports_method_not_found(tmp_path: Path) -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)
    _initialize_server(server, tmp_path)
    try:
        assert server._handle_message(
            {"jsonrpc": "2.0", "id": 7, "method": "unknown/method", "params": {}}
        )
        output.seek(0)
        response = read_message(output)
        assert response is not None
        assert response["error"]["code"] == -32601
    finally:
        server._teardown_session()


@pytest.mark.parametrize("request_id", [0, "request", None])
def test_language_server_preserves_valid_request_ids(
    request_id: int | str | None, tmp_path: Path
) -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)
    _initialize_server(server, tmp_path)
    try:
        assert server._handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "unknown/method",
                "params": {},
            }
        )
        output.seek(0)
        response = read_message(output)
        assert response is not None
        assert response["id"] == request_id
        assert response["error"]["code"] == -32601
    finally:
        server._teardown_session()


@pytest.mark.parametrize(
    "message",
    [
        {"id": 1, "method": "unknown/method"},
        {"jsonrpc": "1.0", "id": 1, "method": "unknown/method"},
        {"jsonrpc": "2.0", "id": 1, "method": 7},
        {"jsonrpc": "2.0", "id": True, "method": "unknown/method"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "unknown/method"},
        {"jsonrpc": "2.0", "id": [], "method": "unknown/method"},
        {"jsonrpc": "2.0", "id": {}, "method": "unknown/method"},
        {"jsonrpc": "2.0", "id": 1, "method": "unknown/method", "params": 1},
    ],
)
def test_language_server_rejects_invalid_request_envelopes(
    message: dict[str, Any],
) -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)

    assert server._handle_message(message)
    output.seek(0)
    response = read_message(output)
    assert response is not None
    assert response["id"] is None
    assert response["error"]["code"] == -32600


def test_language_server_reports_lsp_positional_params_as_invalid(tmp_path: Path) -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)
    _initialize_server(server, tmp_path)
    try:
        assert server._handle_message(
            {"jsonrpc": "2.0", "id": 11, "method": "shutdown", "params": []}
        )
        output.seek(0)
        response = read_message(output)
        assert response is not None
        assert response["id"] == 11
        assert response["error"]["code"] == -32602
    finally:
        server._teardown_session()


def test_language_server_rejects_request_before_initialize() -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)
    assert server._handle_message(
        {"jsonrpc": "2.0", "id": 12, "method": "workspace/symbol", "params": {}}
    )
    output.seek(0)
    response = read_message(output)
    assert response is not None
    assert response["error"]["code"] == -32002


def test_language_server_rejects_reinitialize_and_post_shutdown_requests(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)
    _initialize_server(server, tmp_path)
    try:
        assert server._handle_message(
            {"jsonrpc": "2.0", "id": 13, "method": "initialize", "params": {}}
        )
        assert server._handle_message(
            {"jsonrpc": "2.0", "id": 14, "method": "shutdown", "params": {}}
        )
        assert server._handle_message(
            {"jsonrpc": "2.0", "id": 15, "method": "workspace/symbol", "params": {}}
        )
        output.seek(0)
        responses = (read_message(output), read_message(output), read_message(output))
        assert all(response is not None for response in responses)
        assert [
            response["error"]["code"] if "error" in response else None
            for response in responses
            if response is not None
        ] == [
            -32600,
            None,
            -32600,
        ]
    finally:
        server._teardown_session()


def test_language_server_exit_status_tracks_shutdown(tmp_path: Path) -> None:
    exit_notification = {"jsonrpc": "2.0", "method": "exit", "params": {}}
    assert (
        LanguageServer(
            in_stream=io.BytesIO(_frame(exit_notification)), out_stream=io.BytesIO()
        ).serve()
        == 1
    )

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "rootUri": tmp_path.as_uri(),
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    }
    shutdown = {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}
    server = LanguageServer(
        in_stream=io.BytesIO(_frame(initialize) + _frame(shutdown) + _frame(exit_notification)),
        out_stream=io.BytesIO(),
    )
    assert server.serve() == 0


def test_language_server_notification_oserror_is_logged_and_loop_keeps_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = tmp_path / "mod.py"
    module.write_text("x = 1\n", encoding="utf-8")

    def failing_set_overlay(self: WorkspaceSession, path: str, text: str) -> str:
        raise OSError("mirror write failed")

    monkeypatch.setattr(WorkspaceSession, "set_overlay", failing_set_overlay)

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "rootUri": tmp_path.as_uri(),
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
    did_open = {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": module.as_uri(),
                "languageId": "python",
                "version": 1,
                "text": "x = 1\n",
            }
        },
    }
    shutdown = {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}
    exit_notification = {"jsonrpc": "2.0", "method": "exit", "params": {}}
    output = io.BytesIO()
    server = LanguageServer(
        in_stream=io.BytesIO(
            _frame(initialize)
            + _frame(initialized)
            + _frame(did_open)
            + _frame(shutdown)
            + _frame(exit_notification)
        ),
        out_stream=output,
    )

    assert server.serve() == 0

    output.seek(0)
    responses: dict[Any, dict[str, Any]] = {}
    while True:
        message = read_message(output)
        if message is None:
            break
        if "id" in message:
            responses[message["id"]] = message
    assert "result" in responses[1]
    assert responses[2] == {"jsonrpc": "2.0", "id": 2, "result": None}
    assert "OSError" in capsys.readouterr().err


def test_language_server_serves_parse_error_response() -> None:
    body = b"{"
    output = io.BytesIO()
    server = LanguageServer(
        in_stream=io.BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body),
        out_stream=output,
    )

    assert server.serve() == 0
    output.seek(0)
    response = read_message(output)
    assert response is not None
    assert response["id"] is None
    assert response["error"]["code"] == -32700
    assert read_message(output) is None


def test_language_server_serves_invalid_request_response() -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(_frame([])), out_stream=output)

    assert server.serve() == 0
    output.seek(0)
    response = read_message(output)
    assert response is not None
    assert response["id"] is None
    assert response["error"]["code"] == -32600
    assert read_message(output) is None


def test_language_server_reports_invalid_params(tmp_path: Path) -> None:
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)
    server._handle_request(
        "initialize",
        {
            "rootUri": tmp_path.as_uri(),
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    )
    try:
        assert server._handle_message(
            {"jsonrpc": "2.0", "id": 8, "method": "textDocument/hover", "params": {}}
        )
        output.seek(0)
        response = read_message(output)
        assert response is not None
        assert response["error"]["code"] == -32602
    finally:
        server._teardown_session()


def test_language_server_rejects_split_surrogate_position(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("😀foo = 1\n", encoding="utf-8")
    output = io.BytesIO()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=output)
    server._handle_request(
        "initialize",
        {
            "rootUri": tmp_path.as_uri(),
            "capabilities": {"general": {"positionEncodings": ["utf-16"]}},
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    )
    try:
        assert server._handle_message(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": path.as_uri()},
                    "position": {"line": 0, "character": 1},
                },
            }
        )
        output.seek(0)
        response = read_message(output)
        assert response is not None
        assert response["error"]["code"] == -32602
    finally:
        server._teardown_session()
