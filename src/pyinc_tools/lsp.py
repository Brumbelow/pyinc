from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import threading
import traceback
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from pyinc.integrations import (
    DocumentMap,
    PositionEncoding,
    SourcePosition,
    SourceRange,
    Symbol,
)

from ._document import InvalidParams, convert_payload_positions, negotiate_position_encoding
from ._jsonrpc import (
    BodyParseError,
    InvalidRequest,
    ParseError,
    read_message,
    validate_request,
    write_message,
)
from ._workspace import (
    DEFAULT_MAX_WORKSPACE_FILES,
    SESSION_CLOSED_MESSAGE,
    WorkspaceTooLargeError,
)
from .session import (
    AnalysisDiagnostic,
    CallHierarchyCallSite,
    CallHierarchyItem,
    PollingWorkspaceWatcher,
    SemanticToken,
    TypeHierarchyItem,
    WorkspaceSession,
)

_LSP_SYMBOL_KINDS = {
    "file": 1,
    "module": 2,
    "namespace": 3,
    "package": 4,
    "class": 5,
    "method": 6,
    "property": 7,
    "field": 8,
    "constructor": 9,
    "enum": 10,
    "interface": 11,
    "function": 12,
    "variable": 13,
    "constant": 14,
}

_PYINC_SYMBOL_KIND_TO_LSP = {
    "function": 12,
    "method": 6,
    "class": 5,
    "class_variable": 8,
    "variable": 13,
    "import_alias": 13,
    "from_import_alias": 13,
    "wildcard_import_stub": 13,
}

_PYINC_SEVERITY_TO_LSP = {
    "error": 1,
    "warning": 2,
    "information": 3,
    "hint": 4,
}

# LSP DiagnosticTag enum values (LSP 3.18).
_PYINC_DIAGNOSTIC_TAG_TO_LSP = {
    "unnecessary": 1,
    "deprecated": 2,
}

_DOCUMENT_HIGHLIGHT_KINDS = {
    "text": 1,
    "read": 2,
    "write": 3,
}

_FOLDING_RANGE_KINDS = {
    "comment": "comment",
    "imports": "imports",
    "region": "region",
}

# LSP CompletionItemKind enum values (LSP 3.18).
_COMPLETION_ITEM_KIND = {
    "method": 2,
    "function": 3,
    "field": 5,
    "variable": 6,
    "class": 7,
    "module": 9,
    "keyword": 14,
}

_LSP_REQUEST_FAILED = -32803
_LSP_SERVER_NOT_INITIALIZED = -32002
_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603

# Methods whose params are void. JSON-RPC 2.0 allows only an object or an
# array as params, but a method that reads nothing loses nothing by taking
# null as absent.
_VOID_PARAMS_METHODS = frozenset({"shutdown", "exit"})

# Methods whose params must carry a TextDocumentIdentifier, and those that
# must also carry a Position. These shapes are checked before a handler
# runs, so a malformed message gets Invalid params, and a KeyError or
# TypeError raised inside a handler is reported as the bug it is.
_TEXT_DOCUMENT_METHODS = frozenset(
    {
        "textDocument/didOpen",
        "textDocument/didChange",
        "textDocument/didSave",
        "textDocument/didClose",
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
        "textDocument/diagnostic",
    }
)
_POSITION_METHODS = frozenset(
    {
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
        "textDocument/signatureHelp",
        "textDocument/prepareCallHierarchy",
        "textDocument/prepareTypeHierarchy",
    }
)


def _is_position(value: object) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("line")) is int
        and type(value.get("character")) is int
    )


def _validate_params(method: str, params: Any) -> None:
    """Raise InvalidParams when ``params`` lacks a field ``method`` reads."""

    if not isinstance(params, dict):
        return
    if method in _TEXT_DOCUMENT_METHODS:
        document = params.get("textDocument")
        if not isinstance(document, dict) or not isinstance(document.get("uri"), str):
            raise InvalidParams("textDocument.uri must be a string")
        if method == "textDocument/didOpen" and not isinstance(document.get("text"), str):
            raise InvalidParams("textDocument.text must be a string")
    if method in _POSITION_METHODS and not _is_position(params.get("position")):
        raise InvalidParams("position must have integer line and character")
    if method == "textDocument/selectionRange":
        positions = params.get("positions") or []
        if not isinstance(positions, list) or not all(_is_position(item) for item in positions):
            raise InvalidParams("positions must be a list of positions")


def _log_handler_error(method: str, kind: str) -> None:
    """Print the exception being handled, with its traceback, to stderr."""

    print(
        f"pyinc-tools lsp: {method} {kind} raised:\n{traceback.format_exc()}",
        end="",
        file=sys.stderr,
    )


def _package_version() -> str:
    try:
        return version("pyinc")
    except PackageNotFoundError:
        return "0+unknown"


class _RequestFailed(Exception):
    """Raised by request handlers to surface a user-facing rejection.

    Mapped to the LSP `RequestFailed` (-32803) JSON-RPC error code.
    """


class _MethodNotFound(Exception):
    pass


class _ServerNotInitialized(Exception):
    pass


class _InvalidLifecycleRequest(Exception):
    pass


class _OutsideWorkspace(InvalidParams):
    """The URI names no file under the workspace root.

    A request about such a document gets an empty result, and a notification
    about it is dropped.
    """


def _path_to_uri(path: str) -> str:
    return Path(path).resolve(strict=False).as_uri()


def _position_to_lsp(position: SourcePosition) -> dict[str, int]:
    return {"line": position.line, "character": position.character}


def _range_to_lsp(source_range: SourceRange) -> dict[str, dict[str, int]]:
    return {
        "start": _position_to_lsp(source_range.start),
        "end": _position_to_lsp(source_range.end),
    }


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise InvalidParams(f"Unsupported URI scheme: {parsed.scheme!r}")
    return str(Path(url2pathname(parsed.path)).resolve(strict=False))


def _diagnostic_signature(diagnostic: dict[str, Any]) -> tuple[Any, ...]:
    range_ = diagnostic.get("range", {})
    start = range_.get("start", {})
    end = range_.get("end", {})
    return (
        start.get("line"),
        start.get("character"),
        end.get("line"),
        end.get("character"),
        diagnostic.get("severity"),
        diagnostic.get("source"),
        diagnostic.get("code"),
        diagnostic.get("message"),
        tuple(diagnostic.get("tags") or ()),
    )


def _diagnostics_result_id(items: list[dict[str, Any]]) -> str:
    """Content-addressed identifier for a diagnostic set.

    A pure function of the diagnostic signatures, so an unchanged file yields a
    stable id across pulls and across processes. It uses a SHA-256 digest
    because `hash()` is salted. The server can then answer with an `unchanged`
    report when the client's `previousResultId` still matches.
    """
    signatures = [_diagnostic_signature(item) for item in items]
    payload = json.dumps(signatures, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identifier_span_at_position(
    source: str, line: int, character: int
) -> tuple[str, int, int] | None:
    lines = source.splitlines()
    if not (0 <= line < len(lines)):
        return None
    text = lines[line]
    if not (0 <= character <= len(text)):
        return None
    start = character
    while start > 0 and ("a" + text[start - 1]).isidentifier():
        start -= 1
    end = character
    while end < len(text) and ("a" + text[end]).isidentifier():
        end += 1
    if start == end or not text[start:end].isidentifier():
        return None
    return text[start:end], start, end


def _identifier_at_position(source: str, line: int, character: int) -> str | None:
    span = _identifier_span_at_position(source, line, character)
    return span[0] if span is not None else None


def _format_hover_markdown(symbol: Symbol) -> str:
    header = _format_symbol_declaration(symbol)
    lines = [f"```python\n{header}\n```"]
    if symbol.import_source_module and symbol.import_source_name:
        lines.append(
            f"*re-exported from* `{symbol.import_source_module}.{symbol.import_source_name}`"
        )
    return "\n\n".join(lines)


_CALL_HIERARCHY_KIND_TO_LSP = {
    "function": _LSP_SYMBOL_KINDS["function"],
    "method": _LSP_SYMBOL_KINDS["method"],
    "class": _LSP_SYMBOL_KINDS["class"],
}


# LSP `InlayHintKind`: Type = 1, Parameter = 2.
_INLAY_HINT_KIND_TO_LSP = {
    "type": 1,
    "parameter": 2,
}


# LSP semantic-tokens legend. The order of these tuples is the protocol
# index. `tokens[i].tokenType` is encoded as the integer index of the
# matching entry in `tokenTypes`. The `tokenModifiers` field is a bitmask
# over these positions.
_SEMANTIC_TOKEN_TYPES: tuple[str, ...] = (
    "namespace",
    "class",
    "function",
    "method",
    "parameter",
    "variable",
)
_SEMANTIC_TOKEN_TYPE_INDEX = {name: index for index, name in enumerate(_SEMANTIC_TOKEN_TYPES)}

_SEMANTIC_TOKEN_MODIFIERS: tuple[str, ...] = (
    "declaration",
    "async",
)
_SEMANTIC_TOKEN_MODIFIER_BIT = {
    name: 1 << index for index, name in enumerate(_SEMANTIC_TOKEN_MODIFIERS)
}


def _encode_semantic_tokens(
    tokens: tuple[SemanticToken, ...],
    source: str,
    encoding: PositionEncoding,
) -> list[int]:
    """Encode ``tokens`` into the LSP semantic-tokens wire format.

    The wire format is a flat ``list[int]`` with five integers per token:
    ``[deltaLine, deltaStart, length, tokenType, tokenModifiers]``.
    ``deltaLine`` is relative to the previous token's line. ``deltaStart`` is
    relative to the previous token's start column when both tokens share a
    line, and absolute otherwise. ``tokenModifiers`` is a bitmask over the
    legend positions in ``_SEMANTIC_TOKEN_MODIFIERS``.
    """
    data: list[int] = []
    prev_line = 0
    prev_character = 0
    document = DocumentMap(source)
    for token in tokens:
        start = document.from_codepoint(token.range.start, encoding)
        end = document.from_codepoint(token.range.end, encoding)
        delta_line = start.line - prev_line
        delta_start = start.character if delta_line != 0 else start.character - prev_character
        modifier_mask = 0
        for modifier in token.token_modifiers:
            modifier_mask |= _SEMANTIC_TOKEN_MODIFIER_BIT[modifier]
        data.extend(
            (
                delta_line,
                delta_start,
                end.character - start.character,
                _SEMANTIC_TOKEN_TYPE_INDEX[token.token_type],
                modifier_mask,
            )
        )
        prev_line = start.line
        prev_character = start.character
    return data


def _call_hierarchy_item_to_lsp(item: CallHierarchyItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": item.name,
        "kind": _CALL_HIERARCHY_KIND_TO_LSP[item.kind],
        "uri": _path_to_uri(item.path),
        "range": _range_to_lsp(item.range),
        "selectionRange": _range_to_lsp(item.selection_range),
        "data": {"path": item.path, "qualified_name": item.qualified_name},
    }
    if item.detail is not None:
        payload["detail"] = item.detail
    return payload


def _call_site_to_lsp_range(site: CallHierarchyCallSite) -> dict[str, Any]:
    return _range_to_lsp(site.range)


def _call_hierarchy_identity_from_item(
    item: Any,
) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    if not isinstance(data, dict):
        return None
    path = data.get("path")
    qualified_name = data.get("qualified_name")
    if not isinstance(path, str) or not isinstance(qualified_name, str):
        return None
    return path, qualified_name


def _type_hierarchy_item_to_lsp(item: TypeHierarchyItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": item.name,
        "kind": _LSP_SYMBOL_KINDS["class"],
        "uri": _path_to_uri(item.path),
        "range": _range_to_lsp(item.range),
        "selectionRange": _range_to_lsp(item.selection_range),
        "data": {"path": item.path, "qualified_name": item.qualified_name},
    }
    if item.detail is not None:
        payload["detail"] = item.detail
    return payload


def _type_hierarchy_identity_from_item(
    item: Any,
) -> tuple[str, str] | None:
    # Shape matches `_call_hierarchy_identity_from_item`. Kept separate so
    # the two endpoint families can diverge if needed.
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    if not isinstance(data, dict):
        return None
    path = data.get("path")
    qualified_name = data.get("qualified_name")
    if not isinstance(path, str) or not isinstance(qualified_name, str):
        return None
    return path, qualified_name


def _format_symbol_declaration(symbol: Symbol) -> str:
    bare_name = symbol.qualified_name.rsplit(".", 1)[-1]
    if symbol.kind == "class":
        return f"class {bare_name}"
    if symbol.kind in ("function", "method") and symbol.signature is not None:
        params = ", ".join(
            (
                f"{parameter.name}: {parameter.annotation}"
                if parameter.annotation is not None
                else parameter.name
            )
            for parameter in symbol.signature.parameters
        )
        return_annotation = symbol.signature.return_annotation
        suffix = f" -> {return_annotation}" if return_annotation is not None else ""
        return f"def {bare_name}({params}){suffix}"
    if symbol.annotation is not None:
        return f"{bare_name}: {symbol.annotation}"
    return bare_name


def _refuse_broad_root(root: Path) -> None:
    """Refuse a workspace root that holds whole trees of unrelated files.

    A client started outside a project can send the home directory or a
    filesystem root, or send no root so the server falls back to its working
    directory. The server would copy every Python file under it.
    """

    resolved = root.resolve(strict=False)
    if resolved.parent == resolved:
        raise _RequestFailed(
            f"The workspace root {resolved} is a filesystem root. Open a project folder."
        )
    try:
        is_home = resolved.samefile(Path.home())
    except (OSError, RuntimeError):
        # A root that is not on disk, or a system with no home directory, has
        # no home directory to match.
        is_home = False
    if is_home:
        raise _RequestFailed(
            f"The workspace root {resolved} is the home directory. Open a project folder."
        )


class LanguageServer:
    def __init__(
        self,
        *,
        in_stream: BinaryIO | None = None,
        out_stream: BinaryIO | None = None,
        default_root: str | None = None,
    ) -> None:
        self._input = in_stream or sys.stdin.buffer
        self._output = out_stream or sys.stdout.buffer
        self._default_root = default_root
        self._session: WorkspaceSession | None = None
        self._watcher: PollingWorkspaceWatcher | None = None
        self._initialized = False
        self._shutdown_requested = False
        self._exit_status = 0
        self._position_encoding: PositionEncoding = "utf-16"
        self._published_paths: set[str] = set()
        self._published_signatures: dict[str, tuple[tuple[Any, ...], ...]] = {}
        # The last full text the client sent for each open file, by real path.
        # Only the request loop reads or writes it.
        self._client_texts: dict[str, str] = {}
        # Diagnostics are published by one worker thread, while the request
        # loop writes responses. These locks keep them apart. A thread that
        # holds more than one takes them in this order: the publish lock, then
        # the session's lock (taken by an analysis and by reading document
        # text), then the write lock.
        #
        # The publish lock is held for a whole publish, from before the
        # analysis until the last notification is sent. Publishes therefore
        # reach the client in the order their analyses ran. It also guards the
        # published-diagnostics bookkeeping. Teardown never takes it, because
        # the diagnostics worker may hold it while teardown joins the worker.
        #
        # The write lock keeps each frame on the output stream whole (a frame
        # is two writes plus a flush). `_session` is set and cleared under it
        # too, so a send that holds it knows whether the session it analyzed
        # is still the server's. It is never held while a session is closed
        # or the diagnostics worker joined, because the worker may wait for it.
        # Shutdown sets `_shutdown_requested` under it, so a publish is sent
        # before the shutdown answer or not at all.
        #
        # The worker lock guards the worker's thread, its two flags and the
        # thread last waited for. It is never held together with another lock,
        # so it has no place in the order above. Holding it, a thread only
        # reads or sets those fields and starts the thread.
        self._publish_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._worker_lock = threading.Condition(threading.Lock())
        # The thread running publishes, or None while no run is due. It exits
        # when no run is due, and the next change starts a new one.
        self._worker_thread: threading.Thread | None = None
        # A change has landed since the worker began its current run, so one
        # more run is due.
        self._workspace_dirty = False
        # Set by shutdown and teardown. No run starts while it is set.
        self._worker_stopped = False
        # The worker thread a stop already joined. exit joins it for up to 5 s,
        # and the teardown that follows does not join it again. Closing the
        # session still waits for a running analysis to release the session
        # lock.
        self._joined_worker: threading.Thread | None = None

    def serve(self) -> int:
        try:
            while True:
                try:
                    message = self._read_message()
                except BodyParseError:
                    # The frame was read whole, so the next one can still be
                    # found and the loop keeps serving.
                    self._send_error(None, _JSONRPC_PARSE_ERROR, "Parse error")
                    continue
                except ParseError:
                    # Broken headers leave no way to find the next frame.
                    self._send_error(None, _JSONRPC_PARSE_ERROR, "Parse error")
                    return 1
                except InvalidRequest:
                    self._send_error(None, _JSONRPC_INVALID_REQUEST, "Invalid Request")
                    continue
                if message is None:
                    return 0
                if not self._handle_message(message):
                    return self._exit_status
        finally:
            self._teardown_session()

    def _handle_message(self, message: dict[str, Any]) -> bool:
        if (
            message.get("params", {}) is None
            and isinstance(message.get("method"), str)
            and message["method"] in _VOID_PARAMS_METHODS
        ):
            message = {key: value for key, value in message.items() if key != "params"}
        try:
            validate_request(message)
        except InvalidRequest:
            self._send_error(None, _JSONRPC_INVALID_REQUEST, "Invalid Request")
            return True

        method = message["method"]
        params = message.get("params", {})

        if "id" in message:
            request_id = message["id"]
            try:
                result = self._handle_request(method, params)
            except _ServerNotInitialized:
                self._send_error(
                    request_id,
                    _LSP_SERVER_NOT_INITIALIZED,
                    "Server not initialized",
                )
            except _InvalidLifecycleRequest as exc:
                self._send_error(request_id, _JSONRPC_INVALID_REQUEST, str(exc))
            except _RequestFailed as exc:
                self._send_error(request_id, _LSP_REQUEST_FAILED, str(exc))
            except _MethodNotFound:
                self._send_error(request_id, _JSONRPC_METHOD_NOT_FOUND, "Method not found")
            except InvalidParams as exc:
                # Raised by the params checks, by the conversion of the client's
                # positions, and where _uri_to_path or _initialize refuse the
                # root the client named.
                self._send_error(
                    request_id,
                    _JSONRPC_INVALID_PARAMS,
                    f"Invalid params: {exc}",
                )
            except Exception:
                # Anything else is a fault in the server, most often a bug.
                # The traceback goes to stderr, which clients keep as the
                # server's log.
                _log_handler_error(method, "request")
                self._send_error(request_id, _JSONRPC_INTERNAL_ERROR, "Internal error")
            else:
                try:
                    self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
                except (TypeError, ValueError):
                    # The result does not serialize to JSON. Nothing has been
                    # written yet, so the request still gets a reply.
                    _log_handler_error(method, "request")
                    self._send_error(request_id, _JSONRPC_INTERNAL_ERROR, "Internal error")
            return True

        try:
            _validate_params(method, params)
            return self._handle_notification(method, params)
        except InvalidParams:
            return True
        except Exception:
            # Notifications have no response to carry an error. So a failed
            # handler (e.g. an OSError from a mirror write) is logged with its
            # traceback and the loop keeps serving, like the request branch.
            _log_handler_error(method, "notification")
            return True

    def _handle_request(self, method: str, params: Any) -> Any:
        if method == "initialize":
            if self._initialized or self._shutdown_requested:
                raise _InvalidLifecycleRequest("Initialize request received more than once")
        elif not self._initialized:
            raise _ServerNotInitialized
        elif self._shutdown_requested:
            raise _InvalidLifecycleRequest("Request received after shutdown")
        if not isinstance(params, dict):
            raise InvalidParams("LSP params must be an object")
        _validate_params(method, params)
        if method == "initialize":
            return self._dispatch_request(method, params)
        uri = self._default_uri(params)
        converted_params = convert_payload_positions(
            params,
            encoding=self._position_encoding,
            to_client=False,
            source_for_uri=self._source_for_uri,
            uri=uri,
        )
        try:
            result = self._dispatch_request(method, converted_params)
        except _OutsideWorkspace:
            # The server has nothing to say about a document outside the root,
            # such as a stdlib file or an untitled buffer. Null is an empty
            # result for every request that names one. textDocument/diagnostic
            # needs a report, so it answers an empty one itself.
            return None
        try:
            return convert_payload_positions(
                result,
                encoding=self._position_encoding,
                to_client=True,
                source_for_uri=self._source_for_uri,
                uri=uri,
            )
        except InvalidParams as exc:
            # The result holds a position its document does not have. The
            # client sent nothing wrong, so this is reported as a server fault.
            raise RuntimeError(f"{method} result has a bad position: {exc}") from exc

    def _dispatch_request(self, method: str, params: Any) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "shutdown":
            # Answered without waiting for a running analysis, which holds the
            # session lock. The worker and the watcher are told to stop, and
            # exit or the end of input closes the session. The flag is set
            # under the write lock, so no diagnostics follow this answer.
            with self._write_lock:
                self._shutdown_requested = True
            self._stop_diagnostics_worker(wait=False)
            self._stop_watcher(wait=False)
            return None
        if method == "textDocument/documentSymbol":
            return self._document_symbols(params)
        if method == "workspace/symbol":
            return self._workspace_symbols(params)
        if method == "textDocument/hover":
            return self._hover(params)
        if method == "textDocument/completion":
            return self._completion(params)
        if method == "textDocument/definition":
            return self._definition(params)
        if method == "textDocument/declaration":
            return self._declaration(params)
        if method == "textDocument/typeDefinition":
            return self._type_definition(params)
        if method == "textDocument/references":
            return self._references(params)
        if method == "textDocument/documentHighlight":
            return self._document_highlight(params)
        if method == "textDocument/linkedEditingRange":
            return self._linked_editing_range(params)
        if method == "textDocument/prepareRename":
            return self._prepare_rename(params)
        if method == "textDocument/rename":
            return self._rename(params)
        if method == "textDocument/codeAction":
            return self._code_action(params)
        if method == "textDocument/signatureHelp":
            return self._signature_help(params)
        if method == "textDocument/foldingRange":
            return self._folding_range(params)
        if method == "textDocument/selectionRange":
            return self._selection_range(params)
        if method == "textDocument/documentLink":
            return self._document_link(params)
        if method == "textDocument/codeLens":
            return self._code_lens(params)
        if method == "textDocument/prepareCallHierarchy":
            return self._prepare_call_hierarchy(params)
        if method == "callHierarchy/incomingCalls":
            return self._call_hierarchy_incoming_calls(params)
        if method == "callHierarchy/outgoingCalls":
            return self._call_hierarchy_outgoing_calls(params)
        if method == "textDocument/prepareTypeHierarchy":
            return self._prepare_type_hierarchy(params)
        if method == "typeHierarchy/supertypes":
            return self._type_hierarchy_supertypes(params)
        if method == "typeHierarchy/subtypes":
            return self._type_hierarchy_subtypes(params)
        if method == "textDocument/inlayHint":
            return self._inlay_hint(params)
        if method == "textDocument/semanticTokens/full":
            return self._semantic_tokens_full(params)
        if method == "textDocument/semanticTokens/range":
            return self._semantic_tokens_range(params)
        if method == "textDocument/diagnostic":
            return self._document_diagnostic(params)
        if method == "workspace/diagnostic":
            return self._workspace_diagnostic(params)
        if method == "workspace/willRenameFiles":
            return self._will_rename_files(params)
        if method == "workspace/willDeleteFiles":
            return self._will_delete_files(params)
        raise _MethodNotFound(method)

    def _handle_notification(self, method: str, params: Any) -> bool:
        if not isinstance(params, dict):
            raise InvalidParams("LSP params must be an object")
        if method == "exit":
            self._exit_status = 0 if self._shutdown_requested else 1
            self._stop_watcher()
            self._stop_diagnostics_worker()
            return False
        if not self._initialized or self._shutdown_requested:
            return True
        if method == "initialized":
            self._schedule_diagnostics()
            return True
        params = convert_payload_positions(
            params,
            encoding=self._position_encoding,
            to_client=False,
            source_for_uri=self._source_for_uri,
            uri=self._default_uri(params),
        )
        # The document handlers change the overlay at once, so a request that
        # follows sees the edit. The analysis runs later on the worker.
        if method == "textDocument/didOpen":
            document = params["textDocument"]
            path = self._require_safe_path(document["uri"])
            self._require_session().set_overlay(path, document["text"])
            self._client_texts[path] = document["text"]
            self._schedule_diagnostics()
            return True
        if method == "textDocument/didChange":
            document = params["textDocument"]
            changes = params.get("contentChanges", [])
            if changes:
                latest = changes[-1]
                if "text" in latest:
                    path = self._require_safe_path(document["uri"])
                    # The overlay already holds this text, so there is nothing
                    # to write or analyze.
                    if self._client_texts.get(path) == latest["text"]:
                        return True
                    self._require_session().set_overlay(path, latest["text"])
                    self._client_texts[path] = latest["text"]
                    self._schedule_diagnostics()
            return True
        if method == "textDocument/didSave":
            document = params["textDocument"]
            path = self._require_safe_path(document["uri"])
            self._require_session().clear_overlay(path)
            # The mirror now holds the disk text, which can differ from the
            # buffer. The next didChange is written whatever it holds.
            self._client_texts.pop(path, None)
            self._schedule_diagnostics()
            return True
        if method == "textDocument/didClose":
            document = params["textDocument"]
            path = self._require_safe_path(document["uri"])
            self._require_session().clear_overlay(path)
            self._client_texts.pop(path, None)
            self._schedule_diagnostics()
            return True
        if method == "workspace/didChangeWatchedFiles":
            changes = params.get("changes", [])
            paths: list[str] = []
            for item in changes:
                # A bad entry is skipped alone, so the rest of the batch is
                # still refreshed.
                if not isinstance(item, dict) or not isinstance(item.get("uri"), str):
                    continue
                try:
                    paths.append(self._require_safe_path(item["uri"]))
                except _OutsideWorkspace:
                    continue
            if paths:
                self._require_session().refresh_paths(paths)
                self.publish_workspace_diagnostics()
            return True
        return True

    def publish_workspace_diagnostics(self) -> None:
        # The publish lock is held from before the analysis until the last
        # send. A publish whose analysis ran before an edit therefore sends
        # before the edit's publish, and the client ends with the diagnostics
        # of the latest analysis.
        with self._publish_lock:
            # The worker calls this while the request loop may be tearing the
            # session down. So the session is read once, because a second read
            # could find None. A session torn down meanwhile is closed, and the
            # server leaves its analysis unpublished.
            session = self._session
            if session is None:
                return
            try:
                result = session.analyze_workspace()
            except RuntimeError as exc:
                if str(exc) == SESSION_CLOSED_MESSAGE and self._session is not session:
                    return
                raise
            grouped: dict[str, list[dict[str, Any]]] = {}
            for diagnostic in result.diagnostics:
                grouped.setdefault(diagnostic.path, []).append(
                    self._analysis_diagnostic_to_lsp(diagnostic)
                )

            for path in sorted(set(grouped) | self._published_paths):
                diagnostics = grouped.get(path, [])
                signature = tuple(_diagnostic_signature(item) for item in diagnostics)
                if self._published_signatures.get(path) == signature:
                    continue
                try:
                    sent = self._send_notification(
                        "textDocument/publishDiagnostics",
                        {
                            "uri": _path_to_uri(path),
                            "diagnostics": diagnostics,
                        },
                        session=session,
                    )
                except InvalidParams:
                    # Positions are converted against the live document text.
                    # An edit made after the analysis can leave a range outside
                    # that text. The path is skipped and left unrecorded, and
                    # the publish that follows the edit sends the diagnostics
                    # the edit produced.
                    continue
                if not sent:
                    return
                # Recorded only once the client has the notification, so the
                # bookkeeping always describes what the client was sent.
                if diagnostics:
                    self._published_signatures[path] = signature
                    self._published_paths.add(path)
                else:
                    self._published_signatures.pop(path, None)
                    self._published_paths.discard(path)

    def _document_diagnostic(self, params: Any) -> dict[str, Any]:
        document = params["textDocument"]
        previous_result_id = params.get("previousResultId")
        try:
            real_path = self._require_safe_path(document["uri"])
        except ValueError:
            # A pull for a document outside the workspace succeeds with an
            # empty report.
            items: list[dict[str, Any]] = []
        else:
            result = self._require_session().analyze_file(real_path)
            items = [
                self._analysis_diagnostic_to_lsp(diagnostic) for diagnostic in result.diagnostics
            ]
        result_id = _diagnostics_result_id(items)
        if previous_result_id is not None and previous_result_id == result_id:
            return {"kind": "unchanged", "resultId": result_id}
        return {"kind": "full", "resultId": result_id, "items": items}

    def _workspace_diagnostic(self, params: Any) -> dict[str, Any]:
        previous_by_uri: dict[str, Any] = {}
        for entry in params.get("previousResultIds") or []:
            uri = entry.get("uri")
            if uri is not None:
                previous_by_uri[uri] = entry.get("value")

        result = self._require_session().analyze_workspace()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for diagnostic in result.diagnostics:
            grouped.setdefault(diagnostic.path, []).append(
                self._analysis_diagnostic_to_lsp(diagnostic)
            )
        # Surface every analyzed file even when it is now clean, so a client
        # that previously saw problems receives an empty report that clears
        # them.
        for file_result in result.files:
            grouped.setdefault(file_result.path, [])

        reports: list[dict[str, Any]] = []
        for path in sorted(grouped):
            items = grouped[path]
            uri = _path_to_uri(path)
            result_id = _diagnostics_result_id(items)
            if previous_by_uri.get(uri) == result_id:
                reports.append(
                    {
                        "kind": "unchanged",
                        "uri": uri,
                        "version": None,
                        "resultId": result_id,
                    }
                )
            else:
                reports.append(
                    {
                        "kind": "full",
                        "uri": uri,
                        "version": None,
                        "resultId": result_id,
                        "items": items,
                    }
                )
        return {"items": reports}

    def _initialize(self, params: Any) -> dict[str, Any]:
        root = self._workspace_root_from_params(params)
        _refuse_broad_root(Path(root))
        capabilities = params.get("capabilities") if isinstance(params, dict) else None
        self._position_encoding = negotiate_position_encoding(capabilities)

        options = {}
        if isinstance(params, dict):
            init_options = params.get("initializationOptions")
            if isinstance(init_options, dict):
                options = init_options

        raw_exclusions = options.get("pyinc.workspace.exclude", ())
        exclude_globs = (
            tuple(item for item in raw_exclusions if isinstance(item, str))
            if isinstance(raw_exclusions, list)
            else ()
        )
        raw_max_files = options.get("pyinc.workspace.maxFiles")
        max_files = (
            raw_max_files
            if isinstance(raw_max_files, int)
            and not isinstance(raw_max_files, bool)
            and raw_max_files >= 1
            else DEFAULT_MAX_WORKSPACE_FILES
        )
        try:
            try:
                session = WorkspaceSession(root, exclude_globs=exclude_globs, max_files=max_files)
            except WorkspaceTooLargeError:
                raise
            except ValueError as exc:
                # The session refuses a root that is not a directory, or that
                # holds a file it cannot mirror.
                raise InvalidParams(str(exc)) from exc
            with self._publish_lock, self._write_lock:
                self._session = session
                self._published_paths.clear()
                self._published_signatures.clear()
            # A failed initialize tore down and stopped the worker. A later
            # one that succeeds publishes again.
            with self._worker_lock:
                self._worker_stopped = False

            watcher_enabled = bool(options.get("pyinc.watcher.enabled", True))
            if watcher_enabled:
                debounce_ms = int(options.get("pyinc.watcher.debounceMs", 200))
                interval_ms = options.get("pyinc.watcher.intervalMs")
                interval_s: float | None
                if isinstance(interval_ms, (int, float)):
                    interval_s = float(interval_ms) / 1000.0
                else:
                    interval_s = None
                watcher = PollingWorkspaceWatcher(session, debounce_ms=debounce_ms)
                self._watcher = watcher
                watcher.start(self._on_watcher_change, interval_s=interval_s)
        except WorkspaceTooLargeError as exc:
            self._teardown_session()
            raise _RequestFailed(str(exc)) from exc
        except BaseException:
            self._teardown_session()
            raise
        self._initialized = True

        return {
            "capabilities": {
                "positionEncoding": self._position_encoding,
                "textDocumentSync": {
                    "openClose": True,
                    "change": 1,
                    "save": {"includeText": False},
                },
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "hoverProvider": True,
                "completionProvider": {
                    "triggerCharacters": ["."],
                    "resolveProvider": False,
                },
                "definitionProvider": True,
                "declarationProvider": True,
                "typeDefinitionProvider": True,
                "referencesProvider": True,
                "documentHighlightProvider": True,
                "linkedEditingRangeProvider": True,
                "renameProvider": {"prepareProvider": True},
                "codeActionProvider": {"codeActionKinds": ["quickfix"]},
                "signatureHelpProvider": {
                    "triggerCharacters": ["(", ","],
                    "retriggerCharacters": [","],
                },
                "foldingRangeProvider": True,
                "selectionRangeProvider": True,
                "documentLinkProvider": {"resolveProvider": False},
                "codeLensProvider": {"resolveProvider": False},
                "callHierarchyProvider": True,
                "typeHierarchyProvider": True,
                "inlayHintProvider": {"resolveProvider": False},
                "semanticTokensProvider": {
                    "legend": {
                        "tokenTypes": list(_SEMANTIC_TOKEN_TYPES),
                        "tokenModifiers": list(_SEMANTIC_TOKEN_MODIFIERS),
                    },
                    "full": True,
                    "range": True,
                },
                # Diagnostics are pushed after every edit and refresh. A client
                # that could also pull them would show each one twice, so pull
                # is not advertised. The two pull methods still answer a client
                # that calls them.
                "workspace": {
                    "fileOperations": {
                        "willRename": {
                            "filters": [
                                {
                                    "scheme": "file",
                                    "pattern": {
                                        "glob": "**/*.py",
                                        "matches": "file",
                                    },
                                }
                            ]
                        },
                        "willDelete": {
                            "filters": [
                                {
                                    "scheme": "file",
                                    "pattern": {
                                        "glob": "**/*.py",
                                        "matches": "file",
                                    },
                                }
                            ]
                        },
                    }
                },
            },
            "serverInfo": {"name": "pyinc-tools", "version": _package_version()},
        }

    def _on_watcher_change(self, _changed: tuple[str, ...]) -> None:
        # The watcher has already refreshed the paths. The analysis runs on the
        # worker, so the watcher thread goes back to polling.
        self._schedule_diagnostics()

    def _schedule_diagnostics(self) -> None:
        """Have the worker publish diagnostics for the current workspace state.

        A change that lands while a run is going leaves one more run due, and
        any number of such changes leave one. That run analyzes the state as it
        is when it starts, so the last publish describes the latest change.
        """
        with self._worker_lock:
            if self._worker_stopped:
                return
            self._workspace_dirty = True
            if self._worker_thread is not None:
                return
            thread = threading.Thread(
                target=self._run_diagnostics_worker,
                name="pyinc-tools-diagnostics",
                daemon=True,
            )
            self._worker_thread = thread
            try:
                thread.start()
            except BaseException:
                self._worker_thread = None
                raise

    def _run_diagnostics_worker(self) -> None:
        # One run is one publish. `publish_workspace_diagnostics` holds the
        # publish lock for the whole run, so runs and their sends keep the
        # order they started in.
        try:
            while self._take_diagnostics_run():
                try:
                    self.publish_workspace_diagnostics()
                except Exception as exc:
                    # A session closed while it was still attached has nothing
                    # to publish, so that error is not reported. Teardown
                    # detaches the session first, and the publish returns
                    # without raising then.
                    if isinstance(exc, RuntimeError) and str(exc) == SESSION_CLOSED_MESSAGE:
                        continue
                    print(
                        f"pyinc-tools lsp: publishDiagnostics raised: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
        finally:
            # `_take_diagnostics_run` clears the thread when it ends the loop.
            # This covers an exception that escaped the loop.
            with self._worker_lock:
                if self._worker_thread is threading.current_thread():
                    self._worker_thread = None
                    self._worker_lock.notify_all()

    def _take_diagnostics_run(self) -> bool:
        """Claim the due run, or retire this thread when none is due.

        Both happen under one hold of the worker lock. A change that lands
        after a thread retires finds no thread and starts one. A change that
        lands before is claimed by this thread.
        """
        with self._worker_lock:
            if self._workspace_dirty and not self._worker_stopped:
                self._workspace_dirty = False
                return True
            self._worker_thread = None
            self._worker_lock.notify_all()
            return False

    def _stop_diagnostics_worker(self, *, wait: bool = True) -> None:
        """Start no further diagnostics run.

        With ``wait``, wait up to 5 s for the running one to end, as the
        watcher's teardown waits for its thread. Each thread is joined once,
        so exit and the teardown after it join it for at most 5 s. Closing
        the session afterwards still waits for a running analysis to release
        the session lock, so the server ends when that analysis ends.
        """
        with self._worker_lock:
            self._worker_stopped = True
            self._workspace_dirty = False
            thread = self._worker_thread
            if not wait or thread is None or thread is threading.current_thread():
                return
            if thread is self._joined_worker:
                return
            self._joined_worker = thread
        thread.join(5.0)
        if thread.is_alive():
            print(
                "pyinc-tools lsp: diagnostics worker did not stop within timeout",
                file=sys.stderr,
            )

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Return True once no diagnostics run is going or due, or False on timeout.

        Tests call this to see the publishes that follow a notification.
        """
        with self._worker_lock:
            return self._worker_lock.wait_for(lambda: self._worker_thread is None, timeout)

    def _stop_watcher(self, *, wait: bool = True) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        self._watcher = None
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            if wait:
                watcher.stop()
            else:
                # The thread may be waiting for the session lock. Closing the
                # session joins it, because it registered with the session.
                watcher._request_stop()

    def _teardown_session(self) -> None:
        self._stop_watcher()
        self._stop_diagnostics_worker()
        # Detached before it is closed, and under the write lock, so a publish
        # still running on the worker (its join can time out) or on a caller's
        # thread sees that the session is gone instead of reading it closed, or
        # reading None where a moment before it found a session. The publish
        # lock is left alone, because that publish may still hold it.
        with self._write_lock:
            session = self._session
            self._session = None
        if session is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                session.close()

    def _document_symbols(self, params: Any) -> list[dict[str, Any]]:
        document = params["textDocument"]
        table = self._require_session()._symbol_table_for_file(
            self._require_safe_path(document["uri"])
        )
        if table is None:
            return []
        symbols: list[dict[str, Any]] = []
        for symbol in table.symbols:
            range_payload = _range_to_lsp(symbol.range)
            symbols.append(
                {
                    "name": symbol.qualified_name,
                    "kind": _PYINC_SYMBOL_KIND_TO_LSP.get(
                        symbol.kind, _LSP_SYMBOL_KINDS["variable"]
                    ),
                    "range": range_payload,
                    "selectionRange": range_payload,
                }
            )
        return symbols

    def _workspace_symbols(self, params: Any) -> list[dict[str, Any]]:
        query = str(params.get("query", "")).lower()
        index, module_to_path = self._require_session()._workspace_symbol_entries()
        matches: list[dict[str, Any]] = []
        for entry in index.entries:
            if query and query not in entry.qualified_name.lower():
                continue
            path = module_to_path.get(entry.module)
            if path is None:
                continue
            matches.append(
                {
                    "name": entry.qualified_name,
                    "kind": _PYINC_SYMBOL_KIND_TO_LSP.get(
                        entry.kind, _LSP_SYMBOL_KINDS["variable"]
                    ),
                    "location": {
                        "uri": _path_to_uri(path),
                        "range": _range_to_lsp(entry.range),
                    },
                    "containerName": entry.module,
                }
            )
        return matches

    def _hover(self, params: Any) -> dict[str, Any] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        try:
            target = session.symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return None
        if target is None:
            return None
        table = session._symbol_table_for_file(target.path)
        if table is None:
            return None
        symbol = next(
            (
                item
                for item in table.symbols
                if item.range == target.declaration
                and item.qualified_name.rsplit(".", 1)[-1] == target.name
            ),
            None,
        )
        if symbol is None:
            return None
        return {"contents": {"kind": "markdown", "value": _format_hover_markdown(symbol)}}

    def _completion(self, params: Any) -> dict[str, Any]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        try:
            items, truncated = session._completion_list(real_path, line, character)
        except FileNotFoundError:
            items, truncated = (), False
        payload: list[dict[str, Any]] = []
        for item in items:
            entry: dict[str, Any] = {"label": item.label}
            kind = _COMPLETION_ITEM_KIND.get(item.kind)
            if kind is not None:
                entry["kind"] = kind
            if item.detail is not None:
                entry["detail"] = item.detail
            entry["sortText"] = item.sort_text
            payload.append(entry)
        # A cut list is incomplete, so the client asks again as the prefix
        # grows instead of filtering the first items.
        return {"isIncomplete": truncated, "items": payload}

    def _definition(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        try:
            target = session.symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return []
        if target is None:
            return []
        return [
            {
                "uri": _path_to_uri(target.path),
                "range": _range_to_lsp(target.declaration),
            }
        ]

    def _declaration(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        source = session.source_text(real_path)
        if source is None:
            return []
        try:
            target = session._local_symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return []
        if target is None:
            return []
        location = session.declaration_location_at(target)
        if location is None:
            return []
        return [
            {
                "uri": _path_to_uri(location.path),
                "range": _range_to_lsp(location.range),
            }
        ]

    def _type_definition(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        source = session.source_text(real_path)
        if source is None:
            return []
        try:
            target = session.symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return []
        if target is None:
            return []
        try:
            locations = session.type_definitions_at(target)
        except FileNotFoundError:
            return []
        return [
            {
                "uri": _path_to_uri(location.path),
                "range": _range_to_lsp(location.range),
            }
            for location in locations
        ]

    def _references(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        context = params.get("context") or {}
        include_declaration = bool(context.get("includeDeclaration", True))

        source = session.source_text(real_path)
        if source is None:
            return []
        try:
            target = session.symbol_at(real_path, SourcePosition(line, character))
            if target is None:
                return []
            result = session.find_references(target, include_declaration=include_declaration)
        except FileNotFoundError:
            return []
        locations: list[dict[str, Any]] = []
        for reference in result.references:
            locations.append(
                {
                    "uri": _path_to_uri(reference.path),
                    "range": _range_to_lsp(reference.range),
                }
            )
        return locations

    def _document_highlight(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        source = session.source_text(real_path)
        if source is None:
            return []
        try:
            target = session.symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return []
        if target is None:
            return []
        try:
            highlights = session.find_document_highlights(real_path, target)
        except FileNotFoundError:
            return []
        return [
            {
                "range": _range_to_lsp(highlight.range),
                "kind": _DOCUMENT_HIGHLIGHT_KINDS[highlight.kind],
            }
            for highlight in highlights
        ]

    def _linked_editing_range(self, params: Any) -> dict[str, Any] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        source = session.source_text(real_path)
        if source is None:
            return None
        try:
            target = session.symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return None
        if target is None:
            return None
        try:
            ranges = session.linked_editing_ranges_at(real_path, target)
        except FileNotFoundError:
            return None
        if not ranges:
            return None
        return {"ranges": [_range_to_lsp(editing_range.range) for editing_range in ranges]}

    def _prepare_rename(self, params: Any) -> dict[str, Any] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        source = session.source_text(real_path)
        if source is None:
            return None
        span = _identifier_span_at_position(source, line, character)
        if span is None:
            return None
        identifier, start_col, end_col = span
        try:
            target = session.symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return None
        if target is None:
            return None
        return {
            "range": {
                "start": {"line": line, "character": start_col},
                "end": {"line": line, "character": end_col},
            },
            "placeholder": identifier,
        }

    def _rename(self, params: Any) -> dict[str, Any] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        new_name = str(params.get("newName", ""))

        source = session.source_text(real_path)
        if source is None:
            return None
        identifier = _identifier_at_position(source, line, character)
        if identifier is None:
            return None
        try:
            local_binding = session._local_binding_at(real_path, SourcePosition(line, character))
            target = session.symbol_at(real_path, SourcePosition(line, character))
        except FileNotFoundError:
            return None
        if (
            local_binding is not None
            and local_binding.kind == "from_import_alias"
            and local_binding.import_source is not None
            and local_binding.name != local_binding.import_source.rpartition(":")[2]
        ):
            raise _RequestFailed(
                f"Cannot rename {identifier!r} via an `import ... as` alias; "
                f"rename the original symbol instead."
            )
        if target is None:
            return None
        if identifier != target.name:
            raise _RequestFailed(
                f"Cannot rename {identifier!r} via an `import ... as` alias; "
                f"rename the original symbol instead."
            )
        result = session.rename_symbol(target, new_name)

        if result.status == "invalid_identifier":
            raise _RequestFailed(f"{new_name!r} is not a valid Python identifier.")
        if result.status == "keyword_identifier":
            raise _RequestFailed(f"{new_name!r} is a Python keyword.")
        if result.status == "same_name":
            return None
        if result.status != "ok":
            return None

        changes: dict[str, list[dict[str, Any]]] = {}
        for edit in result.edits:
            uri = _path_to_uri(edit.path)
            changes.setdefault(uri, []).append(
                {
                    "range": _range_to_lsp(edit.range),
                    "newText": edit.new_text,
                }
            )
        return {"changes": changes}

    def _code_action(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        context = params.get("context") or {}
        only = context.get("only")
        if only is not None and not any(
            kind == "quickfix" or "quickfix".startswith(f"{kind}.") for kind in only
        ):
            return []
        range_ = params.get("range") or {}
        start = range_.get("start") or {}
        end = range_.get("end") or {}
        start_line = int(start.get("line", 0))
        start_character = int(start.get("character", 0))
        end_line = int(end.get("line", start_line))
        end_character = int(end.get("character", start_character))
        try:
            actions = session.code_actions_for_range(
                real_path, start_line, start_character, end_line, end_character
            )
        except FileNotFoundError:
            return []
        payload: list[dict[str, Any]] = []
        for action in actions:
            changes: dict[str, list[dict[str, Any]]] = {}
            for edit in action.edits:
                uri = _path_to_uri(edit.path)
                changes.setdefault(uri, []).append(
                    {
                        "range": _range_to_lsp(edit.range),
                        "newText": edit.new_text,
                    }
                )
            payload.append(
                {
                    "title": action.title,
                    "kind": action.kind,
                    "diagnostics": [self._analysis_diagnostic_to_lsp(action.diagnostic)],
                    "edit": {"changes": changes},
                }
            )
        return payload

    def _signature_help(self, params: Any) -> dict[str, Any] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        try:
            signature_help = session.signature_help_at(real_path, line, character)
        except FileNotFoundError:
            return None
        if signature_help is None:
            return None
        signature_info: dict[str, Any] = {
            "label": signature_help.label,
            "parameters": [
                {
                    "label": [
                        parameter.label_offset_start,
                        parameter.label_offset_end,
                    ],
                }
                for parameter in signature_help.parameters
            ],
        }
        result: dict[str, Any] = {
            "signatures": [signature_info],
            "activeSignature": 0,
        }
        if signature_help.active_parameter is not None:
            result["activeParameter"] = signature_help.active_parameter
        return result

    def _folding_range(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        try:
            ranges = session.folding_ranges_for_file(real_path)
        except FileNotFoundError:
            return []
        payload: list[dict[str, Any]] = []
        for fold in ranges:
            entry: dict[str, Any] = {
                "startLine": fold.range.start.line,
                "startCharacter": fold.range.start.character,
                "endLine": fold.range.end.line,
                "endCharacter": fold.range.end.character,
            }
            if fold.kind != "region":
                entry["kind"] = _FOLDING_RANGE_KINDS[fold.kind]
            payload.append(entry)
        return payload

    def _selection_range(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        positions = params.get("positions") or []
        results: list[dict[str, Any]] = []
        for position in positions:
            line = int(position["line"])
            character = int(position["character"])
            try:
                chain = session.selection_ranges_at(real_path, line, character)
            except FileNotFoundError:
                chain = ()
            if not chain:
                results.append(
                    {
                        "range": {
                            "start": {"line": line, "character": character},
                            "end": {"line": line, "character": character},
                        }
                    }
                )
                continue
            payload: dict[str, Any] | None = None
            for entry in reversed(chain):
                node: dict[str, Any] = {"range": _range_to_lsp(entry.range)}
                if payload is not None:
                    node["parent"] = payload
                payload = node
            assert payload is not None
            results.append(payload)
        return results

    def _document_link(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        try:
            links = session.document_links_for_file(real_path)
        except FileNotFoundError:
            return []
        return [
            {
                "range": _range_to_lsp(link.range),
                "target": _path_to_uri(link.target_path),
            }
            for link in links
        ]

    def _code_lens(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        try:
            lenses = session.code_lenses_for_file(real_path)
        except FileNotFoundError:
            return []
        return [
            {
                "range": _range_to_lsp(lens.range),
                "command": {"title": lens.title, "command": ""},
            }
            for lens in lenses
        ]

    def _prepare_call_hierarchy(self, params: Any) -> list[dict[str, Any]] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        try:
            items = session.prepare_call_hierarchy(real_path, line, character)
        except FileNotFoundError:
            return None
        if not items:
            return None
        return [_call_hierarchy_item_to_lsp(item) for item in items]

    def _call_hierarchy_incoming_calls(self, params: Any) -> list[dict[str, Any]] | None:
        ident = _call_hierarchy_identity_from_item(params.get("item"))
        if ident is None:
            return None
        path, qualified_name = ident
        try:
            real_path = self._require_safe_path(_path_to_uri(path))
        except (ValueError, RuntimeError):
            return None
        try:
            results = self._require_session().call_hierarchy_incoming_calls(
                real_path, qualified_name
            )
        except FileNotFoundError:
            return None
        return [
            {
                "from": _call_hierarchy_item_to_lsp(call.caller),
                "fromRanges": [_call_site_to_lsp_range(site) for site in call.call_sites],
            }
            for call in results
        ]

    def _call_hierarchy_outgoing_calls(self, params: Any) -> list[dict[str, Any]] | None:
        ident = _call_hierarchy_identity_from_item(params.get("item"))
        if ident is None:
            return None
        path, qualified_name = ident
        try:
            real_path = self._require_safe_path(_path_to_uri(path))
        except (ValueError, RuntimeError):
            return None
        try:
            results = self._require_session().call_hierarchy_outgoing_calls(
                real_path, qualified_name
            )
        except FileNotFoundError:
            return None
        return [
            {
                "to": _call_hierarchy_item_to_lsp(call.callee),
                "fromRanges": [_call_site_to_lsp_range(site) for site in call.call_sites],
            }
            for call in results
        ]

    def _prepare_type_hierarchy(self, params: Any) -> list[dict[str, Any]] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        try:
            items = session.prepare_type_hierarchy(real_path, line, character)
        except FileNotFoundError:
            return None
        if not items:
            return None
        return [_type_hierarchy_item_to_lsp(item) for item in items]

    def _type_hierarchy_supertypes(self, params: Any) -> list[dict[str, Any]] | None:
        ident = _type_hierarchy_identity_from_item(params.get("item"))
        if ident is None:
            return None
        path, qualified_name = ident
        try:
            real_path = self._require_safe_path(_path_to_uri(path))
        except (ValueError, RuntimeError):
            return None
        try:
            results = self._require_session().type_hierarchy_supertypes(real_path, qualified_name)
        except FileNotFoundError:
            return None
        return [_type_hierarchy_item_to_lsp(item) for item in results]

    def _type_hierarchy_subtypes(self, params: Any) -> list[dict[str, Any]] | None:
        ident = _type_hierarchy_identity_from_item(params.get("item"))
        if ident is None:
            return None
        path, qualified_name = ident
        try:
            real_path = self._require_safe_path(_path_to_uri(path))
        except (ValueError, RuntimeError):
            return None
        try:
            results = self._require_session().type_hierarchy_subtypes(real_path, qualified_name)
        except FileNotFoundError:
            return None
        return [_type_hierarchy_item_to_lsp(item) for item in results]

    def _inlay_hint(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        lsp_range = params.get("range") or {}
        start = lsp_range.get("start") or {}
        end = lsp_range.get("end") or {}
        start_line = int(start.get("line", 0))
        start_character = int(start.get("character", 0))
        end_line_raw = end.get("line")
        end_line = int(end_line_raw) if end_line_raw is not None else None
        end_character = int(end.get("character", 0))
        try:
            hints = session.inlay_hints_for_file(
                real_path,
                start_line=start_line,
                start_character=start_character,
                end_line=end_line,
                end_character=end_character,
            )
        except FileNotFoundError:
            return []
        return [
            {
                "position": _position_to_lsp(hint.position),
                "label": hint.label,
                "kind": _INLAY_HINT_KIND_TO_LSP[hint.kind],
                "paddingLeft": hint.padding_left,
                "paddingRight": hint.padding_right,
            }
            for hint in hints
        ]

    def _semantic_tokens_full(self, params: Any) -> dict[str, Any]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        try:
            tokens = session.semantic_tokens_for_file(real_path)
        except FileNotFoundError:
            return {"data": []}
        source = session.source_text(real_path) or ""
        return {"data": _encode_semantic_tokens(tokens, source, self._position_encoding)}

    def _semantic_tokens_range(self, params: Any) -> dict[str, Any]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        lsp_range = params.get("range") or {}
        start = lsp_range.get("start") or {}
        end = lsp_range.get("end") or {}
        start_line = int(start.get("line", 0))
        start_character = int(start.get("character", 0))
        end_line_raw = end.get("line")
        end_line = int(end_line_raw) if end_line_raw is not None else None
        end_character = int(end.get("character", 0))
        try:
            tokens = session.semantic_tokens_range_for_file(
                real_path,
                start_line=start_line,
                start_character=start_character,
                end_line=end_line,
                end_character=end_character,
            )
        except FileNotFoundError:
            return {"data": []}
        source = session.source_text(real_path) or ""
        return {"data": _encode_semantic_tokens(tokens, source, self._position_encoding)}

    def _will_rename_files(self, params: Any) -> dict[str, Any] | None:
        files = params.get("files", []) if isinstance(params, dict) else []
        session = self._require_session()
        renames: list[tuple[str, str]] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            old_uri = entry.get("oldUri")
            new_uri = entry.get("newUri")
            if not isinstance(old_uri, str) or not isinstance(new_uri, str):
                continue
            try:
                old_path = _uri_to_path(old_uri)
                new_path = _uri_to_path(new_uri)
            except ValueError:
                continue
            renames.append((old_path, new_path))
        edits = session.import_edits_for_file_renames(renames)
        if not edits:
            return None
        changes: dict[str, list[dict[str, Any]]] = {}
        for edit in edits:
            uri = _path_to_uri(edit.path)
            changes.setdefault(uri, []).append(
                {
                    "range": _range_to_lsp(edit.range),
                    "newText": edit.new_text,
                }
            )
        return {"changes": changes}

    def _will_delete_files(self, params: Any) -> dict[str, Any] | None:
        files = params.get("files", []) if isinstance(params, dict) else []
        session = self._require_session()
        deletions: list[str] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri")
            if not isinstance(uri, str):
                continue
            try:
                deletions.append(_uri_to_path(uri))
            except ValueError:
                continue
        edits = session.import_edits_for_file_deletions(deletions)
        if not edits:
            return None
        changes: dict[str, list[dict[str, Any]]] = {}
        for edit in edits:
            uri = _path_to_uri(edit.path)
            changes.setdefault(uri, []).append(
                {
                    "range": _range_to_lsp(edit.range),
                    "newText": edit.new_text,
                }
            )
        return {"changes": changes}

    def _workspace_root_from_params(self, params: Any) -> str:
        if isinstance(params, dict):
            root_uri = params.get("rootUri")
            if isinstance(root_uri, str):
                return _uri_to_path(root_uri)
            workspace_folders = params.get("workspaceFolders")
            if isinstance(workspace_folders, list) and workspace_folders:
                first = workspace_folders[0]
                if isinstance(first, dict) and isinstance(first.get("uri"), str):
                    return _uri_to_path(first["uri"])
            root_path = params.get("rootPath")
            if isinstance(root_path, str):
                return str(Path(root_path).resolve(strict=False))
        if self._default_root is not None:
            return str(Path(self._default_root).resolve(strict=False))
        return str(Path.cwd().resolve(strict=False))

    def _analysis_diagnostic_to_lsp(self, diagnostic: AnalysisDiagnostic) -> dict[str, Any]:
        # A diagnostic without a range points at the start of its file. The
        # range is empty, so it exists whatever the first line holds.
        source_range = diagnostic.range or SourceRange(SourcePosition(0, 0), SourcePosition(0, 0))
        payload: dict[str, Any] = {
            "range": _range_to_lsp(source_range),
            "severity": _PYINC_SEVERITY_TO_LSP[diagnostic.severity],
            "source": diagnostic.source,
            "code": diagnostic.code,
            "message": diagnostic.message,
        }
        tags = [
            _PYINC_DIAGNOSTIC_TAG_TO_LSP[tag]
            for tag in diagnostic.tags
            if tag in _PYINC_DIAGNOSTIC_TAG_TO_LSP
        ]
        if tags:
            payload["tags"] = tags
        return payload

    def _require_safe_path(self, uri: str) -> str:
        try:
            path = _uri_to_path(uri)
        except InvalidParams as exc:
            raise _OutsideWorkspace(str(exc)) from exc
        root = Path(self._require_session().root).resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise _OutsideWorkspace(str(exc)) from exc
        return str(resolved)

    def _require_session(self) -> WorkspaceSession:
        session = self._session
        if session is None:
            raise RuntimeError("LSP session has not been initialized.")
        return session

    def _default_uri(self, params: Any) -> str | None:
        if not isinstance(params, dict):
            return None
        document = params.get("textDocument")
        if isinstance(document, dict) and isinstance(document.get("uri"), str):
            return cast(str, document["uri"])
        item = params.get("item")
        if isinstance(item, dict) and isinstance(item.get("uri"), str):
            return cast(str, item["uri"])
        return None

    def _source_for_uri(self, uri: str) -> str | None:
        session = self._session
        if session is None:
            return None
        try:
            path = _uri_to_path(uri)
            return session.source_text(path)
        except (FileNotFoundError, ValueError):
            return None

    def _read_message(self) -> dict[str, Any] | None:
        return read_message(self._input)

    def _send(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            write_message(self._output, payload)

    def _send_error(self, request_id: Any, code: int, message: str) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _send_notification(
        self,
        method: str,
        params: dict[str, Any],
        *,
        session: WorkspaceSession | None = None,
    ) -> bool:
        """Send a notification and return whether it was written.

        Positions are converted before the write lock is taken. The conversion
        reads document text under the session's lock, which comes before the
        write lock in the lock order. Given a session, the notification is
        written only while that session is still the server's and shutdown has
        not been requested. The check and the write share one hold of the
        write lock, so teardown and shutdown land either before both or after
        both.
        """
        converted = convert_payload_positions(
            params,
            encoding=self._position_encoding,
            to_client=True,
            source_for_uri=self._source_for_uri,
            uri=params.get("uri") if isinstance(params.get("uri"), str) else None,
        )
        with self._write_lock:
            if session is not None and (self._session is not session or self._shutdown_requested):
                return False
            self._send({"jsonrpc": "2.0", "method": method, "params": converted})
        return True
