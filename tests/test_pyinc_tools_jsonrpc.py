from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import pyinc_tools._jsonrpc as jsonrpc_module
from pyinc_tools._document import InvalidParams, convert_payload_positions
from pyinc_tools._jsonrpc import (
    BodyParseError,
    InvalidRequest,
    ParseError,
    read_message,
    write_message,
)
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


def test_read_message_discards_oversized_body_and_reads_next_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jsonrpc_module, "_MAX_CONTENT_LENGTH", 16)
    # Larger than one discard chunk, so the body is dropped over several reads.
    body = b"x" * (3 * 64 * 1024 + 1)
    stream = io.BytesIO(
        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body + _frame({"id": 1})
    )

    with pytest.raises(BodyParseError, match="exceeds"):
        read_message(stream)
    assert read_message(stream) == {"id": 1}


def test_read_message_reports_truncated_oversized_body_as_broken_framing() -> None:
    length = jsonrpc_module._MAX_CONTENT_LENGTH + 1
    with pytest.raises(ParseError, match="truncated JSON-RPC body") as raised:
        read_message(io.BytesIO(f"Content-Length: {length}\r\n\r\n".encode("ascii")))
    assert type(raised.value) is ParseError


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
    with pytest.raises(BodyParseError, match="invalid UTF-8 JSON-RPC body"):
        read_message(stream)


def test_read_message_rejects_deep_json_as_parse_error() -> None:
    body = b'{"jsonrpc":"2.0","method":"x","params":' + b"[" * 200_000
    body += b"0" + b"]" * 200_000 + b"}"
    stream = io.BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    with pytest.raises(BodyParseError, match="invalid UTF-8 JSON-RPC body"):
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
        {"jsonrpc": "2.0", "id": 1, "method": "workspace/symbol", "params": None},
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


@pytest.mark.parametrize(
    "frame",
    [
        b"Content-Type: application/json\r\n\r\n{}",
        b"Content-Length: nope\r\n\r\n{}",
        b"Content-Length: 10000000000\r\n\r\n",
        b"Not-A-Header\r\n\r\n",
        b"Content-Length: 4\r\n\r\n{}",
    ],
)
def test_read_message_reports_broken_framing_as_plain_parse_error(frame: bytes) -> None:
    with pytest.raises(ParseError) as raised:
        read_message(io.BytesIO(frame))
    assert type(raised.value) is ParseError


_PARSE_ERROR_REPLY = {
    "jsonrpc": "2.0",
    "id": None,
    "error": {"code": -32700, "message": "Parse error"},
}
_SHUTDOWN = {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}
_EXIT = {"jsonrpc": "2.0", "method": "exit", "params": {}}
_WORKSPACE_SYMBOL = {
    "jsonrpc": "2.0",
    "id": 5,
    "method": "workspace/symbol",
    "params": {"query": ""},
}
_INTERNAL_ERROR_REPLY = {
    "jsonrpc": "2.0",
    "id": 5,
    "error": {"code": -32603, "message": "Internal error"},
}


def _initialize_request(root: Path) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "rootUri": root.as_uri(),
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    }


def _read_messages(output: io.BytesIO) -> list[dict[str, Any]]:
    output.seek(0)
    messages: list[dict[str, Any]] = []
    while (message := read_message(output)) is not None:
        messages.append(message)
    return messages


def _serve(frames: bytes) -> tuple[int, list[dict[str, Any]]]:
    output = io.BytesIO()
    status = LanguageServer(in_stream=io.BytesIO(frames), out_stream=output).serve()
    return status, _read_messages(output)


def _assert_served_lifecycle(messages: list[dict[str, Any]]) -> None:
    assert messages[0]["id"] == 1
    assert "result" in messages[0]
    assert messages[-1] == {"jsonrpc": "2.0", "id": 2, "result": None}


@pytest.mark.parametrize("body", [b"{", b"\xff", b'{"value":NaN}'])
def test_invalid_json_body_then_request_is_answered(body: bytes, tmp_path: Path) -> None:
    bad_frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    status, messages = _serve(
        bad_frame + _frame(_initialize_request(tmp_path)) + _frame(_SHUTDOWN) + _frame(_EXIT)
    )

    assert status == 0
    assert messages[0] == _PARSE_ERROR_REPLY
    _assert_served_lifecycle(messages[1:])
    assert len(messages) == 3


def test_oversize_body_is_discarded_then_request_is_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = 1024
    initialize = _initialize_request(tmp_path)
    assert len(_frame(initialize)) < limit
    # A well-formed request padded past the limit. It is dropped unanswered.
    oversize = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "workspace/symbol",
        "params": {"query": "x" * (3 * 64 * 1024)},
    }

    frames = _frame(oversize) + _frame(initialize) + _frame(_SHUTDOWN) + _frame(_EXIT)
    output = io.BytesIO()
    # The limit applies to the server's reads only. The initialize reply is
    # larger than it.
    with monkeypatch.context() as patch:
        patch.setattr(jsonrpc_module, "_MAX_CONTENT_LENGTH", limit)
        status = LanguageServer(in_stream=io.BytesIO(frames), out_stream=output).serve()
    messages = _read_messages(output)

    assert status == 0
    assert messages[0] == _PARSE_ERROR_REPLY
    _assert_served_lifecycle(messages[1:])
    assert len(messages) == 3


def test_broken_header_ends_the_server_with_status_1() -> None:
    status, messages = _serve(b"Not-A-Header\r\n\r\n" + _frame(_SHUTDOWN))

    assert status == 1
    assert messages == [_PARSE_ERROR_REPLY]


def test_shutdown_and_exit_with_null_params_stop_the_server(tmp_path: Path) -> None:
    shutdown = {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None}
    exit_notification = {"jsonrpc": "2.0", "method": "exit", "params": None}

    status, messages = _serve(
        _frame(_initialize_request(tmp_path))
        + _frame(shutdown)
        + _frame(exit_notification)
        + _frame(_WORKSPACE_SYMBOL)
    )

    assert status == 0
    _assert_served_lifecycle(messages)
    assert len(messages) == 2


def test_exit_with_null_params_without_shutdown_returns_1() -> None:
    exit_notification = {"jsonrpc": "2.0", "method": "exit", "params": None}

    status, messages = _serve(_frame(exit_notification) + _frame(_SHUTDOWN))

    assert status == 1
    assert messages == []


def _patch_workspace_symbol(monkeypatch: pytest.MonkeyPatch, handler: Callable[[], Any]) -> None:
    original = LanguageServer._dispatch_request

    def dispatch(self: LanguageServer, method: str, params: Any) -> Any:
        if method == "workspace/symbol":
            return handler()
        return original(self, method, params)

    monkeypatch.setattr(LanguageServer, "_dispatch_request", dispatch)


def test_unserializable_result_gives_internal_error_and_keeps_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_workspace_symbol(monkeypatch, lambda: {"a set", "is not JSON"})

    status, messages = _serve(
        _frame(_initialize_request(tmp_path))
        + _frame(_WORKSPACE_SYMBOL)
        + _frame(_SHUTDOWN)
        + _frame(_EXIT)
    )

    assert status == 0
    assert messages[1] == _INTERNAL_ERROR_REPLY
    _assert_served_lifecycle(messages)
    error_output = capsys.readouterr().err
    assert "Traceback" in error_output
    assert "TypeError" in error_output


@pytest.mark.parametrize("error_type", [KeyError, TypeError, ValueError, RuntimeError])
def test_handler_bug_logs_traceback_and_gives_internal_error(
    error_type: type[Exception],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken_handler() -> Any:
        raise error_type("handler bug")

    _patch_workspace_symbol(monkeypatch, broken_handler)

    status, messages = _serve(
        _frame(_initialize_request(tmp_path))
        + _frame(_WORKSPACE_SYMBOL)
        + _frame(_SHUTDOWN)
        + _frame(_EXIT)
    )

    assert status == 0
    assert messages[1] == _INTERNAL_ERROR_REPLY
    _assert_served_lifecycle(messages)
    error_output = capsys.readouterr().err
    assert "workspace/symbol" in error_output
    assert "Traceback" in error_output
    assert f"{error_type.__name__}: " in error_output


def test_result_position_outside_its_document_gives_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = tmp_path / "mod.py"
    module.write_text("x = 1\n", encoding="utf-8")
    uri = module.as_uri()
    original = LanguageServer._dispatch_request

    def dispatch(self: LanguageServer, method: str, params: Any) -> Any:
        if method == "textDocument/definition":
            line_40 = {"line": 40, "character": 0}
            return {"uri": uri, "range": {"start": line_40, "end": line_40}}
        return original(self, method, params)

    monkeypatch.setattr(LanguageServer, "_dispatch_request", dispatch)
    definition = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "textDocument/definition",
        "params": {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0}},
    }

    status, messages = _serve(
        _frame(_initialize_request(tmp_path))
        + _frame(definition)
        + _frame(_SHUTDOWN)
        + _frame(_EXIT)
    )

    assert status == 0
    assert messages[1] == _INTERNAL_ERROR_REPLY
    _assert_served_lifecycle(messages)
    error_output = capsys.readouterr().err
    assert "textDocument/definition" in error_output
    assert "Traceback" in error_output


_URI = "file:///workspace/mod.py"


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("textDocument/hover", {}),
        ("textDocument/hover", {"textDocument": _URI, "position": {"line": 0, "character": 0}}),
        ("textDocument/definition", {"textDocument": {"uri": _URI}}),
        ("textDocument/references", {"textDocument": {"uri": _URI}, "position": [0, 0]}),
        ("textDocument/selectionRange", {"textDocument": {"uri": _URI}, "positions": ["0:0"]}),
        ("textDocument/documentSymbol", {"textDocument": {"uri": 7}}),
    ],
)
def test_malformed_request_params_get_invalid_params_without_traceback(
    method: str,
    params: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = {"jsonrpc": "2.0", "id": 5, "method": method, "params": params}

    status, messages = _serve(
        _frame(_initialize_request(tmp_path)) + _frame(request) + _frame(_SHUTDOWN) + _frame(_EXIT)
    )

    assert status == 0
    assert messages[1]["id"] == 5
    assert messages[1]["error"]["code"] == -32602
    _assert_served_lifecycle(messages)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("root_uri", ["http://example.invalid/root", "missing"])
def test_refused_root_gets_invalid_params_without_traceback(
    root_uri: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if root_uri == "missing":
        root_uri = (tmp_path / "missing").as_uri()
    initialize = _initialize_request(tmp_path)
    initialize["params"]["rootUri"] = root_uri

    status, messages = _serve(_frame(initialize) + _frame(_EXIT))

    assert status == 1
    assert len(messages) == 1
    assert messages[0]["id"] == 1
    assert messages[0]["error"]["code"] == -32602
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("error_type", [KeyError, ValueError])
def test_notification_handler_bug_logs_traceback_and_keeps_serving(
    error_type: type[Exception],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = LanguageServer._handle_notification

    def handle(self: LanguageServer, method: str, params: Any) -> bool:
        if method == "initialized":
            raise error_type("handler bug")
        return original(self, method, params)

    monkeypatch.setattr(LanguageServer, "_handle_notification", handle)
    initialized = {"jsonrpc": "2.0", "method": "initialized", "params": {}}

    status, messages = _serve(
        _frame(_initialize_request(tmp_path))
        + _frame(initialized)
        + _frame(_SHUTDOWN)
        + _frame(_EXIT)
    )

    assert status == 0
    _assert_served_lifecycle(messages)
    error_output = capsys.readouterr().err
    assert "initialized" in error_output
    assert "Traceback" in error_output
    assert f"{error_type.__name__}: " in error_output


def test_malformed_notification_params_are_dropped_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    did_open = {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": (tmp_path / "mod.py").as_uri()}},
    }

    status, messages = _serve(
        _frame(_initialize_request(tmp_path)) + _frame(did_open) + _frame(_SHUTDOWN) + _frame(_EXIT)
    )

    assert status == 0
    _assert_served_lifecycle(messages)
    assert len(messages) == 2
    assert capsys.readouterr().err == ""
