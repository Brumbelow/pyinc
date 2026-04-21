from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from .session import AnalysisDiagnostic, WorkspaceSession

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


def _path_to_uri(path: str) -> str:
    return Path(path).resolve(strict=False).as_uri()


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"Unsupported URI scheme: {parsed.scheme!r}")
    return str(Path(url2pathname(parsed.path)).resolve(strict=False))


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
        self._shutdown_requested = False
        self._published_paths: set[str] = set()

    def serve(self) -> int:
        while True:
            message = self._read_message()
            if message is None:
                return 0
            if not self._handle_message(message):
                return 0

    def _handle_message(self, message: dict[str, Any]) -> bool:
        if "method" not in message:
            return True

        method = str(message["method"])
        params = message.get("params", {})

        if "id" in message:
            request_id = message["id"]
            try:
                result = self._handle_request(method, params)
            except Exception as exc:  # pragma: no cover - defensive JSON-RPC boundary
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": str(exc)},
                    }
                )
            else:
                self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
            return True

        return self._handle_notification(method, params)

    def _handle_request(self, method: str, params: Any) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "shutdown":
            self._shutdown_requested = True
            return None
        if method == "textDocument/documentSymbol":
            return self._document_symbols(params)
        if method == "workspace/symbol":
            return self._workspace_symbols(params)
        raise ValueError(f"Unsupported LSP request: {method}")

    def _handle_notification(self, method: str, params: Any) -> bool:
        if method == "exit":
            return False
        if method == "initialized":
            self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didOpen":
            document = params["textDocument"]
            self._require_session().set_overlay(_uri_to_path(document["uri"]), document["text"])
            self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didChange":
            document = params["textDocument"]
            changes = params.get("contentChanges", [])
            if changes:
                latest = changes[-1]
                if "text" in latest:
                    self._require_session().set_overlay(_uri_to_path(document["uri"]), latest["text"])
                    self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didSave":
            document = params["textDocument"]
            self._require_session().clear_overlay(_uri_to_path(document["uri"]))
            self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didClose":
            document = params["textDocument"]
            self._require_session().clear_overlay(_uri_to_path(document["uri"]))
            self.publish_workspace_diagnostics()
            return True
        if method == "workspace/didChangeWatchedFiles":
            changes = params.get("changes", [])
            paths = [_uri_to_path(item["uri"]) for item in changes if "uri" in item]
            if paths:
                self._require_session().refresh_paths(paths)
                self.publish_workspace_diagnostics()
            return True
        return True

    def publish_workspace_diagnostics(self) -> None:
        if self._session is None:
            return
        result = self._session.analyze_workspace()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for diagnostic in result.diagnostics:
            grouped.setdefault(diagnostic.path, []).append(
                self._analysis_diagnostic_to_lsp(diagnostic)
            )

        current_paths = set(grouped)
        for path in sorted(current_paths | self._published_paths):
            self._send_notification(
                "textDocument/publishDiagnostics",
                {
                    "uri": _path_to_uri(path),
                    "diagnostics": grouped.get(path, []),
                },
            )
        self._published_paths = current_paths

    def _initialize(self, params: Any) -> dict[str, Any]:
        root = self._workspace_root_from_params(params)
        if self._session is not None:
            self._session.close()
        self._session = WorkspaceSession(root)
        self._published_paths.clear()
        return {
            "capabilities": {
                "textDocumentSync": {
                    "openClose": True,
                    "change": 1,
                    "save": {"includeText": True},
                },
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
            },
            "serverInfo": {"name": "pyinc-tools", "version": "1.0.1"},
        }

    def _document_symbols(self, params: Any) -> list[dict[str, Any]]:
        document = params["textDocument"]
        result = self._require_session().analyze_file(_uri_to_path(document["uri"]))
        if result.symbols is None:
            return []
        symbols: list[dict[str, Any]] = []
        for symbol in result.symbols.symbols:
            line = max(symbol.lineno - 1, 0)
            range_payload = {
                "start": {"line": line, "character": 0},
                "end": {"line": line, "character": 1},
            }
            symbols.append(
                {
                    "name": symbol.qualified_name,
                    "kind": _PYINC_SYMBOL_KIND_TO_LSP.get(symbol.kind, _LSP_SYMBOL_KINDS["variable"]),
                    "range": range_payload,
                    "selectionRange": range_payload,
                }
            )
        return symbols

    def _workspace_symbols(self, params: Any) -> list[dict[str, Any]]:
        query = str(params.get("query", "")).lower()
        result = self._require_session().analyze_workspace()
        module_to_path = {module.module: module.path for module in result.python.modules}
        matches: list[dict[str, Any]] = []
        for entry in result.symbols.entries:
            if query and query not in entry.qualified_name.lower():
                continue
            path = module_to_path.get(entry.module)
            if path is None:
                continue
            line = max(entry.lineno - 1, 0)
            matches.append(
                {
                    "name": entry.qualified_name,
                    "kind": _PYINC_SYMBOL_KIND_TO_LSP.get(entry.kind, _LSP_SYMBOL_KINDS["variable"]),
                    "location": {
                        "uri": _path_to_uri(path),
                        "range": {
                            "start": {"line": line, "character": 0},
                            "end": {"line": line, "character": 1},
                        },
                    },
                    "containerName": entry.module,
                }
            )
        return matches

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
        line = max((diagnostic.lineno or 1) - 1, 0)
        character = max(diagnostic.col_offset or 0, 0)
        return {
            "range": {
                "start": {"line": line, "character": character},
                "end": {"line": line, "character": character + 1},
            },
            "severity": _PYINC_SEVERITY_TO_LSP[diagnostic.severity],
            "source": diagnostic.source,
            "code": diagnostic.code,
            "message": diagnostic.message,
        }

    def _require_session(self) -> WorkspaceSession:
        if self._session is None:
            raise RuntimeError("LSP session has not been initialized.")
        return self._session

    def _read_message(self) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = self._input.readline()
            if not line:
                return None
            decoded = line.decode("utf-8").strip()
            if not decoded:
                break
            key, _, value = decoded.partition(":")
            headers[key.lower()] = value.strip()

        content_length = headers.get("content-length")
        if content_length is None:
            raise ValueError("Missing Content-Length header.")
        body = self._input.read(int(content_length))
        return cast(dict[str, Any], json.loads(body.decode("utf-8")))

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._output.write(header)
        self._output.write(body)
        self._output.flush()

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})
