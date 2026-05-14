from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from pyinc.integrations import Symbol

from .session import (
    AnalysisDiagnostic,
    CallHierarchyCallSite,
    CallHierarchyItem,
    PollingWorkspaceWatcher,
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

_ID_START_RE = re.compile(r"[A-Za-z_]")
_ID_CONT_RE = re.compile(r"[A-Za-z0-9_]")

_LSP_REQUEST_FAILED = -32803


class _RequestFailed(Exception):
    """Raised by request handlers to surface a user-facing rejection.

    Mapped to the LSP `RequestFailed` (-32803) JSON-RPC error code.
    """


def _path_to_uri(path: str) -> str:
    return Path(path).resolve(strict=False).as_uri()


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"Unsupported URI scheme: {parsed.scheme!r}")
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
    )


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
    while start > 0 and _ID_CONT_RE.match(text[start - 1]):
        start -= 1
    end = character
    while end < len(text) and _ID_CONT_RE.match(text[end]):
        end += 1
    if start == end or not _ID_START_RE.match(text[start]):
        return None
    return text[start:end], start, end


def _identifier_at_position(source: str, line: int, character: int) -> str | None:
    span = _identifier_span_at_position(source, line, character)
    return span[0] if span is not None else None


def _find_symbol_by_identifier(
    symbols: tuple[Symbol, ...], identifier: str
) -> Symbol | None:
    for symbol in symbols:
        if symbol.qualified_name == identifier:
            return symbol
    for symbol in symbols:
        if symbol.qualified_name.rsplit(".", 1)[-1] == identifier:
            return symbol
    return None


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


def _call_hierarchy_item_to_lsp(item: CallHierarchyItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": item.name,
        "kind": _CALL_HIERARCHY_KIND_TO_LSP[item.kind],
        "uri": _path_to_uri(item.path),
        "range": {
            "start": {
                "line": item.range_start_line,
                "character": item.range_start_character,
            },
            "end": {
                "line": item.range_end_line,
                "character": item.range_end_character,
            },
        },
        "selectionRange": {
            "start": {
                "line": item.selection_start_line,
                "character": item.selection_start_character,
            },
            "end": {
                "line": item.selection_end_line,
                "character": item.selection_end_character,
            },
        },
        "data": {"path": item.path, "qualified_name": item.qualified_name},
    }
    if item.detail is not None:
        payload["detail"] = item.detail
    return payload


def _call_site_to_lsp_range(site: CallHierarchyCallSite) -> dict[str, Any]:
    return {
        "start": {"line": site.start_line, "character": site.start_character},
        "end": {"line": site.end_line, "character": site.end_character},
    }


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
        self._shutdown_requested = False
        self._published_paths: set[str] = set()
        self._published_signatures: dict[str, tuple[tuple[Any, ...], ...]] = {}

    def serve(self) -> int:
        try:
            while True:
                message = self._read_message()
                if message is None:
                    return 0
                if not self._handle_message(message):
                    return 0
        finally:
            self._teardown_session()

    def _handle_message(self, message: dict[str, Any]) -> bool:
        if "method" not in message:
            return True

        method = str(message["method"])
        params = message.get("params", {})

        if "id" in message:
            request_id = message["id"]
            try:
                result = self._handle_request(method, params)
            except _RequestFailed as exc:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": _LSP_REQUEST_FAILED,
                            "message": str(exc),
                        },
                    }
                )
            except Exception:  # pragma: no cover - defensive JSON-RPC boundary
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": "Internal error"},
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
            self._stop_watcher()
            return None
        if method == "textDocument/documentSymbol":
            return self._document_symbols(params)
        if method == "workspace/symbol":
            return self._workspace_symbols(params)
        if method == "textDocument/hover":
            return self._hover(params)
        if method == "textDocument/definition":
            return self._definition(params)
        if method == "textDocument/typeDefinition":
            return self._type_definition(params)
        if method == "textDocument/references":
            return self._references(params)
        if method == "textDocument/documentHighlight":
            return self._document_highlight(params)
        if method == "textDocument/prepareRename":
            return self._prepare_rename(params)
        if method == "textDocument/rename":
            return self._rename(params)
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
        raise ValueError(f"Unsupported LSP request: {method}")

    def _handle_notification(self, method: str, params: Any) -> bool:
        if method == "exit":
            self._stop_watcher()
            return False
        if method == "initialized":
            self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didOpen":
            document = params["textDocument"]
            self._require_session().set_overlay(
                self._require_safe_path(document["uri"]), document["text"]
            )
            self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didChange":
            document = params["textDocument"]
            changes = params.get("contentChanges", [])
            if changes:
                latest = changes[-1]
                if "text" in latest:
                    self._require_session().set_overlay(
                        self._require_safe_path(document["uri"]), latest["text"]
                    )
                    self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didSave":
            document = params["textDocument"]
            self._require_session().clear_overlay(self._require_safe_path(document["uri"]))
            self.publish_workspace_diagnostics()
            return True
        if method == "textDocument/didClose":
            document = params["textDocument"]
            self._require_session().clear_overlay(self._require_safe_path(document["uri"]))
            self.publish_workspace_diagnostics()
            return True
        if method == "workspace/didChangeWatchedFiles":
            changes = params.get("changes", [])
            paths = [self._require_safe_path(item["uri"]) for item in changes if "uri" in item]
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
            diagnostics = grouped.get(path, [])
            signature = tuple(_diagnostic_signature(item) for item in diagnostics)
            if self._published_signatures.get(path) == signature:
                continue
            self._published_signatures[path] = signature
            self._send_notification(
                "textDocument/publishDiagnostics",
                {
                    "uri": _path_to_uri(path),
                    "diagnostics": diagnostics,
                },
            )
        for stale_path in self._published_paths - current_paths:
            # Clear the cached signature so a future reappearance republishes.
            self._published_signatures.pop(stale_path, None)
        self._published_paths = current_paths

    def _initialize(self, params: Any) -> dict[str, Any]:
        root = self._workspace_root_from_params(params)
        self._teardown_session()
        self._session = WorkspaceSession(root)
        self._published_paths.clear()
        self._published_signatures.clear()

        options = {}
        if isinstance(params, dict):
            init_options = params.get("initializationOptions")
            if isinstance(init_options, dict):
                options = init_options

        watcher_enabled = bool(options.get("pyinc.watcher.enabled", True))
        if watcher_enabled:
            debounce_ms = int(options.get("pyinc.watcher.debounceMs", 200))
            interval_ms = options.get("pyinc.watcher.intervalMs")
            interval_s: float | None
            if isinstance(interval_ms, (int, float)):
                interval_s = float(interval_ms) / 1000.0
            else:
                interval_s = None
            self._watcher = PollingWorkspaceWatcher(
                self._session, debounce_ms=debounce_ms
            )
            self._watcher.start(self._on_watcher_change, interval_s=interval_s)

        return {
            "capabilities": {
                "textDocumentSync": {
                    "openClose": True,
                    "change": 1,
                    "save": {"includeText": True},
                },
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "hoverProvider": True,
                "definitionProvider": True,
                "typeDefinitionProvider": True,
                "referencesProvider": True,
                "documentHighlightProvider": True,
                "renameProvider": {"prepareProvider": True},
                "signatureHelpProvider": {
                    "triggerCharacters": ["(", ","],
                    "retriggerCharacters": [","],
                },
                "foldingRangeProvider": True,
                "selectionRangeProvider": True,
                "documentLinkProvider": {"resolveProvider": False},
                "codeLensProvider": {"resolveProvider": False},
                "callHierarchyProvider": True,
            },
            "serverInfo": {"name": "pyinc-tools", "version": "2.0.0"},
        }

    def _on_watcher_change(self, _changed: tuple[str, ...]) -> None:
        try:
            self.publish_workspace_diagnostics()
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"pyinc-tools lsp: publishDiagnostics raised: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def _stop_watcher(self) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        self._watcher = None
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            watcher.stop()

    def _teardown_session(self) -> None:
        self._stop_watcher()
        if self._session is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                self._session.close()
            self._session = None

    def _document_symbols(self, params: Any) -> list[dict[str, Any]]:
        document = params["textDocument"]
        result = self._require_session().analyze_file(self._require_safe_path(document["uri"]))
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
        result = self._require_session().analyze_workspace()
        module_to_path = {
            module.module: module.path for module in result.python.modules
        }
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
                    "kind": _PYINC_SYMBOL_KIND_TO_LSP.get(
                        entry.kind, _LSP_SYMBOL_KINDS["variable"]
                    ),
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

    def _hover(self, params: Any) -> dict[str, Any] | None:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        source = session.source_text(real_path)
        if source is None:
            return None
        identifier = _identifier_at_position(source, line, character)
        if identifier is None:
            return None
        analysis = session.analyze_file(real_path)
        if analysis.symbols is None:
            return None
        symbol = _find_symbol_by_identifier(analysis.symbols.symbols, identifier)
        if symbol is None:
            return None
        return {
            "contents": {"kind": "markdown", "value": _format_hover_markdown(symbol)}
        }

    def _definition(self, params: Any) -> list[dict[str, Any]]:
        session = self._require_session()
        real_path = self._require_safe_path(params["textDocument"]["uri"])
        position = params["position"]
        line = int(position["line"])
        character = int(position["character"])
        source = session.source_text(real_path)
        if source is None:
            return []
        identifier = _identifier_at_position(source, line, character)
        if identifier is None:
            return []
        try:
            resolved = session.resolve_symbol_reference(real_path, identifier)
        except FileNotFoundError:
            return []
        if resolved.defining_path is None or resolved.defining_lineno is None:
            return []
        line_zero = max(resolved.defining_lineno - 1, 0)
        return [
            {
                "uri": _path_to_uri(resolved.defining_path),
                "range": {
                    "start": {"line": line_zero, "character": 0},
                    "end": {"line": line_zero, "character": 1},
                },
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
        identifier = _identifier_at_position(source, line, character)
        if identifier is None:
            return []
        try:
            locations = session.type_definitions_at(real_path, identifier)
        except FileNotFoundError:
            return []
        return [
            {
                "uri": _path_to_uri(location.path),
                "range": {
                    "start": {
                        "line": max(location.lineno - 1, 0),
                        "character": location.col_offset,
                    },
                    "end": {
                        "line": max(location.lineno - 1, 0),
                        "character": location.end_col_offset,
                    },
                },
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
        identifier = _identifier_at_position(source, line, character)
        if identifier is None:
            return []
        try:
            result = session.find_references(
                real_path, identifier, include_declaration=include_declaration
            )
        except FileNotFoundError:
            return []
        if result.target.resolution != "workspace":
            return []
        locations: list[dict[str, Any]] = []
        for reference in result.references:
            ref_line = max(reference.lineno - 1, 0)
            locations.append(
                {
                    "uri": _path_to_uri(reference.path),
                    "range": {
                        "start": {"line": ref_line, "character": reference.col_offset},
                        "end": {
                            "line": ref_line,
                            "character": reference.end_col_offset,
                        },
                    },
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
        identifier = _identifier_at_position(source, line, character)
        if identifier is None:
            return []
        try:
            highlights = session.find_document_highlights(real_path, identifier)
        except FileNotFoundError:
            return []
        return [
            {
                "range": {
                    "start": {
                        "line": max(highlight.lineno - 1, 0),
                        "character": highlight.col_offset,
                    },
                    "end": {
                        "line": max(highlight.lineno - 1, 0),
                        "character": highlight.end_col_offset,
                    },
                },
                "kind": _DOCUMENT_HIGHLIGHT_KINDS[highlight.kind],
            }
            for highlight in highlights
        ]

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
            resolved = session.resolve_symbol_reference(real_path, identifier)
        except FileNotFoundError:
            return None
        if resolved.resolution != "workspace":
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
            result = session.rename_symbol(real_path, identifier, new_name)
        except FileNotFoundError:
            return None

        if result.status == "invalid_identifier":
            raise _RequestFailed(
                f"{new_name!r} is not a valid Python identifier."
            )
        if result.status == "keyword_identifier":
            raise _RequestFailed(f"{new_name!r} is a Python keyword.")
        if result.status == "alias_rename_unsupported":
            raise _RequestFailed(
                f"Cannot rename {identifier!r} via an `import ... as` alias; "
                f"rename the original symbol instead."
            )
        if result.status == "same_name":
            return None
        if result.status != "ok":
            return None

        changes: dict[str, list[dict[str, Any]]] = {}
        for edit in result.edits:
            uri = _path_to_uri(edit.path)
            edit_line = max(edit.lineno - 1, 0)
            changes.setdefault(uri, []).append(
                {
                    "range": {
                        "start": {
                            "line": edit_line,
                            "character": edit.col_offset,
                        },
                        "end": {
                            "line": edit_line,
                            "character": edit.end_col_offset,
                        },
                    },
                    "newText": edit.new_text,
                }
            )
        return {"changes": changes}

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
                "startLine": max(fold.start_line - 1, 0),
                "endLine": max(fold.end_line - 1, 0),
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
                node: dict[str, Any] = {
                    "range": {
                        "start": {
                            "line": entry.start_line,
                            "character": entry.start_character,
                        },
                        "end": {
                            "line": entry.end_line,
                            "character": entry.end_character,
                        },
                    }
                }
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
                "range": {
                    "start": {
                        "line": link.start_line,
                        "character": link.start_character,
                    },
                    "end": {
                        "line": link.end_line,
                        "character": link.end_character,
                    },
                },
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
                "range": {
                    "start": {
                        "line": lens.start_line,
                        "character": lens.start_character,
                    },
                    "end": {
                        "line": lens.end_line,
                        "character": lens.end_character,
                    },
                },
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

    def _call_hierarchy_incoming_calls(
        self, params: Any
    ) -> list[dict[str, Any]] | None:
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
                "fromRanges": [
                    _call_site_to_lsp_range(site) for site in call.call_sites
                ],
            }
            for call in results
        ]

    def _call_hierarchy_outgoing_calls(
        self, params: Any
    ) -> list[dict[str, Any]] | None:
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
                "fromRanges": [
                    _call_site_to_lsp_range(site) for site in call.call_sites
                ],
            }
            for call in results
        ]

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

    def _analysis_diagnostic_to_lsp(
        self, diagnostic: AnalysisDiagnostic
    ) -> dict[str, Any]:
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

    def _require_safe_path(self, uri: str) -> str:
        path = _uri_to_path(uri)
        root = Path(self._require_session().root).resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        resolved.relative_to(root)  # raises ValueError if path escapes workspace
        return str(resolved)

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
