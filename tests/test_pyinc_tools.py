from __future__ import annotations

import contextlib
import io
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

import pyinc_tools.cli as cli
from pyinc.integrations import SourcePosition, SourceRange, SymbolId
from pyinc_tools._jsonrpc import ParseError, read_message
from pyinc_tools.lsp import LanguageServer, _package_version, _RequestFailed
from pyinc_tools.session import (
    AnalysisDiagnostic,
    CallHierarchyCallSite,
    CallHierarchyIncomingCall,
    CallHierarchyItem,
    CallHierarchyOutgoingCall,
    CodeAction,
    CodeActionEdit,
    CodeLens,
    CompletionItem,
    DeclarationLocation,
    DocumentLink,
    FileDeletionEdit,
    FileRenameEdit,
    FoldingRange,
    InlayHint,
    PollingWorkspaceWatcher,
    RenameEdit,
    SelectionRange,
    SemanticToken,
    TypeDefinitionLocation,
    TypeHierarchyItem,
    WorkspaceSession,
)

_LSP_SYMBOL_KIND_FUNCTION = 12
_LSP_SYMBOL_KIND_METHOD = 6
_LSP_SYMBOL_KIND_CLASS = 5
_LSP_SYMBOL_KIND_FIELD = 8
_LSP_SYMBOL_KIND_VARIABLE = 13


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _range(
    start_line: int,
    start_character: int,
    end_line: int,
    end_character: int,
) -> SourceRange:
    return SourceRange(
        SourcePosition(start_line, start_character),
        SourcePosition(end_line, end_character),
    )


def _fold_signatures(
    ranges: tuple[FoldingRange, ...],
) -> set[tuple[int, int, str]]:
    return {(item.range.start.line + 1, item.range.end.line + 1, item.kind) for item in ranges}


def _symbol_for_name(
    session: WorkspaceSession,
    path: Path,
    name: str,
) -> SymbolId:
    source = session.source_text(path)
    assert source is not None
    offset = source.index(name)
    prefix = source[:offset]
    line = prefix.count("\n")
    character = len(prefix.rsplit("\n", 1)[-1])
    symbol_id = session.symbol_at(path, SourcePosition(line, character))
    assert symbol_id is not None
    return symbol_id


class _WatcherFactory:
    def __init__(self) -> None:
        self.built: list[PollingWorkspaceWatcher] = []

    def __call__(
        self, session: WorkspaceSession, *, debounce_ms: int = 30
    ) -> PollingWorkspaceWatcher:
        watcher = PollingWorkspaceWatcher(session, debounce_ms=debounce_ms)
        self.built.append(watcher)
        return watcher


@pytest.fixture()
def watcher_factory() -> Iterator[_WatcherFactory]:
    factory = _WatcherFactory()
    try:
        yield factory
    finally:
        for watcher in factory.built:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort teardown
                watcher.stop(timeout=2.0)


def test_workspace_session_overlay_edits_do_not_touch_disk(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "def ok() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        clean = session.analyze_file(target)
        assert not clean.diagnostics

        session.set_overlay(target, "def broken(\n")
        edited = session.analyze_file(target)

        assert any(diagnostic.code == "syntax-error" for diagnostic in edited.diagnostics)
        assert target.read_text(encoding="utf-8") == "def ok() -> int:\n    return 1\n"


def test_workspace_session_save_and_close_reconcile_with_disk(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "def original() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        session.set_overlay(target, "def renamed() -> int:\n    return 2\n")
        assert session.analyze_file(target).module is not None
        assert session.analyze_file(target).module.definitions[0].name == "renamed"  # type: ignore[union-attr]

        _write(target, "def saved() -> int:\n    return 3\n")
        session.clear_overlay(target)
        saved = session.analyze_file(target)

        assert saved.module is not None
        assert saved.module.definitions[0].name == "saved"
        assert not any(diagnostic.code == "syntax-error" for diagnostic in saved.diagnostics)


def test_workspace_session_cross_file_invalidation_and_path_remap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def foo() -> int:\n    return 1\n")
    _write(consumer, "from a import foo\n")

    with WorkspaceSession(root) as session:
        clean = session.analyze_file(consumer)
        assert not any(diagnostic.code == "unresolved-symbol" for diagnostic in clean.diagnostics)

        session.set_overlay(provider, "def bar() -> int:\n    return 1\n")
        broken = session.analyze_file(consumer)
        assert any(diagnostic.code == "unresolved-symbol" for diagnostic in broken.diagnostics)

        workspace = session.analyze_workspace()
        module_by_path = {module.path: module for module in workspace.python.modules}
        consumer_module = module_by_path[str(consumer)]

        assert workspace.python.root == str(root)
        assert all(
            not module.path.startswith(session.mirror_root) for module in workspace.python.modules
        )
        assert consumer_module.resolved_imports[0].resolved_path == str(provider)
        assert consumer_module.dependencies[0].path == str(provider)


def test_workspace_session_remaps_mirror_paths_inside_diagnostic_messages(
    tmp_path: Path,
) -> None:
    """A kernel `Diagnostic` has no path field, so an integration that needs to
    name a file interpolates it into the message. Under a session that file is
    the mirror copy, in a randomly named temporary directory, so the message has
    to be remapped just like the `path` field is.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    bad = root / "bad.py"
    bad.write_bytes(b'# -*- coding: ascii -*-\nx = "\xff\xfe"\n')

    with WorkspaceSession(root) as session:
        result = session.analyze_file(bad)
        mirror_root = session.mirror_root

    decode_errors = [d for d in result.diagnostics if d.code == "source-decode-error"]
    assert len(decode_errors) == 1
    assert mirror_root not in decode_errors[0].message
    assert decode_errors[0].message.startswith(f"{bad}: ")
    # The exposed module analysis carries the same corrected text.
    assert result.module is not None
    assert [d.message for d in result.module.diagnostics] == [decode_errors[0].message]


def test_polling_workspace_watcher_batches_changes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    first = root / "a.py"
    second = root / "b.py"
    _write(first, "def a() -> int:\n    return 1\n")

    clock_state = {"now": 0.0}

    def fake_clock() -> float:
        return clock_state["now"]

    with WorkspaceSession(root) as session:
        watcher = PollingWorkspaceWatcher(session, debounce_ms=100, clock=fake_clock)

        _write(first, "def a() -> int:\n    return 22\n")
        _write(second, "def b() -> int:\n    return 3\n")

        assert watcher.poll() == ()
        clock_state["now"] = 0.11
        changed = watcher.poll()

        assert set(changed) == {str(first), str(second)}
        workspace = session.analyze_workspace()
        assert {module.path for module in workspace.python.modules} == {
            str(first),
            str(second),
        }


def test_language_server_reports_document_and_workspace_symbols(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "symbols.py"
    _write(
        target,
        "class Box:\n    pass\n\ndef helper() -> int:\n    return 1\n",
    )

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["documentSymbolProvider"] is True
        assert init["capabilities"]["hoverProvider"] is True
        assert init["capabilities"]["definitionProvider"] is True

        document_symbols = server._handle_request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": target.as_uri()}},
        )
        assert {item["name"] for item in document_symbols} == {"Box", "helper"}

        workspace_symbols = server._handle_request("workspace/symbol", {"query": "help"})
        assert len(workspace_symbols) == 1
        assert workspace_symbols[0]["name"] == "helper"
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_hover_local_function_includes_signature(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper(x: int) -> int:\n    return x\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        hover = server._handle_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )
        assert hover is not None
        assert "def helper(x: int) -> int" in hover["contents"]["value"]
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_hover_on_whitespace_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        hover = server._handle_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 3},
            },
        )
        assert hover is None
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_hover_class_shows_kind(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "class Box:\n    pass\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        hover = server._handle_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 7},
            },
        )
        assert hover is not None
        assert "class Box" in hover["contents"]["value"]
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_definition_local_function_returns_same_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )
        assert len(locations) == 1
        assert locations[0]["uri"] == target.as_uri()
        assert locations[0]["range"]["start"]["line"] == 0
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_definition_follows_cross_file_reexport(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def foo() -> int:\n    return 1\n")
    _write(consumer, "from a import foo\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )
        assert len(locations) == 1
        assert locations[0]["uri"] == provider.as_uri()
        assert locations[0]["range"]["start"]["line"] == 0
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_definition_on_unknown_identifier_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return xyz\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 1, "character": 12},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()


def _write_reexport_chain(root: Path, length: int, symbol: str) -> Path:
    """Write `length + 1` files hop_00..hop_<length> where hop_<length> defines `symbol`
    and every earlier hop does `from hop_<n+1> import <symbol>`.

    Returns the first file (hop_00), the entry point for a caller that wants to resolve
    `symbol` across `length` re-export hops.
    """
    for index in range(length):
        next_index = index + 1
        _write(
            root / f"hop_{index:02d}.py",
            f"from hop_{next_index:02d} import {symbol}\n",
        )
    _write(root / f"hop_{length:02d}.py", f"def {symbol}() -> int:\n    return 1\n")
    return root / "hop_00.py"


def test_language_server_definition_follows_single_level_wildcard_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "provider.py", "def foo() -> int:\n    return 1\n")
    consumer = root / "consumer.py"
    _write(consumer, "from provider import *\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )
        assert len(locations) == 1
        assert locations[0]["uri"] == (root / "provider.py").as_uri()
        assert locations[0]["range"]["start"]["line"] == 0
    finally:
        if server._session is not None:
            server._session.close()


def test_symbol_at_wildcard_chain_is_bounded_by_intermediate_surface(
    tmp_path: Path,
) -> None:
    """Two-level ``from X import *`` chain currently does **not** resolve end-to-end:
    ``_module_binding_analysis`` treats ``from X import *`` as a
    "top-level wildcard re-export" impurity and binds no names, so an intermediate
    module's wildcard export surface is empty. Resolution from the outer consumer
    therefore cannot see the innermost definition. This pins the design so a future
    change that widens wildcard propagation does not do so silently.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "deepest.py", "def foo() -> int:\n    return 1\n")
    _write(root / "middle.py", "from deepest import *\n")
    outer = root / "outer.py"
    _write(outer, "from middle import *\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        assert session.symbol_at(outer, SourcePosition(2, 1)) is None


def test_symbol_at_max_follow_depth_boundary(tmp_path: Path) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    entry_inside = _write_reexport_chain(inside, length=7, symbol="target")

    with WorkspaceSession(inside) as session:
        resolved = session.symbol_at(
            entry_inside, SourcePosition(0, len("from hop_01 import ") + 1)
        )
        assert resolved is not None
        assert resolved.path == str(inside / "hop_07.py")
        assert resolved.declaration.start.line == 0

    outside = tmp_path / "outside"
    outside.mkdir()
    entry_outside = _write_reexport_chain(outside, length=8, symbol="target")

    with WorkspaceSession(outside) as session:
        too_deep = session.symbol_at(
            entry_outside, SourcePosition(0, len("from hop_01 import ") + 1)
        )
        assert too_deep is None

    server = LanguageServer(default_root=str(outside))
    try:
        server._handle_request("initialize", {"rootUri": outside.as_uri()})
        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": entry_outside.as_uri()},
                "position": {"line": 0, "character": len("from hop_01 import ") + 1},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()


def test_symbol_at_cyclic_reexport_returns_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "from b import foo\n")
    _write(root / "b.py", "from a import foo\n")

    with WorkspaceSession(root) as session:
        resolved = session.symbol_at(root / "a.py", SourcePosition(0, len("from b import ") + 1))
        assert resolved is None


def test_language_server_hover_on_ambiguous_wildcard_returns_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "providers_a.py", "def foo() -> int:\n    return 1\n")
    _write(root / "providers_b.py", "def foo() -> int:\n    return 2\n")
    consumer = root / "consumer.py"
    _write(consumer, "from providers_a import *\nfrom providers_b import *\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        resolved = session.symbol_at(consumer, SourcePosition(3, 1))
        assert resolved is None

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        hover = server._handle_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 3, "character": 1},
            },
        )
        # `foo` isn't a local symbol in consumer.py (only wildcard stubs are), so
        # the hover handler finds no symbol and returns None. This pins the current
        # behavior: the LSP does not synthesize a hover payload for ambiguous
        # wildcard resolutions.
        assert hover is None

        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 3, "character": 1},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_advertises_declaration_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        response = server._handle_request("initialize", {"rootUri": root.as_uri()})
        capabilities = response["capabilities"]
        assert capabilities["declarationProvider"] is True
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_local_function_returns_def_line(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )
        # For workspace functions, declaration coincides with definition: the
        # def line. The range targets the bare-name `helper`.
        assert len(locations) == 1
        assert locations[0]["uri"] == target.as_uri()
        assert locations[0]["range"]["start"]["line"] == 0
        assert locations[0]["range"]["start"]["character"] == 4
        assert locations[0]["range"]["end"]["character"] == 10
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_import_alias_points_at_import_line(
    tmp_path: Path,
) -> None:
    # The point of `textDocument/declaration` vs `textDocument/definition`:
    # the declaration of an import alias is the `import` statement itself,
    # while the definition follows the import chain through to the imported
    # module's file. For a stdlib import like `os`, definition returns []
    # (stdlib targets are not surfaced), but declaration jumps to the
    # `import os` line in the current file.
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "import os\n\nos.getcwd()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # Cursor on `os` in the body of the file.
        decl = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )
        defn = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )

        assert len(decl) == 1
        assert decl[0]["uri"] == target.as_uri()
        assert decl[0]["range"]["start"]["line"] == 0  # `import os` line
        # Range spans the bare `os` on the import line.
        assert decl[0]["range"]["start"]["character"] == 7
        assert decl[0]["range"]["end"]["character"] == 9

        # `definition` does not surface a Location for stdlib targets, so the
        # distinction is visible: declaration points at the import statement,
        # definition is empty.
        assert defn == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_from_import_alias_stops_at_from_line(
    tmp_path: Path,
) -> None:
    # `from a import foo`: declaration is the `from … import …` line in the
    # current file. `definition` follows the chain through to `a.py`. Both
    # are useful and distinct.
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def foo() -> int:\n    return 1\n")
    _write(consumer, "from a import foo\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        decl = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )
        defn = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )

        assert len(decl) == 1
        assert decl[0]["uri"] == consumer.as_uri()  # current file
        assert decl[0]["range"]["start"]["line"] == 0  # `from a import foo`
        # The bare-name `foo` is at columns 14..17 on `from a import foo`.
        assert decl[0]["range"]["start"]["character"] == 14
        assert decl[0]["range"]["end"]["character"] == 17

        # `definition` follows through to `a.py`.
        assert len(defn) == 1
        assert defn[0]["uri"] == provider.as_uri()
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_wildcard_stub_points_at_wildcard_line(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "provider.py", "def foo() -> int:\n    return 1\n")
    consumer = root / "consumer.py"
    _write(consumer, "from provider import *\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # The local module_symbol_table only records a `wildcard_import_stub`
        # for `*` — `foo` itself isn't a bare-name entry. So
        # declaration_location_at falls through and returns None.
        decl = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )
        assert decl == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_on_unknown_identifier_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return xyz\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 1, "character": 12},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_on_whitespace_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # Whitespace position — no identifier under cursor.
        locations = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 3},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_class_returns_class_line(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "class Box:\n    pass\n\nBox()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 3, "character": 1},
            },
        )
        assert len(locations) == 1
        assert locations[0]["uri"] == target.as_uri()
        assert locations[0]["range"]["start"]["line"] == 0
        # `Box` on the class header line spans columns 6..9.
        assert locations[0]["range"]["start"]["character"] == 6
        assert locations[0]["range"]["end"]["character"] == 9
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_declaration_import_with_alias_uses_alias_offsets(
    tmp_path: Path,
) -> None:
    # `import os as my_os` — the bound name is `my_os`. Clicking on `my_os`
    # should jump to the import line, and the range should span `my_os`,
    # not the original module name `os`.
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "import os as my_os\n\nmy_os.getcwd()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 2, "character": 2},
            },
        )
        assert len(locations) == 1
        assert locations[0]["range"]["start"]["line"] == 0
        # `my_os` on the import line spans columns 13..18.
        assert locations[0]["range"]["start"]["character"] == 13
        assert locations[0]["range"]["end"]["character"] == 18
    finally:
        if server._session is not None:
            server._session.close()


def test_workspace_session_declaration_location_at_returns_dataclass(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "import os\n\nos.getcwd()\n")

    with WorkspaceSession(root) as session:
        symbol_id = SymbolId(str(target.resolve()), "module", "os", _range(0, 7, 0, 9))
        location = session.declaration_location_at(symbol_id)

    assert isinstance(location, DeclarationLocation)
    assert location.path == str(target.resolve())
    assert location.range.start.line + 1 == 1  # 1-based
    assert location.range.start.character == 7
    assert location.range.end.character == 9


def test_workspace_session_declaration_location_at_unknown_returns_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        symbol_id = SymbolId(str(target.resolve()), "module", "nonexistent", _range(0, 4, 0, 10))
        location = session.declaration_location_at(symbol_id)

    assert location is None


def test_workspace_session_declaration_location_at_missing_file_raises(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")
    missing = root / "missing.py"
    symbol_id = SymbolId(str(missing), "module", "x", _range(0, 0, 0, 1))

    with WorkspaceSession(root) as session, pytest.raises(FileNotFoundError):
        session.declaration_location_at(symbol_id)


def test_language_server_declaration_overlay_sees_edit(tmp_path: Path) -> None:
    # The overlay (editor buffer) reaches declaration just like definition.
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # Apply an overlay that introduces a new symbol `extra` on line 0.
        server._handle_notification(
            "textDocument/didChange",
            {
                "textDocument": {"uri": target.as_uri()},
                "contentChanges": [
                    {
                        "text": (
                            "def extra() -> int:\n"
                            "    return 2\n"
                            "def helper() -> int:\n"
                            "    return 1\n"
                        )
                    }
                ],
            },
        )
        locations = server._handle_request(
            "textDocument/declaration",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )
        assert len(locations) == 1
        # Overlay declaration found at the new line 0 — disk is unchanged.
        assert locations[0]["range"]["start"]["line"] == 0
        assert locations[0]["range"]["start"]["character"] == 4
        assert locations[0]["range"]["end"]["character"] == 9
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_document_symbol_surfaces_every_symbol_kind(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "all_kinds.py"
    _write(
        target,
        "import os as my_os\n"
        "from typing import Any as A\n"
        "from json import *\n"
        "\n"
        "x: int = 1\n"
        "\n"
        "class Box:\n"
        '    attr: str = ""\n'
        "\n"
        "    def method(self) -> int:\n"
        "        return 0\n"
        "\n"
        "def func() -> int:\n"
        "    return 1\n",
    )

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        document_symbols = server._handle_request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": target.as_uri()}},
        )
        kinds_by_name = {item["name"]: item["kind"] for item in document_symbols}

        assert kinds_by_name["my_os"] == _LSP_SYMBOL_KIND_VARIABLE
        assert kinds_by_name["A"] == _LSP_SYMBOL_KIND_VARIABLE
        assert kinds_by_name["*"] == _LSP_SYMBOL_KIND_VARIABLE
        assert kinds_by_name["x"] == _LSP_SYMBOL_KIND_VARIABLE
        assert kinds_by_name["Box"] == _LSP_SYMBOL_KIND_CLASS
        assert kinds_by_name["Box.attr"] == _LSP_SYMBOL_KIND_FIELD
        assert kinds_by_name["Box.method"] == _LSP_SYMBOL_KIND_METHOD
        assert kinds_by_name["func"] == _LSP_SYMBOL_KIND_FUNCTION
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_references_local_function_returns_declaration_and_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["referencesProvider"] is True
        assert init["serverInfo"]["version"] == _package_version()

        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
                "context": {"includeDeclaration": True},
            },
        )
        uris = sorted(loc["uri"] for loc in locations)
        assert uris == sorted([target.as_uri(), target.as_uri()])
        lines = sorted(loc["range"]["start"]["line"] for loc in locations)
        assert lines == [0, 3]
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_references_exclude_declaration_honored(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
                "context": {"includeDeclaration": False},
            },
        )
        assert len(locations) == 1
        assert locations[0]["range"]["start"]["line"] == 3
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_references_follows_cross_file_reexport(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def foo() -> int:\n    return 1\n")
    _write(consumer, "from a import foo\n\nfoo()\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
                "context": {"includeDeclaration": True},
            },
        )
        uris = [loc["uri"] for loc in locations]
        assert provider.as_uri() in uris
        assert consumer.as_uri() in uris
        lines_in_consumer = sorted(
            loc["range"]["start"]["line"] for loc in locations if loc["uri"] == consumer.as_uri()
        )
        assert lines_in_consumer == [2, 3]
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_references_on_unknown_identifier_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 3},
                "context": {"includeDeclaration": True},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_references_on_stdlib_identifier_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    _write(consumer, "from json import JSONDecoder\n\nJSONDecoder()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 0},
                "context": {"includeDeclaration": True},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_references_range_matches_occurrence_columns(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n\n    result = helper()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
                "context": {"includeDeclaration": False},
            },
        )
        assert len(locations) == 1
        range_ = locations[0]["range"]
        assert range_["start"]["character"] == len("    result = ")
        assert range_["end"]["character"] == len("    result = ") + len("helper")
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_references_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})

        # Overlay adds a new call site without touching disk.
        overlay_text = "def foo() -> int:\n    return 1\n\nfoo()\nfoo()\n"
        server._handle_notification(
            "textDocument/didOpen",
            {"textDocument": {"uri": target.as_uri(), "text": overlay_text}},
        )

        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
                "context": {"includeDeclaration": False},
            },
        )
        assert len(locations) == 2
        lines = sorted(loc["range"]["start"]["line"] for loc in locations)
        assert lines == [3, 4]
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_advertises_document_highlight_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["documentHighlightProvider"] is True
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_document_highlight_marks_declaration_write_and_calls_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def foo() -> int:\n"  # line 0 — declaration
        "    return 1\n"  # line 1
        "\n"  # line 2
        "foo()\n"  # line 3 — call
        "x = foo\n",  # line 4 — bare reference
    )

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})

        highlights = server._handle_request(
            "textDocument/documentHighlight",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )

        by_line = {h["range"]["start"]["line"]: h for h in highlights}
        assert set(by_line) == {0, 3, 4}

        # Declaration is reported as Write (kind=3) with a real identifier span,
        # not the synthetic col=0..1 placeholder that find_references emits.
        decl = by_line[0]
        assert decl["kind"] == 3
        assert decl["range"]["start"]["character"] == len("def ")
        assert decl["range"]["end"]["character"] == len("def ") + len("foo")

        # Call site is Text (kind=1) and spans the identifier exactly.
        call = by_line[3]
        assert call["kind"] == 1
        assert call["range"]["start"]["character"] == 0
        assert call["range"]["end"]["character"] == len("foo")

        bare = by_line[4]
        assert bare["kind"] == 1
        assert bare["range"]["start"]["character"] == len("x = ")
        assert bare["range"]["end"]["character"] == len("x = ") + len("foo")
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_document_highlight_excludes_other_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def foo() -> int:\n    return 1\n")
    _write(consumer, "from a import foo\n\nfoo()\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})

        highlights = server._handle_request(
            "textDocument/documentHighlight",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )

        # Only consumer.py occurrences should be returned; provider.py is in
        # the workspace-wide reference set but is filtered out for highlight.
        # The `from a import foo` line is an import binding (not a Name AST
        # node) so the occurrence walker does not emit it.
        assert len(highlights) == 2
        lines = sorted(h["range"]["start"]["line"] for h in highlights)
        assert lines == [2, 3]
        for highlight in highlights:
            assert highlight["range"]["start"]["character"] == 0
            assert highlight["range"]["end"]["character"] == len("foo")
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_document_highlight_on_unknown_identifier_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        highlights = server._handle_request(
            "textDocument/documentHighlight",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 3},  # cursor on `def`
            },
        )
        assert highlights == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_document_highlight_on_stdlib_identifier_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    _write(consumer, "from json import JSONDecoder\n\nJSONDecoder()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        highlights = server._handle_request(
            "textDocument/documentHighlight",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 0},
            },
        )
        assert highlights == []
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_document_highlight_overlay_sees_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # Overlay adds two new call sites without touching disk.
        overlay_text = "def foo() -> int:\n    return 1\n\nfoo()\nfoo()\n"
        server._handle_notification(
            "textDocument/didOpen",
            {"textDocument": {"uri": target.as_uri(), "text": overlay_text}},
        )

        highlights = server._handle_request(
            "textDocument/documentHighlight",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )
        lines = sorted(h["range"]["start"]["line"] for h in highlights)
        assert lines == [0, 3, 4]
    finally:
        if server._session is not None:
            server._session.close()


def test_workspace_session_find_document_highlights_returns_dataclasses(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def foo() -> int:\n    return 1\n\nfoo()\n",
    )

    with WorkspaceSession(root) as session:
        symbol_id = session.symbol_at(target, SourcePosition(0, len("def ") + 1))
        assert symbol_id is not None
        highlights = session.find_document_highlights(target, symbol_id)
        kinds = sorted(h.kind for h in highlights)
        # Exactly one declaration ("write") and one call site ("text").
        assert kinds == ["text", "write"]
        decl = next(h for h in highlights if h.kind == "write")
        assert decl.range.start.line + 1 == 1
        assert decl.range.start.character == len("def ")
        assert decl.range.end.character == len("def ") + len("foo")
        call = next(h for h in highlights if h.kind == "text")
        assert call.range.start.line + 1 == 4
        assert call.range.start.character == 0
        assert call.range.end.character == len("foo")


def test_workspace_session_find_document_highlights_non_workspace_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    _write(consumer, "from json import JSONDecoder\n\nJSONDecoder()\n")

    with WorkspaceSession(root) as session:
        assert session.symbol_at(consumer, SourcePosition(2, 1)) is None


def test_language_server_advertises_linked_editing_range_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["linkedEditingRangeProvider"] is True
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_linked_editing_range_returns_in_file_ranges(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def foo() -> int:\n"  # line 0 — declaration
        "    return 1\n"  # line 1
        "\n"  # line 2
        "foo()\n"  # line 3 — call
        "x = foo\n",  # line 4 — bare reference
    )

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})

        result = server._handle_request(
            "textDocument/linkedEditingRange",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )

        assert "wordPattern" not in result
        ranges = result["ranges"]
        # All three in-file occurrences (declaration name, call, bare ref).
        by_line = {r["start"]["line"]: r for r in ranges}
        assert set(by_line) == {0, 3, 4}
        # The declaration range spans the real identifier, not the def keyword.
        decl = by_line[0]
        assert decl["start"]["character"] == len("def ")
        assert decl["end"]["character"] == len("def ") + len("foo")
        # Every mirrored range spans exactly the identifier (identical content).
        source_lines = target.read_text().splitlines()
        for editing_range in ranges:
            text_line = source_lines[editing_range["start"]["line"]]
            assert (
                text_line[editing_range["start"]["character"] : editing_range["end"]["character"]]
                == "foo"
            )
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_linked_editing_range_excludes_other_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def foo() -> int:\n    return 1\n")
    _write(consumer, "from a import foo\n\nfoo()\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})

        result = server._handle_request(
            "textDocument/linkedEditingRange",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
            },
        )

        # Linked editing is in-file only — provider.py is filtered out. Use
        # textDocument/rename for workspace-wide edits.
        lines = sorted(r["start"]["line"] for r in result["ranges"])
        assert lines == [2, 3]
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_linked_editing_range_on_unknown_identifier_returns_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/linkedEditingRange",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 3},  # cursor on `def`
            },
        )
        assert result is None
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_linked_editing_range_on_stdlib_identifier_returns_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    _write(consumer, "from json import JSONDecoder\n\nJSONDecoder()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/linkedEditingRange",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 0},
            },
        )
        assert result is None
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_linked_editing_range_overlay_sees_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        overlay_text = "def foo() -> int:\n    return 1\n\nfoo()\nfoo()\n"
        server._handle_notification(
            "textDocument/didOpen",
            {"textDocument": {"uri": target.as_uri(), "text": overlay_text}},
        )

        result = server._handle_request(
            "textDocument/linkedEditingRange",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )
        lines = sorted(r["start"]["line"] for r in result["ranges"])
        assert lines == [0, 3, 4]
    finally:
        if server._session is not None:
            server._session.close()


def test_workspace_session_linked_editing_ranges_at_returns_dataclasses(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        symbol_id = session.symbol_at(target, SourcePosition(0, len("def ") + 1))
        assert symbol_id is not None
        ranges = session.linked_editing_ranges_at(target, symbol_id)
        assert {r.range.start.line + 1 for r in ranges} == {1, 4}
        decl = next(r for r in ranges if r.range.start.line + 1 == 1)
        assert decl.range.start.character == len("def ")
        assert decl.range.end.character == len("def ") + len("foo")


def test_workspace_session_linked_editing_ranges_at_non_workspace_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    _write(consumer, "from json import JSONDecoder\n\nJSONDecoder()\n")

    with WorkspaceSession(root) as session:
        assert session.symbol_at(consumer, SourcePosition(2, 1)) is None


def test_type_checking_imports_visible_and_lsp_hover_works(tmp_path: Path) -> None:
    """``if TYPE_CHECKING:`` imports are walked into ``ModuleSymbolTable.symbols``
    so LSP hover and goto-definition work for any bare identifier that matches a
    symbol name, including identifiers that appear inside string annotations — the
    identifier-at-position parser operates on raw source characters.
    """

    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "class Foo:\n    pass\n")
    consumer = root / "consumer.py"
    _write(
        consumer,
        "from typing import TYPE_CHECKING\n"  # line 0
        "\n"  # line 1
        "if TYPE_CHECKING:\n"  # line 2
        "    from helper import Foo\n"  # line 3
        "\n"  # line 4
        "x: Foo\n"  # line 5 — bare identifier reference
        "\n"  # line 6
        'def g(a: "Foo") -> "Foo":\n'  # line 7 — string annotation (forward-ref)
        "    return a\n",  # line 8
    )

    with WorkspaceSession(root) as session:
        analysis = session.analyze_file(consumer)
        assert analysis.symbols is not None
        qualified_names = {symbol.qualified_name for symbol in analysis.symbols.symbols}
        assert "Foo" in qualified_names
        assert "conditional top-level binding" not in analysis.symbols.impurity_reasons

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})

        # Bare identifier `Foo` on line 5 — hover resolves via the symbol table.
        hover = server._handle_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 5, "character": 3},
            },
        )
        assert hover is not None
        assert "Foo" in hover["contents"]["value"]

        # Goto-def on the bare `Foo` follows the TYPE_CHECKING import to helper.py.
        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 5, "character": 3},
            },
        )
        assert len(locations) == 1
        assert locations[0]["uri"].endswith("helper.py")

        # ``"Foo"`` inside a string annotation on line 7 — the identifier-at-position
        # parser extracts ``Foo`` from raw source characters, so hover resolves
        # against the symbol table and returns a result here too.
        hover_str = server._handle_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 7, "character": 10},
            },
        )
        assert hover_str is not None
        assert "Foo" in hover_str["contents"]["value"]
    finally:
        if server._session is not None:
            server._session.close()


# ---------------------------------------------------------------------------
# Threaded watcher (live polling)
# ---------------------------------------------------------------------------


def _wait_for_event(event: threading.Event, timeout: float = 2.0) -> bool:
    return event.wait(timeout=timeout)


def test_watcher_start_stop_clean_lifecycle(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session)
        assert watcher.is_running is False

        invocations: list[tuple[str, ...]] = []
        watcher.start(invocations.append, interval_s=0.02)
        assert watcher.is_running is True
        # Let the watcher spin a few times with no filesystem changes.
        threading.Event().wait(0.15)
        watcher.stop()
        assert watcher.is_running is False
        assert invocations == []


def test_watcher_callback_fires_for_debounced_change(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    _write(target, "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session, debounce_ms=30)
        fired = threading.Event()
        batches: list[tuple[str, ...]] = []

        def on_change(paths: tuple[str, ...]) -> None:
            batches.append(paths)
            fired.set()

        watcher.start(on_change, interval_s=0.02)
        # Make a change; content size differs from the original.
        _write(target, "x = 1\ny = 2\nz = 3\n")
        assert _wait_for_event(fired)
        watcher.stop()

        assert any(str(target) in batch for batch in batches)


def test_watcher_callback_batches_multiple_changes_per_window(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    first = root / "a.py"
    second = root / "b.py"
    _write(first, "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session, debounce_ms=40)
        lock = threading.Lock()
        batches: list[tuple[str, ...]] = []
        both_seen = threading.Event()

        def on_change(paths: tuple[str, ...]) -> None:
            with lock:
                batches.append(paths)
                combined_inner = {p for batch in batches for p in batch}
                if str(first) in combined_inner and str(second) in combined_inner:
                    both_seen.set()

        watcher.start(on_change, interval_s=0.01)
        # Both writes land before any debounce window can fully close.
        _write(first, "x = 11\n")
        _write(second, "y = 22\n")
        assert _wait_for_event(both_seen, timeout=3.0)
        watcher.stop()

        with lock:
            combined = {p for batch in batches for p in batch}
        assert str(first) in combined
        assert str(second) in combined


def test_watcher_callback_error_is_contained(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    _write(target, "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session, debounce_ms=30)
        error_seen = threading.Event()
        errors: list[Exception] = []

        def on_change(_paths: tuple[str, ...]) -> None:
            raise RuntimeError("boom")

        def on_error(exc: Exception) -> None:
            errors.append(exc)
            error_seen.set()

        watcher.start(on_change, interval_s=0.02, on_error=on_error)
        _write(target, "x = 99\n")
        assert _wait_for_event(error_seen)
        # Thread should still be alive after the callback raised.
        assert watcher.is_running is True
        watcher.stop()

        assert errors and isinstance(errors[0], RuntimeError)


def test_watcher_double_start_raises(tmp_path: Path, watcher_factory: _WatcherFactory) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session)
        watcher.start(lambda _paths: None, interval_s=0.02)
        try:
            with pytest.raises(RuntimeError):
                watcher.start(lambda _paths: None)
        finally:
            watcher.stop()


def test_watcher_double_stop_is_noop(tmp_path: Path, watcher_factory: _WatcherFactory) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session)
        watcher.stop()  # before start
        watcher.start(lambda _paths: None, interval_s=0.02)
        watcher.stop()
        watcher.stop()  # after start + stop — still a no-op
        assert watcher.is_running is False


def test_watcher_poll_after_start_raises(tmp_path: Path, watcher_factory: _WatcherFactory) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session)
        watcher.start(lambda _paths: None, interval_s=0.02)
        try:
            with pytest.raises(RuntimeError):
                watcher.poll()
        finally:
            watcher.stop()


def test_watcher_stop_joins_within_timeout(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
    import time

    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session)
        watcher.start(lambda _paths: None, interval_s=0.2)
        start = time.monotonic()
        watcher.stop(timeout=2.0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5


def test_watcher_concurrent_overlay_and_poll_preserves_mirror(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    _write(target, "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session, debounce_ms=30)
        stop_event = threading.Event()
        errors: list[Exception] = []
        watcher.start(lambda _paths: None, interval_s=0.01, on_error=errors.append)
        try:
            # Hammer overlays from the main thread while the watcher loops.
            for i in range(20):
                session.set_overlay(target, f"x = {i}\n")
                stop_event.wait(0.01)
            final = session.set_overlay(target, "x = 999\n")
            stop_event.wait(0.1)
            # The latest overlay should be visible via analyze_file (uses mirror + db).
            assert session.source_text(final) == "x = 999\n"
        finally:
            watcher.stop()
        assert errors == []


def test_watcher_context_manager_stops_on_exit(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    with WorkspaceSession(root) as session:
        watcher = watcher_factory(session)
        with watcher:
            watcher.start(lambda _paths: None, interval_s=0.02)
            assert watcher.is_running is True
        assert watcher.is_running is False


def test_session_close_stops_watcher_before_removing_mirror(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    session = WorkspaceSession(root)
    watchers = (PollingWorkspaceWatcher(session), PollingWorkspaceWatcher(session))
    for watcher in watchers:
        watcher.start(lambda _paths: None, interval_s=60.0)
    threads = tuple(watcher._thread for watcher in watchers)
    assert all(thread is not None and thread.is_alive() for thread in threads)

    session.close()

    assert all(thread is not None and not thread.is_alive() for thread in threads)
    assert all(watcher.is_running is False for watcher in watchers)
    assert not Path(session.mirror_root).exists()


def test_session_can_close_from_watcher_callback(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    _write(target, "x = 1\n")

    session = WorkspaceSession(root)
    watcher = PollingWorkspaceWatcher(session, debounce_ms=0)
    callback_finished = threading.Event()
    errors: list[Exception] = []

    def close_session(_paths: tuple[str, ...]) -> None:
        session.close()
        callback_finished.set()

    watcher.start(close_session, interval_s=0.01, on_error=errors.append)
    thread = watcher._thread
    assert thread is not None
    _write(target, "x = 22\n")

    assert callback_finished.wait(timeout=2.0)
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert errors == []
    assert not Path(session.mirror_root).exists()


def test_concurrent_session_close_does_not_deadlock_watcher_callback(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    _write(target, "x = 1\n")

    session = WorkspaceSession(root)
    watcher = PollingWorkspaceWatcher(session, debounce_ms=0)
    callback_started = threading.Event()
    finish_callback = threading.Event()
    callback_finished = threading.Event()

    def close_session(_paths: tuple[str, ...]) -> None:
        callback_started.set()
        assert finish_callback.wait(timeout=2.0)
        session.close()
        callback_finished.set()

    watcher.start(close_session, interval_s=0.01)
    _write(target, "x = 22\n")
    assert callback_started.wait(timeout=2.0)

    close_thread = threading.Thread(target=session.close)
    close_thread.start()
    try:
        assert watcher._stop_event.wait(timeout=2.0)
        finish_callback.set()
        assert callback_finished.wait(timeout=2.0)
        close_thread.join(timeout=2.0)
        assert not close_thread.is_alive()
    finally:
        finish_callback.set()
        watcher.stop(timeout=2.0)
        close_thread.join(timeout=2.0)

    assert watcher.is_running is False
    assert not Path(session.mirror_root).exists()


def test_watcher_cannot_start_after_session_close(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    session = WorkspaceSession(root)
    watcher = PollingWorkspaceWatcher(session)
    session.close()

    with pytest.raises(RuntimeError, match="WorkspaceSession is closed"):
        watcher.start(lambda _paths: None)
    assert watcher.is_running is False


def test_watcher_start_failure_unregisters_from_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    session = WorkspaceSession(root)
    watcher = PollingWorkspaceWatcher(session)

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        watcher.start(lambda _paths: None)

    assert watcher.is_running is False
    assert session._watchers == set()
    session.close()


def test_session_raises_after_close(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    _write(target, "x = 1\n")

    session = WorkspaceSession(root)
    session.close()
    with pytest.raises(RuntimeError):
        session.set_overlay(target, "x = 2\n")
    # source_text tolerates close (returns None) so it is safe to call from a
    # thread that races with session teardown.
    assert session.source_text(target) is None


def test_local_symbol_and_binding_lookups_raise_after_close(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    _write(target, "x = 1\n")

    session = WorkspaceSession(root)
    session.close()
    with pytest.raises(RuntimeError):
        session._local_symbol_at(target, SourcePosition(0, 0))
    with pytest.raises(RuntimeError):
        session._local_binding_at(target, SourcePosition(0, 0))


def test_language_server_initialize_starts_watcher_and_shutdown_stops_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request(
            "initialize",
            {
                "rootUri": root.as_uri(),
                "initializationOptions": {
                    "pyinc.watcher.enabled": True,
                    "pyinc.watcher.debounceMs": 30,
                    "pyinc.watcher.intervalMs": 20,
                },
            },
        )
        assert server._watcher is not None
        assert server._watcher.is_running is True

        server._handle_request("shutdown", {})
        assert server._watcher is None
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_watcher_opt_out(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request(
            "initialize",
            {
                "rootUri": root.as_uri(),
                "initializationOptions": {
                    "pyinc.watcher.enabled": False,
                },
            },
        )
        assert server._watcher is None
    finally:
        if server._session is not None:
            server._session.close()


class _PairedWriteStream:
    """In-memory output stream that stalls after each write until the other
    writer thread has also written.

    ``write_message`` emits a frame as two writes — header, then body. The
    rendezvous forces both writers' headers onto the stream before either
    body, so frames interleave unless each writer keeps whole frames atomic.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._chunks_lock = threading.Lock()
        self._rendezvous = threading.Barrier(2)

    def write(self, data: bytes) -> int:
        with self._chunks_lock:
            self._chunks.append(bytes(data))
        # A timeout breaks the barrier for good: once one writer holds its
        # frame together, the peer is blocked outside the stream rather than
        # mid-frame, and every later write proceeds without waiting.
        with contextlib.suppress(threading.BrokenBarrierError):
            self._rendezvous.wait(timeout=1.0)
        return len(data)

    def flush(self) -> None:
        pass

    def getvalue(self) -> bytes:
        with self._chunks_lock:
            return b"".join(self._chunks)


def test_concurrent_response_and_watcher_notification_writes_keep_framing_parseable() -> None:
    stream = _PairedWriteStream()
    server = LanguageServer(in_stream=io.BytesIO(), out_stream=cast(BinaryIO, stream))
    message_count = 8
    ready = threading.Barrier(2)

    def send_responses() -> None:
        # The request loop's side of the race: responses on the main thread.
        ready.wait(timeout=5.0)
        for sequence in range(message_count):
            server._send({"jsonrpc": "2.0", "id": sequence, "result": {"writer": "loop"}})

    def send_notifications() -> None:
        # The watcher callback's side: publishes on the polling thread.
        ready.wait(timeout=5.0)
        for sequence in range(message_count):
            server._send_notification("test/ping", {"writer": "watcher", "sequence": sequence})

    writers = (
        threading.Thread(target=send_responses),
        threading.Thread(target=send_notifications),
    )
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=30.0)
    assert all(not writer.is_alive() for writer in writers)

    replay = io.BytesIO(stream.getvalue())
    messages: list[dict[str, Any]] = []
    while True:
        try:
            message = read_message(replay)
        except ParseError as exc:
            pytest.fail(f"interleaved writers corrupted the Content-Length framing: {exc}")
        if message is None:
            break
        messages.append(message)

    assert len(messages) == 2 * message_count
    response_ids = sorted(message["id"] for message in messages if "id" in message)
    notification_sequences = sorted(
        message["params"]["sequence"] for message in messages if "method" in message
    )
    assert response_ids == list(range(message_count))
    assert notification_sequences == list(range(message_count))


def _apply_rename_edits(edits: tuple[RenameEdit, ...]) -> None:
    """Apply RenameEdits to disk, right-to-left within each file."""
    by_path: dict[str, list[RenameEdit]] = {}
    for edit in edits:
        by_path.setdefault(edit.path, []).append(edit)
    for path, file_edits in by_path.items():
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        ordered = sorted(
            file_edits,
            key=lambda edit: (-edit.range.start.line, -edit.range.start.character),
        )
        for edit in ordered:
            line = lines[edit.range.start.line]
            newline = "\n" if line.endswith("\n") else ""
            content = line[:-1] if newline else line
            patched = (
                content[: edit.range.start.character]
                + edit.new_text
                + content[edit.range.end.character :]
            )
            lines[edit.range.start.line] = patched + newline
        Path(path).write_text("".join(lines), encoding="utf-8")


def test_rename_symbol_function_updates_def_call_and_import_sites(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")
    _write(root / "b.py", "from a import foo\n\nfoo()\nfoo()\n")
    _write(root / "c.py", "from a import foo as aliased\n\naliased()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, root / "b.py", "foo"), "bar")
        assert result.status == "ok"
        assert isinstance(result.target, SymbolId)
        edits_by_file: dict[str, list[tuple[int, int, int, str]]] = {}
        for edit in result.edits:
            edits_by_file.setdefault(Path(edit.path).name, []).append(
                (
                    edit.range.start.line + 1,
                    edit.range.start.character,
                    edit.range.end.character,
                    edit.new_text,
                )
            )
        assert edits_by_file["a.py"] == [(1, 4, 7, "bar")]
        assert edits_by_file["b.py"] == [
            (1, 14, 17, "bar"),
            (3, 0, 3, "bar"),
            (4, 0, 3, "bar"),
        ]
        # The `as aliased` clause is preserved; only the source name `foo`
        # in the import is rewritten.
        assert edits_by_file["c.py"] == [(1, 14, 17, "bar")]
        _apply_rename_edits(result.edits)

    assert (root / "a.py").read_text() == "def bar() -> int:\n    return 1\n"
    assert (root / "b.py").read_text() == "from a import bar\n\nbar()\nbar()\n"
    assert (root / "c.py").read_text() == "from a import bar as aliased\n\naliased()\n"


def test_rename_symbol_class_locates_def_offset_in_decorated_line(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "x.py", "class Widget:\n    pass\n")
    _write(root / "y.py", "from x import Widget\n\nWidget()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, root / "x.py", "Widget"), "Gadget")
        assert result.status == "ok"
        per_file = sorted(
            (
                Path(e.path).name,
                e.range.start.line + 1,
                e.range.start.character,
                e.range.end.character,
            )
            for e in result.edits
        )
        assert per_file == [
            ("x.py", 1, 6, 12),
            ("y.py", 1, 14, 20),
            ("y.py", 3, 0, 6),
        ]
        _apply_rename_edits(result.edits)

    assert (root / "x.py").read_text() == "class Gadget:\n    pass\n"
    assert (root / "y.py").read_text() == "from x import Gadget\n\nGadget()\n"


def test_rename_symbol_rejects_invalid_identifier(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, root / "a.py", "foo"), "1bad")
        assert result.status == "invalid_identifier"
        assert result.edits == ()


def test_rename_symbol_rejects_python_keyword(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, root / "a.py", "foo"), "class")
        assert result.status == "keyword_identifier"
        assert result.edits == ()


def test_rename_symbol_same_name_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, root / "a.py", "foo"), "foo")
        assert result.status == "same_name"
        assert result.edits == ()


def test_symbol_at_rejects_non_workspace_target(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "consumer.py",
        "from json import JSONDecoder\n\nJSONDecoder()\n",
    )

    with WorkspaceSession(root) as session:
        assert session.symbol_at(root / "consumer.py", SourcePosition(2, 1)) is None


def test_local_alias_binding_preserves_lexical_symbol_id(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")
    _write(root / "c.py", "from a import foo as aliased\n\naliased()\n")

    with WorkspaceSession(root) as session:
        target = session._local_symbol_at(root / "c.py", SourcePosition(2, 1))
        assert target is not None
        assert target.name == "aliased"
        assert target.path == str(root / "c.py")


def test_rename_symbol_rewrites_sibling_relative_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "sub.py", "from .helper import foo\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, pkg / "helper.py", "foo"), "bar")
        assert result.status == "ok"
        sub_edits = sorted(
            (edit.range.start.line + 1, edit.range.start.character, edit.range.end.character)
            for edit in result.edits
            if Path(edit.path).name == "sub.py"
        )
        assert sub_edits == [(1, 20, 23), (3, 0, 3)]
        _apply_rename_edits(result.edits)

    assert (pkg / "helper.py").read_text() == "def bar() -> int:\n    return 1\n"
    assert (pkg / "sub.py").read_text() == "from .helper import bar\n\nbar()\n"


def test_rename_symbol_rewrites_dotted_relative_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    sub = pkg / "sub"
    _write(pkg / "__init__.py", "")
    _write(sub / "__init__.py", "")
    _write(sub / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(sub / "user.py", "from .helper import foo\n\nfoo()\n")
    _write(pkg / "outer.py", "from .sub.helper import foo\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, sub / "helper.py", "foo"), "bar")
        assert result.status == "ok"
        rewritten = sorted(Path(e.path).name for e in result.edits)
        assert "user.py" in rewritten
        assert "outer.py" in rewritten
        _apply_rename_edits(result.edits)

    assert (sub / "user.py").read_text() == "from .helper import bar\n\nbar()\n"
    assert (pkg / "outer.py").read_text() == "from .sub.helper import bar\n\nbar()\n"


def test_rename_symbol_rewrites_parent_package_relative_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    sub = pkg / "sub"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(sub / "__init__.py", "")
    _write(sub / "user.py", "from ..helper import foo\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, pkg / "helper.py", "foo"), "bar")
        assert result.status == "ok"
        _apply_rename_edits(result.edits)

    assert (sub / "user.py").read_text() == "from ..helper import bar\n\nbar()\n"


def test_rename_symbol_relative_import_preserves_as_alias(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "sub.py", "from .helper import foo as aliased\n\naliased()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, pkg / "helper.py", "foo"), "bar")
        assert result.status == "ok"
        sub_edits = [e for e in result.edits if Path(e.path).name == "sub.py"]
        # Exactly one edit on sub.py — the import-site `foo`. The `as aliased`
        # clause is preserved and `aliased()` is not a reference to `foo`.
        assert len(sub_edits) == 1
        assert sub_edits[0].range.start.character == 20
        assert sub_edits[0].range.end.character == 23
        _apply_rename_edits(result.edits)

    assert (pkg / "sub.py").read_text() == "from .helper import bar as aliased\n\naliased()\n"


def test_rename_symbol_rewrites_attribute_access_through_module_import(
    tmp_path: Path,
) -> None:
    """When a consumer uses ``import a; a.foo()``, rename of ``foo`` rewrites
    just the ``foo`` portion of ``a.foo`` (the leading ``a.`` is left intact),
    in addition to the canonical declaration site. ``import a as alias``
    plus ``alias.foo()`` is rewritten the same way."""
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")
    _write(root / "b.py", "import a\n\na.foo()\n")
    _write(root / "c.py", "import a as alias\n\nalias.foo()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(_symbol_for_name(session, root / "a.py", "foo"), "bar")
        assert result.status == "ok"
        per_file = sorted(
            (
                Path(e.path).name,
                e.range.start.line + 1,
                e.range.start.character,
                e.range.end.character,
                e.new_text,
            )
            for e in result.edits
        )
        assert per_file == [
            ("a.py", 1, 4, 7, "bar"),
            ("b.py", 3, 2, 5, "bar"),
            ("c.py", 3, 6, 9, "bar"),
        ]
        _apply_rename_edits(result.edits)

    assert (root / "a.py").read_text() == "def bar() -> int:\n    return 1\n"
    assert (root / "b.py").read_text() == "import a\n\na.bar()\n"
    assert (root / "c.py").read_text() == "import a as alias\n\nalias.bar()\n"


def test_rename_symbol_with_overlay_uses_overlay_text(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")
    _write(root / "b.py", "from a import foo\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        # An overlay adds another call site on b.py that isn't on disk yet.
        session.set_overlay(root / "b.py", "from a import foo\n\nfoo()\nfoo()\n")
        result = session.rename_symbol(_symbol_for_name(session, root / "b.py", "foo"), "bar")
        assert result.status == "ok"
        b_edits = sorted(
            edit.range.start.line + 1 for edit in result.edits if Path(edit.path).name == "b.py"
        )
        assert b_edits == [1, 3, 4]


def test_language_server_advertises_rename_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["renameProvider"] == {"prepareProvider": True}
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_prepare_rename_returns_range_for_workspace_symbol(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper() -> int:\n    return 1\n\nhelper()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        prepared = server._handle_request(
            "textDocument/prepareRename",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
            },
        )
        assert prepared == {
            "range": {
                "start": {"line": 0, "character": 4},
                "end": {"line": 0, "character": 10},
            },
            "placeholder": "helper",
        }
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_prepare_rename_returns_null_for_stdlib_symbol(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "consumer.py"
    _write(target, "from json import JSONDecoder\n\nJSONDecoder()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        prepared = server._handle_request(
            "textDocument/prepareRename",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 2, "character": 0},
            },
        )
        assert prepared is None
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_rename_returns_workspace_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def foo() -> int:\n    return 1\n")
    _write(consumer, "from a import foo\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        edit = server._handle_request(
            "textDocument/rename",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 1},
                "newName": "bar",
            },
        )
        assert edit is not None
        changes = edit["changes"]
        assert set(changes.keys()) == {provider.as_uri(), consumer.as_uri()}
        provider_changes = changes[provider.as_uri()]
        assert provider_changes == [
            {
                "range": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 7},
                },
                "newText": "bar",
            }
        ]
        consumer_changes = sorted(
            changes[consumer.as_uri()],
            key=lambda c: (
                c["range"]["start"]["line"],
                c["range"]["start"]["character"],
            ),
        )
        assert consumer_changes == [
            {
                "range": {
                    "start": {"line": 0, "character": 14},
                    "end": {"line": 0, "character": 17},
                },
                "newText": "bar",
            },
            {
                "range": {
                    "start": {"line": 2, "character": 0},
                    "end": {"line": 2, "character": 3},
                },
                "newText": "bar",
            },
        ]
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_rename_invalid_identifier_raises_request_failed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        with pytest.raises(_RequestFailed):
            server._handle_request(
                "textDocument/rename",
                {
                    "textDocument": {"uri": target.as_uri()},
                    "position": {"line": 0, "character": 5},
                    "newName": "1bad",
                },
            )
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_rename_keyword_raises_request_failed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        with pytest.raises(_RequestFailed):
            server._handle_request(
                "textDocument/rename",
                {
                    "textDocument": {"uri": target.as_uri()},
                    "position": {"line": 0, "character": 5},
                    "newName": "class",
                },
            )
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_rename_alias_raises_request_failed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")
    consumer = root / "c.py"
    _write(consumer, "from a import foo as aliased\n\naliased()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        with pytest.raises(_RequestFailed):
            server._handle_request(
                "textDocument/rename",
                {
                    "textDocument": {"uri": consumer.as_uri()},
                    "position": {"line": 2, "character": 0},
                    "newName": "quux",
                },
            )
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_rename_on_stdlib_symbol_returns_null(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    _write(consumer, "from json import JSONDecoder\n\nJSONDecoder()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/rename",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 2, "character": 0},
                "newName": "MyDecoder",
            },
        )
        assert result is None
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_rename_same_name_returns_null(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/rename",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 5},
                "newName": "foo",
            },
        )
        assert result is None
    finally:
        if server._session is not None:
            server._session.close()


def test_signature_help_at_local_function_active_first_param(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def helper(x: int, y: int) -> int:\n    return x + y\n\nhelper()\n",
    )
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=3, character=7)
        assert signature_help is not None
        assert signature_help.label == "def helper(x: int, y: int) -> int"
        assert signature_help.active_parameter == 0
        assert tuple(
            (p.label, p.label_offset_start, p.label_offset_end) for p in signature_help.parameters
        ) == (
            ("x: int", 11, 17),
            ("y: int", 19, 25),
        )


def test_signature_help_at_advances_active_parameter_after_comma(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def helper(a: int, b: int, c: int) -> int:\n    return a + b + c\n\nhelper(1, 2, 3)\n",
    )
    with WorkspaceSession(root) as session:
        # Just inside `(`: arg 0.
        first = session.signature_help_at(target, line=3, character=7)
        # After "1, ": arg 1.
        second = session.signature_help_at(target, line=3, character=10)
        # After "1, 2, ": arg 2.
        third = session.signature_help_at(target, line=3, character=13)
    assert first is not None and first.active_parameter == 0
    assert second is not None and second.active_parameter == 1
    assert third is not None and third.active_parameter == 2


def test_signature_help_at_returns_none_outside_call(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper(x: int) -> int:\n    return x\n")
    with WorkspaceSession(root) as session:
        assert session.signature_help_at(target, line=0, character=5) is None


def test_signature_help_at_skips_call_in_string_literal(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        'def helper(x: int) -> int:\n    return x\n\nvalue = "foo(" + str(\n',
    )
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=3, character=21)
    assert signature_help is None


def test_signature_help_at_nested_call_picks_innermost(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def outer(x: int) -> int:\n"
        "    return x\n"
        "\n"
        "def inner(y: int, z: int) -> int:\n"
        "    return y + z\n"
        "\n"
        "outer(inner(1, ))\n",
    )
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=6, character=15)
    assert signature_help is not None
    assert signature_help.label.startswith("def inner(")
    assert signature_help.active_parameter == 1


def test_signature_help_at_resolves_cross_module_reexport(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "a.py"
    consumer = root / "b.py"
    _write(provider, "def helper(x: int, y: int) -> int:\n    return x + y\n")
    _write(consumer, "from a import helper\n\nhelper(1, 2)\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(consumer, line=2, character=10)
    assert signature_help is not None
    assert signature_help.label == "def helper(x: int, y: int) -> int"
    assert signature_help.active_parameter == 1


def test_signature_help_at_class_uses_init_without_self(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "class Box:\n"
        "    def __init__(self, width: int, height: int) -> None:\n"
        "        self.width = width\n"
        "        self.height = height\n"
        "\n"
        "Box()\n",
    )
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=5, character=4)
    assert signature_help is not None
    assert signature_help.label == "def Box(width: int, height: int)"
    assert signature_help.active_parameter == 0


def test_signature_help_at_class_without_init_returns_empty_signature(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "class Bare:\n    pass\n\nBare()\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=3, character=5)
    assert signature_help is not None
    assert signature_help.label == "def Bare()"
    assert signature_help.active_parameter is None
    assert signature_help.parameters == ()


def test_signature_help_at_stdlib_target_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "from json import dumps\n\ndumps(\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=2, character=6)
    assert signature_help is None


def test_signature_help_at_def_definition_header_is_not_a_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper(x: int) -> int:\n    return x\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=0, character=11)
    assert signature_help is None


def test_signature_help_at_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper(x: int) -> int:\n    return x\n\nhelper(1)\n")
    with WorkspaceSession(root) as session:
        overlay_text = "def helper(a: int, b: int) -> int:\n    return a + b\n\nhelper(1, 2)\n"
        session.set_overlay(target, overlay_text)
        signature_help = session.signature_help_at(target, line=3, character=11)
    assert signature_help is not None
    assert signature_help.label == "def helper(a: int, b: int) -> int"
    assert signature_help.active_parameter == 1


def test_language_server_advertises_signature_help_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["signatureHelpProvider"] == {
            "triggerCharacters": ["(", ","],
            "retriggerCharacters": [","],
        }
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_signature_help_returns_lsp_payload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def helper(x: int, y: int) -> int:\n    return x + y\n\nhelper(1, )\n",
    )
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/signatureHelp",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 3, "character": 10},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is not None
    assert result["activeSignature"] == 0
    assert result["activeParameter"] == 1
    signatures = result["signatures"]
    assert len(signatures) == 1
    assert signatures[0]["label"] == "def helper(x: int, y: int) -> int"
    assert signatures[0]["parameters"] == [
        {"label": [11, 17]},
        {"label": [19, 25]},
    ]


def test_language_server_signature_help_outside_call_returns_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def helper(x: int) -> int:\n    return x\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/signatureHelp",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 0, "character": 0},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_folding_ranges_cover_function_class_and_method_bodies(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def foo(x: int) -> int:\n"
        "    if x:\n"
        "        return 1\n"
        "    return 2\n"
        "\n"
        "class Box:\n"
        "    def method(self) -> int:\n"
        "        return 1\n"
        "\n"
        "    def other(self) -> int:\n"
        "        return 2\n",
    )
    with WorkspaceSession(root) as session:
        ranges = session.folding_ranges_for_file(target)
    signatures = _fold_signatures(ranges)
    assert (1, 4, "region") in signatures
    assert (6, 11, "region") in signatures
    assert (7, 8, "region") in signatures
    assert (10, 11, "region") in signatures


def test_folding_ranges_decorated_function_starts_at_first_decorator(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "@first\n@second\ndef decorated(x: int) -> int:\n    return x\n",
    )
    with WorkspaceSession(root) as session:
        ranges = session.folding_ranges_for_file(target)
    assert _fold_signatures(ranges) == {(1, 4, "region")}


def test_folding_ranges_group_consecutive_top_level_imports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "import a\nimport b\nfrom c import d\n\ndef main() -> int:\n    return 0\n",
    )
    with WorkspaceSession(root) as session:
        ranges = session.folding_ranges_for_file(target)
    signatures = _fold_signatures(ranges)
    assert (1, 3, "imports") in signatures
    assert (5, 6, "region") in signatures


def test_folding_ranges_collapse_multi_line_from_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "from c import (\n    d,\n    e,\n)\n",
    )
    with WorkspaceSession(root) as session:
        ranges = session.folding_ranges_for_file(target)
    assert _fold_signatures(ranges) == {(1, 4, "imports")}


def test_folding_ranges_skip_single_line_definitions(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "import a\nx = 1\n")
    with WorkspaceSession(root) as session:
        ranges = session.folding_ranges_for_file(target)
    assert ranges == ()


def test_folding_ranges_invalid_syntax_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def broken(\n")
    with WorkspaceSession(root) as session:
        ranges = session.folding_ranges_for_file(target)
    assert ranges == ()


def test_folding_ranges_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def foo() -> int:\n    return 1\n")
    with WorkspaceSession(root) as session:
        before = session.folding_ranges_for_file(target)
        assert (1, 2, "region") in _fold_signatures(before)

        session.set_overlay(
            target,
            "def foo() -> int:\n    if True:\n        return 1\n    return 2\n",
        )
        after = session.folding_ranges_for_file(target)
    assert (1, 4, "region") in _fold_signatures(after)


def test_folding_ranges_for_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with WorkspaceSession(root) as session, pytest.raises(FileNotFoundError):
        session.folding_ranges_for_file(root / "missing.py")


def test_language_server_advertises_folding_range_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["foldingRangeProvider"] is True
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_folding_range_returns_lsp_payload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "import a\n"
        "import b\n"
        "\n"
        "def foo() -> int:\n"
        "    return 1\n"
        "\n"
        "class Box:\n"
        "    def method(self) -> int:\n"
        "        return 2\n",
    )
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/foldingRange",
            {"textDocument": {"uri": target.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert {
        "startLine": 0,
        "startCharacter": 0,
        "endLine": 1,
        "endCharacter": len("import b"),
        "kind": "imports",
    } in result
    assert {
        "startLine": 3,
        "startCharacter": 0,
        "endLine": 4,
        "endCharacter": len("    return 1"),
    } in result
    assert {
        "startLine": 6,
        "startCharacter": 0,
        "endLine": 8,
        "endCharacter": len("        return 2"),
    } in result
    assert {
        "startLine": 7,
        "startCharacter": len("    "),
        "endLine": 8,
        "endCharacter": len("        return 2"),
    } in result
    for entry in result:
        assert "kind" not in entry or entry["kind"] in ("imports", "comment", "region")


def test_language_server_folding_range_for_unparseable_file_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def broken(\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/foldingRange",
            {"textDocument": {"uri": target.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result == []


def test_selection_ranges_chain_innermost_to_outermost(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def foo(x: int) -> int:\n    return x + 1\n",
    )
    with WorkspaceSession(root) as session:
        # cursor on the `x` in `return x + 1` (line 1, character 11)
        chain = session.selection_ranges_at(target, 1, 11)

    assert len(chain) >= 3
    # Innermost is the bare Name `x`.
    assert chain[0] == SelectionRange(range=_range(1, 11, 1, 12))
    # Each subsequent range strictly contains its predecessor.
    for inner, outer in zip(chain, chain[1:], strict=False):
        assert (outer.range.start.line, outer.range.start.character) <= (
            inner.range.start.line,
            inner.range.start.character,
        )
        assert (outer.range.end.line, outer.range.end.character) >= (
            inner.range.end.line,
            inner.range.end.character,
        )
        assert (
            outer.range.start.line,
            outer.range.start.character,
            outer.range.end.line,
            outer.range.end.character,
        ) != (
            inner.range.start.line,
            inner.range.start.character,
            inner.range.end.line,
            inner.range.end.character,
        )
    # Outermost reaches the function definition (line 0..1).
    outermost = chain[-1]
    assert outermost.range.start.line == 0
    assert outermost.range.end.line == 1


def test_selection_ranges_for_attribute_access_picks_up_each_subexpression(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "v = a.b.c\n")
    with WorkspaceSession(root) as session:
        # cursor on the `c` in `a.b.c` (line 0, character 9)
        chain = session.selection_ranges_at(target, 0, 8)

    starts_and_ends = [(r.range.start.character, r.range.end.character) for r in chain]
    # Expect at least: `c` (8,9), `a.b.c` (4,9), full assignment (0,9).
    assert (0, 9) in starts_and_ends
    assert (4, 9) in starts_and_ends


def test_selection_ranges_for_invalid_syntax_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def broken(\n")
    with WorkspaceSession(root) as session:
        chain = session.selection_ranges_at(target, 0, 4)
    assert chain == ()


def test_selection_ranges_for_position_out_of_bounds_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "x = 1\n")
    with WorkspaceSession(root) as session:
        assert session.selection_ranges_at(target, 99, 0) == ()
        assert session.selection_ranges_at(target, -1, 0) == ()
        assert session.selection_ranges_at(target, 0, 99) == ()


def test_selection_ranges_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "x = 1\n")
    with WorkspaceSession(root) as session:
        before = session.selection_ranges_at(target, 0, 4)
        assert any(r.range.end.character == 5 for r in before)

        session.set_overlay(target, "x = 1 + 2 + 3\n")
        after = session.selection_ranges_at(target, 0, 4)
    assert any(r.range.end.character == 13 for r in after)


def test_selection_ranges_for_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with WorkspaceSession(root) as session, pytest.raises(FileNotFoundError):
        session.selection_ranges_at(root / "missing.py", 0, 0)


def test_language_server_advertises_selection_range_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        assert init["capabilities"]["selectionRangeProvider"] is True
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_selection_range_returns_lsp_payload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def foo(x: int) -> int:\n    return x + 1\n",
    )
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/selectionRange",
            {
                "textDocument": {"uri": target.as_uri()},
                "positions": [{"line": 1, "character": 11}],
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert isinstance(result, list)
    assert len(result) == 1
    head = result[0]
    # Innermost: bare Name `x`.
    assert head["range"] == {
        "start": {"line": 1, "character": 11},
        "end": {"line": 1, "character": 12},
    }
    # Walk parent chain — each parent must contain its child.
    current = head
    seen = 1
    while "parent" in current:
        parent = current["parent"]
        assert (
            parent["range"]["start"]["line"],
            parent["range"]["start"]["character"],
        ) <= (
            current["range"]["start"]["line"],
            current["range"]["start"]["character"],
        )
        assert (
            parent["range"]["end"]["line"],
            parent["range"]["end"]["character"],
        ) >= (
            current["range"]["end"]["line"],
            current["range"]["end"]["character"],
        )
        current = parent
        seen += 1
    assert seen >= 3


def test_language_server_selection_range_emits_zero_width_for_unparseable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def broken(\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/selectionRange",
            {
                "textDocument": {"uri": target.as_uri()},
                "positions": [{"line": 0, "character": 4}],
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result == [
        {
            "range": {
                "start": {"line": 0, "character": 4},
                "end": {"line": 0, "character": 4},
            }
        }
    ]


def test_language_server_selection_range_handles_multiple_positions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "x = 1\ny = 2\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/selectionRange",
            {
                "textDocument": {"uri": target.as_uri()},
                "positions": [
                    {"line": 0, "character": 0},
                    {"line": 1, "character": 0},
                ],
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert isinstance(result, list)
    assert len(result) == 2
    # Each entry contains its own first range starting at (line, 0).
    assert result[0]["range"]["start"] == {"line": 0, "character": 0}
    assert result[1]["range"]["start"] == {"line": 1, "character": 0}


def test_document_links_for_plain_import_targets_module_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def greet() -> str:\n    return 'hi'\n")
    consumer = root / "app.py"
    _write(consumer, "import helper\n\nprint(helper.greet())\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    assert links == (
        DocumentLink(
            range=_range(
                0,
                len("import "),
                0,
                len("import ") + len("helper"),
            ),
            target_path=str(root / "helper.py"),
        ),
    )


def test_document_links_for_import_as_covers_alias_span(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "x = 1\n")
    consumer = root / "app.py"
    _write(consumer, "import helper as h\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    # The AST alias span covers `helper as h` end-to-end, which matches what most
    # LSP clients underline.
    assert len(links) == 1
    link = links[0]
    assert link.range.start.line == 0
    assert link.range.start.character == len("import ")
    assert link.range.end.character == len("import helper as h")
    assert link.target_path == str(root / "helper.py")


def test_document_links_for_from_import_link_each_alias(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "ALPHA = 1\nBETA = 2\n")
    consumer = root / "app.py"
    _write(consumer, "from helper import ALPHA, BETA\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    assert len(links) == 2
    by_char = sorted(links, key=lambda link: link.range.start.character)
    alpha, beta = by_char
    assert alpha.range.start.character == len("from helper import ")
    assert alpha.range.end.character == len("from helper import ALPHA")
    assert alpha.target_path == str(root / "helper.py")
    assert beta.range.start.character == len("from helper import ALPHA, ")
    assert beta.range.end.character == len("from helper import ALPHA, BETA")
    assert beta.target_path == str(root / "helper.py")


def test_document_links_for_from_import_submodule_targets_submodule(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    _write(pkg / "__init__.py", "")
    _write(pkg / "child.py", "x = 1\n")
    consumer = root / "app.py"
    _write(consumer, "from pkg import child\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    # `from pkg import child` resolves `child` to the submodule file, not to
    # `pkg/__init__.py`, so clicking the alias jumps directly to child.py.
    assert links == (
        DocumentLink(
            range=_range(
                0,
                len("from pkg import "),
                0,
                len("from pkg import child"),
            ),
            target_path=str(pkg / "child.py"),
        ),
    )


def test_document_links_skip_stdlib_and_missing_imports(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "app.py"
    _write(consumer, "import os\nimport nonexistent_xyz\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    # Stdlib and unresolved targets do not get links — the LSP only navigates
    # to workspace targets.
    assert links == ()


def test_document_links_skip_wildcard_imports(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "__all__ = ['x']\nx = 1\n")
    consumer = root / "app.py"
    _write(consumer, "from helper import *\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    # `*` is not a navigable target; skip it.
    assert links == ()


def test_document_links_for_multiline_from_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "A = 1\nB = 2\n")
    consumer = root / "app.py"
    _write(consumer, "from helper import (\n    A,\n    B,\n)\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    assert len(links) == 2
    by_line = {link.range.start.line: link for link in links}
    assert set(by_line) == {1, 2}
    assert by_line[1].range.start.character == 4
    assert by_line[1].range.end.character == 5
    assert by_line[2].range.start.character == 4
    assert by_line[2].range.end.character == 5
    for link in links:
        assert link.target_path == str(root / "helper.py")


def test_document_links_invalid_syntax_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "app.py"
    _write(consumer, "import (\n")

    with WorkspaceSession(root) as session:
        links = session.document_links_for_file(consumer)

    assert links == ()


def test_document_links_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "x = 1\n")
    _write(root / "other.py", "y = 2\n")
    consumer = root / "app.py"
    _write(consumer, "import helper\n")

    with WorkspaceSession(root) as session:
        before = session.document_links_for_file(consumer)
        assert len(before) == 1
        assert before[0].target_path == str(root / "helper.py")

        session.set_overlay(str(consumer), "import other\n")
        after = session.document_links_for_file(consumer)
        assert len(after) == 1
        assert after[0].target_path == str(root / "other.py")


def test_document_links_for_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with (
        WorkspaceSession(root) as session,
        pytest.raises(FileNotFoundError),
    ):
        session.document_links_for_file(root / "absent.py")


def test_language_server_advertises_document_link_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        provider = init["capabilities"]["documentLinkProvider"]
        assert provider == {"resolveProvider": False}
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_document_link_returns_lsp_payload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def greet() -> str:\n    return 'hi'\n")
    consumer = root / "app.py"
    _write(consumer, "from helper import greet\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/documentLink",
            {"textDocument": {"uri": consumer.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == [
        {
            "range": {
                "start": {
                    "line": 0,
                    "character": len("from helper import "),
                },
                "end": {
                    "line": 0,
                    "character": len("from helper import greet"),
                },
            },
            "target": (root / "helper.py").as_uri(),
        }
    ]


def test_language_server_document_link_unparseable_file_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "import (\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/documentLink",
            {"textDocument": {"uri": target.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == []


def test_code_lenses_for_function_with_zero_workspace_references(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def lonely() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        lenses = session.code_lenses_for_file(target)

    assert lenses == (
        CodeLens(
            range=_range(0, len("def "), 0, len("def lonely")),
            title="0 references",
        ),
    )


def test_code_lenses_count_workspace_references_singular(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    _write(root / "app.py", "from helper import greet\n\nprint(greet())\n")

    with WorkspaceSession(root) as session:
        lenses = session.code_lenses_for_file(helper)

    assert len(lenses) == 1
    assert lenses[0].title == "1 reference"
    assert lenses[0].range.start.line == 0
    assert lenses[0].range.start.character == len("def ")
    assert lenses[0].range.end.character == len("def greet")


def test_code_lenses_count_multiple_workspace_references(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    _write(root / "a.py", "from helper import greet\n\nprint(greet())\n")
    _write(root / "b.py", "import helper\n\nprint(helper.greet())\n")

    with WorkspaceSession(root) as session:
        lenses = session.code_lenses_for_file(helper)

    assert len(lenses) == 1
    # `from a import greet` binds the alias and one bare call site; the
    # `import helper; helper.greet()` chain adds one attribute reference.
    assert lenses[0].title.endswith(" references")
    # Loose lower bound — the resolver may or may not count the import alias
    # itself depending on `include_declaration`; we asked for declarations
    # excluded so this is the call-site count only.
    count = int(lenses[0].title.split(" ", 1)[0])
    assert count >= 2


def test_code_lenses_emit_one_lens_per_top_level_def_and_class(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def f() -> int:\n"
        "    return 1\n"
        "\n"
        "async def g() -> int:\n"
        "    return 2\n"
        "\n"
        "class C:\n"
        "    def m(self) -> int:\n"
        "        return 3\n"
        "\n"
        "    X = 1\n",
    )

    with WorkspaceSession(root) as session:
        lenses = session.code_lenses_for_file(target)

    titles = {(lens.range.start.line, lens.title) for lens in lenses}
    # f at line 0, g at line 3, C at line 6 — no lens for method m or class
    # variable X (kind="method" / "class_variable" are excluded).
    assert titles == {(0, "0 references"), (3, "0 references"), (6, "0 references")}


def test_code_lenses_skip_methods_and_nested_classes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "class Outer:\n    class Inner:\n        pass\n\n    def m(self) -> None:\n        pass\n",
    )

    with WorkspaceSession(root) as session:
        lenses = session.code_lenses_for_file(target)

    assert len(lenses) == 1
    assert lenses[0].range.start.line == 0
    assert lenses[0].range.end.character == len("class Outer")


def test_code_lenses_for_decorated_function_use_def_header_line(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "import functools\n\n@functools.cache\ndef cached() -> int:\n    return 1\n",
    )

    with WorkspaceSession(root) as session:
        lenses = session.code_lenses_for_file(target)

    assert len(lenses) == 1
    # The lens covers the bare identifier on the `def` line, not the
    # decorator line — the decorator's `@functools.cache` lineno would
    # collide with `functools` identifier resolution.
    assert lenses[0].range.start.line == 3
    assert lenses[0].range.start.character == len("def ")
    assert lenses[0].range.end.character == len("def cached")


def test_code_lenses_for_invalid_syntax_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")

    with WorkspaceSession(root) as session:
        lenses = session.code_lenses_for_file(target)

    assert lenses == ()


def test_code_lenses_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def first() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        before = session.code_lenses_for_file(target)
        assert len(before) == 1
        assert before[0].range.end.character == len("def first")

        session.set_overlay(
            str(target),
            "def first() -> int:\n    return 1\n\ndef second() -> int:\n    return 2\n",
        )
        after = session.code_lenses_for_file(target)
        assert len(after) == 2
        assert {lens.range.start.line for lens in after} == {0, 3}


def test_code_lenses_for_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with (
        WorkspaceSession(root) as session,
        pytest.raises(FileNotFoundError),
    ):
        session.code_lenses_for_file(root / "absent.py")


def test_language_server_advertises_code_lens_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        provider = init["capabilities"]["codeLensProvider"]
        assert provider == {"resolveProvider": False}
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_code_lens_returns_lsp_payload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    _write(root / "app.py", "from helper import greet\n\nprint(greet())\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/codeLens",
            {"textDocument": {"uri": helper.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == [
        {
            "range": {
                "start": {"line": 0, "character": len("def ")},
                "end": {"line": 0, "character": len("def greet")},
            },
            "command": {"title": "1 reference", "command": ""},
        }
    ]


def test_language_server_code_lens_unparseable_file_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/codeLens",
            {"textDocument": {"uri": target.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == []


def test_inlay_hints_for_local_call_emits_parameter_names(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def greet(first: str, second: int) -> None:\n    pass\n\ngreet('hi', 7)\n",
    )

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    # Line 3 (0-based): `greet('hi', 7)` — args at columns 6 and 12.
    assert hints == (
        InlayHint(
            position=SourcePosition(3, 6),
            label="first:",
            kind="parameter",
            padding_left=False,
            padding_right=True,
        ),
        InlayHint(
            position=SourcePosition(3, 12),
            label="second:",
            kind="parameter",
            padding_left=False,
            padding_right=True,
        ),
    )


def test_inlay_hints_suppress_redundant_when_arg_name_matches_param(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def f(name: str, count: int) -> None:\n    pass\n\nname = 'x'\nf(name, 3)\n",
    )

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    # The first arg `name` matches the parameter name — suppressed.
    assert hints == (
        InlayHint(
            position=SourcePosition(4, 8),
            label="count:",
            kind="parameter",
            padding_left=False,
            padding_right=True,
        ),
    )


def test_inlay_hints_skip_keyword_arguments(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def f(first: str, second: int) -> None:\n    pass\n\nf('hi', second=2)\n",
    )

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    # Only the positional first arg gets a hint; `second=2` is already named.
    assert len(hints) == 1
    assert hints[0].label == "first:"


def test_inlay_hints_resolve_cross_module(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "helper.py",
        "def greet(message: str, times: int) -> None:\n    pass\n",
    )
    consumer = root / "app.py"
    _write(consumer, "from helper import greet\n\ngreet('hi', 3)\n")

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(consumer)

    assert tuple(hint.label for hint in hints) == ("message:", "times:")


def test_inlay_hints_resolve_module_attribute_call(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "helper.py",
        "def greet(message: str, times: int) -> None:\n    pass\n",
    )
    consumer = root / "app.py"
    _write(consumer, "import helper\n\nhelper.greet('hi', 3)\n")

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(consumer)

    assert tuple(hint.label for hint in hints) == ("message:", "times:")


def test_inlay_hints_class_construction_strips_self(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "class Point:\n"
        "    def __init__(self, x: int, y: int) -> None:\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "\n"
        "Point(1, 2)\n",
    )

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    # `self` is stripped from class `__init__` signatures by
    # `_lookup_callable_signature`.
    assert tuple(hint.label for hint in hints) == ("x:", "y:")


def test_inlay_hints_stop_at_starred_call_arg(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def f(a: int, b: int, c: int) -> None:\n    pass\n\nitems = (1, 2)\nf(0, *items)\n",
    )

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    # Only the first arg gets a hint; *items consumes unknown slots, so the
    # walker stops there.
    assert tuple(hint.label for hint in hints) == ("a:",)


def test_inlay_hints_stop_at_varargs_parameter(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def f(first: int, *rest: int) -> None:\n    pass\n\nf(1, 2, 3, 4)\n",
    )

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    # Only the bound `first` gets a hint; the rest is absorbed by *rest.
    assert tuple(hint.label for hint in hints) == ("first:",)


def test_inlay_hints_skip_method_attribute_call(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "class C:\n    def m(self, x: int) -> None:\n        pass\n\nobj = C()\nobj.m(7)\n",
    )

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    # `obj.m(...)` is an instance-attribute call — the resolver only handles
    # `Name.attr` where `Name` is a workspace module/class, not an instance.
    # `C()` is a class construction with no positional args, so no hints.
    assert hints == ()


def test_inlay_hints_skip_stdlib_target(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "print('hello', 1)\n")

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    assert hints == ()


def test_inlay_hints_for_unparseable_file_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")

    with WorkspaceSession(root) as session:
        hints = session.inlay_hints_for_file(target)

    assert hints == ()


def test_inlay_hints_for_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with (
        WorkspaceSession(root) as session,
        pytest.raises(FileNotFoundError),
    ):
        session.inlay_hints_for_file(root / "absent.py")


def test_inlay_hints_range_filter_excludes_outside_calls(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def f(first: int, second: int) -> None:\n    pass\n\nf(1, 2)\nf(3, 4)\n",
    )

    with WorkspaceSession(root) as session:
        # Range covering only the second call site (line 4, 0-based).
        hints = session.inlay_hints_for_file(
            target, start_line=4, start_character=0, end_line=5, end_character=0
        )

    assert tuple((h.position.line, h.label) for h in hints) == (
        (4, "first:"),
        (4, "second:"),
    )


def test_inlay_hints_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def f(x: int) -> None:\n    pass\n\nf(1)\n")

    with WorkspaceSession(root) as session:
        before = session.inlay_hints_for_file(target)
        assert tuple(h.label for h in before) == ("x:",)

        session.set_overlay(
            str(target),
            "def f(x: int, y: int) -> None:\n    pass\n\nf(1, 2)\n",
        )
        after = session.inlay_hints_for_file(target)
        assert tuple(h.label for h in after) == ("x:", "y:")


def test_language_server_advertises_inlay_hint_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        provider = init["capabilities"]["inlayHintProvider"]
        assert provider == {"resolveProvider": False}
        assert init["serverInfo"]["version"] == _package_version()
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_inlay_hint_returns_lsp_payload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def greet(message: str, times: int) -> None:\n    pass\n\ngreet('hi', 3)\n",
    )

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/inlayHint",
            {
                "textDocument": {"uri": target.as_uri()},
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 4, "character": 0},
                },
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == [
        {
            "position": {"line": 3, "character": 6},
            "label": "message:",
            "kind": 2,
            "paddingLeft": False,
            "paddingRight": True,
        },
        {
            "position": {"line": 3, "character": 12},
            "label": "times:",
            "kind": 2,
            "paddingLeft": False,
            "paddingRight": True,
        },
    ]


def test_language_server_inlay_hint_unparseable_file_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/inlayHint",
            {
                "textDocument": {"uri": target.as_uri()},
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 0},
                },
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == []


def test_type_definitions_at_variable_with_local_class(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "class Foo:\n    pass\n\nx: Foo = Foo()\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == (
        TypeDefinitionLocation(
            path=str(target), range=_range(0, len("class "), 0, len("class Foo"))
        ),
    )


def test_type_definitions_at_variable_resolves_through_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "class Foo:\n    pass\n")
    target = root / "app.py"
    _write(target, "from helper import Foo\n\nx: Foo = Foo()\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == (
        TypeDefinitionLocation(
            path=str(root / "helper.py"),
            range=_range(0, len("class "), 0, len("class Foo")),
        ),
    )


def test_type_definitions_at_unwraps_string_forward_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "class Foo:\n    pass\n\nx: 'Foo' = Foo()\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == (
        TypeDefinitionLocation(
            path=str(target), range=_range(0, len("class "), 0, len("class Foo"))
        ),
    )


def test_type_definitions_at_walks_generic_subscript(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "class Foo:\n    pass\n\nx: list[Foo] = []\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == (
        TypeDefinitionLocation(
            path=str(target), range=_range(0, len("class "), 0, len("class Foo"))
        ),
    )


def test_type_definitions_at_union_returns_both_workspace_types(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "helper.py",
        "class Foo:\n    pass\n\nclass Bar:\n    pass\n",
    )
    target = root / "app.py"
    _write(
        target,
        "from helper import Foo, Bar\n\nx: Foo | Bar = Foo()\n",
    )
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == (
        TypeDefinitionLocation(
            path=str(root / "helper.py"),
            range=_range(0, len("class "), 0, len("class Foo")),
        ),
        TypeDefinitionLocation(
            path=str(root / "helper.py"),
            range=_range(3, len("class "), 3, len("class Bar")),
        ),
    )


def test_type_definitions_at_attribute_resolves_through_module_alias(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "class Foo:\n    pass\n")
    target = root / "app.py"
    _write(
        target,
        "import helper\n\nx: helper.Foo = helper.Foo()\n",
    )
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == (
        TypeDefinitionLocation(
            path=str(root / "helper.py"),
            range=_range(0, len("class "), 0, len("class Foo")),
        ),
    )


def test_type_definitions_at_skips_stdlib_type(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "x: int = 1\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == ()


def test_type_definitions_at_function_return_annotation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(
        target,
        "class Foo:\n    pass\n\ndef make() -> Foo:\n    return Foo()\n",
    )
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "make"))
    assert result == (
        TypeDefinitionLocation(
            path=str(target), range=_range(0, len("class "), 0, len("class Foo"))
        ),
    )


def test_type_definitions_at_function_no_return_annotation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "def make():\n    return 1\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "make"))
    assert result == ()


def test_type_definitions_at_class_returns_self_location(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "class Foo:\n    pass\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "Foo"))
    assert result == (
        TypeDefinitionLocation(
            path=str(target), range=_range(0, len("class "), 0, len("class Foo"))
        ),
    )


def test_type_definitions_at_variable_without_annotation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "x = 1\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == ()


def test_symbol_at_unknown_identifier_returns_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "x: int = 1\n")
    with WorkspaceSession(root) as session:
        assert session.symbol_at(target, SourcePosition(0, len("x: int "))) is None


def test_type_definitions_at_import_alias_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "class Foo:\n    pass\n")
    target = root / "app.py"
    _write(target, "import helper\n")
    with WorkspaceSession(root) as session:
        symbol_id = session._local_symbol_at(target, SourcePosition(0, len("import ")))
        assert symbol_id is not None
        result = session.type_definitions_at(symbol_id)
    assert result == ()


def test_type_definitions_at_deduplicates_repeated_type_refs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "class Foo:\n    pass\n")
    target = root / "app.py"
    _write(
        target,
        "from helper import Foo\n\nx: dict[Foo, Foo] = {}\n",
    )
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == (
        TypeDefinitionLocation(
            path=str(root / "helper.py"),
            range=_range(0, len("class "), 0, len("class Foo")),
        ),
    )


def test_type_definitions_at_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    symbol_id = SymbolId(
        str(root / "missing.py"),
        "module",
        "x",
        _range(0, 0, 0, 1),
    )
    with WorkspaceSession(root) as session, pytest.raises(FileNotFoundError):
        session.type_definitions_at(symbol_id)


def test_type_definitions_at_invalid_annotation_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    # Annotation text is whatever `ast.unparse` produces; we only need to
    # cover the "annotation re-parses cleanly but contains no resolvable
    # workspace name" path.
    _write(target, "x: object = object()\n")
    with WorkspaceSession(root) as session:
        result = session.type_definitions_at(_symbol_for_name(session, target, "x"))
    assert result == ()


def test_language_server_type_definition_returns_lsp_location(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "class Foo:\n    pass\n")
    target = root / "app.py"
    _write(target, "from helper import Foo\n\nx: Foo = Foo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/typeDefinition",
            {
                "textDocument": {"uri": target.as_uri()},
                "position": {"line": 2, "character": 0},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == [
        {
            "uri": (root / "helper.py").as_uri(),
            "range": {
                "start": {"line": 0, "character": len("class ")},
                "end": {"line": 0, "character": len("class Foo")},
            },
        }
    ]


def test_language_server_type_definition_position_off_identifier_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "app.py"
    _write(target, "class Foo:\n    pass\n\nx: Foo = Foo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/typeDefinition",
            {
                "textDocument": {"uri": target.as_uri()},
                # Line 3 is "x: Foo = Foo()"; column 7 is the "=" sign with
                # whitespace on both sides — not on any identifier.
                "position": {"line": 3, "character": 7},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result == []


def test_language_server_advertises_type_definition_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        result = server._handle_request("initialize", {"rootUri": root.as_uri()})
    finally:
        if server._session is not None:
            server._session.close()
    assert result["capabilities"]["typeDefinitionProvider"] is True


# ---------------------------------------------------------------------------
# Call hierarchy
# ---------------------------------------------------------------------------


def test_prepare_call_hierarchy_top_level_function_at_call_site(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(app, "from helper import greet\n\nprint(greet())\n")

    with WorkspaceSession(root) as session:
        # Cursor on `greet` inside `print(greet())` on line 2 (0-based).
        items = session.prepare_call_hierarchy(app, 2, len("print("))

    assert len(items) == 1
    item = items[0]
    assert item.name == "greet"
    assert item.kind == "function"
    assert item.path == str(helper)
    assert item.qualified_name == "greet"
    assert item.detail == "helper"
    # selectionRange is the bare identifier on the def header line.
    assert item.selection_range.start.line == 0
    assert item.selection_range.start.character == len("def ")
    assert item.selection_range.end.line == 0
    assert item.selection_range.end.character == len("def greet")
    # range covers the whole def block (header through body's last line).
    assert item.range.start.line == 0
    assert item.range.start.character == 0
    assert item.range.end.line == 1


def test_prepare_call_hierarchy_on_class_returns_class_item(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "class Widget:\n    pass\n")
    app = root / "app.py"
    _write(app, "from helper import Widget\n\nWidget()\n")

    with WorkspaceSession(root) as session:
        # Cursor on the `Widget()` call on line 2.
        items = session.prepare_call_hierarchy(app, 2, 0)

    assert len(items) == 1
    item = items[0]
    assert item.name == "Widget"
    assert item.kind == "class"
    assert item.qualified_name == "Widget"
    assert item.path == str(helper)


def test_prepare_call_hierarchy_off_identifier_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "x = 1\n")
    with WorkspaceSession(root) as session:
        items = session.prepare_call_hierarchy(app, 0, 1)  # the "=" sign
    assert items == ()


def test_prepare_call_hierarchy_on_variable_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "x: int = 1\nprint(x)\n")
    with WorkspaceSession(root) as session:
        # Cursor on `x` in `print(x)` — a variable, not a callable.
        items = session.prepare_call_hierarchy(app, 1, len("print("))
    assert items == ()


def test_prepare_call_hierarchy_on_stdlib_target_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "import json\n\njson.dumps({})\n")
    with WorkspaceSession(root) as session:
        items = session.prepare_call_hierarchy(app, 2, 0)  # `json`
    assert items == ()


def test_prepare_call_hierarchy_decorated_function_range_includes_decorator(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(
        helper,
        "import functools\n\n@functools.cache\ndef cached() -> int:\n    return 1\n",
    )
    app = root / "app.py"
    _write(app, "from helper import cached\n\nprint(cached())\n")

    with WorkspaceSession(root) as session:
        items = session.prepare_call_hierarchy(app, 2, len("print("))

    assert len(items) == 1
    item = items[0]
    # Decorator is on line 2 (0-based); range starts there.
    assert item.range.start.line == 2
    # selectionRange is the bare-name span on the `def` line (line 3).
    assert item.selection_range.start.line == 3
    assert item.selection_range.start.character == len("def ")
    assert item.selection_range.end.character == len("def cached")


def test_call_hierarchy_incoming_calls_groups_per_caller(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(
        app,
        "from helper import greet\n"
        "\n"
        "def caller_one() -> str:\n"
        "    return greet()\n"
        "\n"
        "def caller_two() -> str:\n"
        "    return greet() + greet()\n",
    )

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_incoming_calls(helper, "greet")

    assert len(calls) == 2
    by_caller = {call.caller.qualified_name: call for call in calls}
    assert set(by_caller) == {"caller_one", "caller_two"}
    assert len(by_caller["caller_one"].call_sites) == 1
    assert len(by_caller["caller_two"].call_sites) == 2
    # The caller items point at the *consumer* file, not the helper.
    assert by_caller["caller_one"].caller.path == str(app)


def test_call_hierarchy_incoming_calls_inside_method_attributes_to_method(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(
        app,
        "from helper import greet\n"
        "\n"
        "class Caller:\n"
        "    def run(self) -> str:\n"
        "        return greet()\n",
    )

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_incoming_calls(helper, "greet")

    assert len(calls) == 1
    assert calls[0].caller.qualified_name == "Caller.run"
    assert calls[0].caller.kind == "method"


def test_call_hierarchy_incoming_calls_inside_nested_function_bubbles_to_outer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(
        app,
        "from helper import greet\n"
        "\n"
        "def outer() -> str:\n"
        "    def inner() -> str:\n"
        "        return greet()\n"
        "    return inner()\n",
    )

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_incoming_calls(helper, "greet")

    # `inner` is not in the symbol table; the call is attributed to `outer`.
    assert len(calls) == 1
    assert calls[0].caller.qualified_name == "outer"


def test_call_hierarchy_incoming_calls_skips_module_top_level_references(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(app, "from helper import greet\n\nprint(greet())\n")

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_incoming_calls(helper, "greet")

    # The call site at module top level has no enclosing def/class, so it is
    # dropped — there is no `CallHierarchyItem` to attribute it to.
    assert calls == ()


def test_call_hierarchy_incoming_calls_non_workspace_target_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "x = 1\n")
    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_incoming_calls(app, "missing_symbol")
    assert calls == ()


def test_call_hierarchy_outgoing_calls_resolves_bare_and_module_attr_calls(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(
        helper,
        "def alpha() -> int:\n    return 1\n\ndef beta() -> int:\n    return 2\n",
    )
    app = root / "app.py"
    _write(
        app,
        "import helper\n"
        "from helper import alpha\n"
        "\n"
        "def driver() -> int:\n"
        "    return alpha() + helper.beta()\n",
    )

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_outgoing_calls(app, "driver")

    by_callee = {call.callee.qualified_name: call for call in calls}
    assert set(by_callee) == {"alpha", "beta"}
    assert by_callee["alpha"].callee.path == str(helper)
    assert by_callee["beta"].callee.path == str(helper)
    # Each callee is called once from `driver`.
    assert len(by_callee["alpha"].call_sites) == 1
    assert len(by_callee["beta"].call_sites) == 1
    # The bare `alpha()` call site spans just the identifier `alpha`.
    alpha_site = by_callee["alpha"].call_sites[0]
    assert alpha_site.range.start.line == 4
    assert alpha_site.range.end.character - alpha_site.range.start.character == len("alpha")
    # The attribute `helper.beta()` reports only the rightmost-attr span.
    beta_site = by_callee["beta"].call_sites[0]
    assert beta_site.range.end.character - beta_site.range.start.character == len("beta")


def test_call_hierarchy_outgoing_calls_skips_nested_function_calls(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def alpha() -> int:\n    return 1\n")
    app = root / "app.py"
    _write(
        app,
        "from helper import alpha\n"
        "\n"
        "def outer() -> int:\n"
        "    def inner() -> int:\n"
        "        return alpha()\n"
        "    return inner()\n",
    )

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_outgoing_calls(app, "outer")

    # The `alpha()` call lives inside the nested `inner` function, which has
    # its own outgoing-call list; `outer`'s outgoing calls only include
    # `inner()`. `inner` is not in the symbol table, so it is also dropped.
    assert calls == ()


def test_call_hierarchy_outgoing_calls_aggregates_repeated_call_sites(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def alpha() -> int:\n    return 1\n")
    app = root / "app.py"
    _write(
        app,
        "from helper import alpha\n"
        "\n"
        "def driver() -> int:\n"
        "    return alpha() + alpha() + alpha()\n",
    )

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_outgoing_calls(app, "driver")

    assert len(calls) == 1
    assert calls[0].callee.qualified_name == "alpha"
    assert len(calls[0].call_sites) == 3
    # Call sites are emitted in document order.
    starts = [site.range.start.character for site in calls[0].call_sites]
    assert starts == sorted(starts)


def test_call_hierarchy_outgoing_calls_skips_stdlib_and_unresolvable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(
        app,
        "import json\n\ndef driver() -> None:\n    print(json.dumps({}))\n",
    )

    with WorkspaceSession(root) as session:
        calls = session.call_hierarchy_outgoing_calls(app, "driver")

    # `print` is a builtin, `json.dumps` is stdlib — neither contributes a
    # workspace callee, so the result is empty.
    assert calls == ()


def test_call_hierarchy_outgoing_calls_on_unparseable_file_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    broken = root / "broken.py"
    _write(broken, "def (\n")
    with WorkspaceSession(root) as session:
        assert session.call_hierarchy_outgoing_calls(broken, "anything") == ()


def test_call_hierarchy_outgoing_calls_unknown_qname_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "def driver() -> int:\n    return 1\n")
    with WorkspaceSession(root) as session:
        assert session.call_hierarchy_outgoing_calls(app, "missing") == ()


def test_call_hierarchy_methods_for_missing_file_raise_filenotfound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with WorkspaceSession(root) as session:
        with pytest.raises(FileNotFoundError):
            session.prepare_call_hierarchy(root / "absent.py", 0, 0)
        with pytest.raises(FileNotFoundError):
            session.call_hierarchy_incoming_calls(root / "absent.py", "x")
        with pytest.raises(FileNotFoundError):
            session.call_hierarchy_outgoing_calls(root / "absent.py", "x")


def test_call_hierarchy_outgoing_overlay_sees_edit(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def alpha() -> int:\n    return 1\n")
    app = root / "app.py"
    _write(app, "from helper import alpha\n\ndef driver() -> int:\n    return 0\n")

    with WorkspaceSession(root) as session:
        before = session.call_hierarchy_outgoing_calls(app, "driver")
        assert before == ()
        session.set_overlay(
            str(app),
            "from helper import alpha\n\ndef driver() -> int:\n    return alpha()\n",
        )
        after = session.call_hierarchy_outgoing_calls(app, "driver")
        assert len(after) == 1
        assert after[0].callee.qualified_name == "alpha"


def test_language_server_advertises_call_hierarchy_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
    finally:
        if server._session is not None:
            server._session.close()
    assert init["capabilities"]["callHierarchyProvider"] is True


def test_language_server_prepare_call_hierarchy_returns_lsp_payload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(app, "from helper import greet\n\nprint(greet())\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 2, "character": len("print(")},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    assert item["name"] == "greet"
    assert item["kind"] == _LSP_SYMBOL_KIND_FUNCTION
    assert item["uri"] == helper.as_uri()
    assert item["selectionRange"] == {
        "start": {"line": 0, "character": len("def ")},
        "end": {"line": 0, "character": len("def greet")},
    }
    assert item["data"] == {"path": str(helper), "qualified_name": "greet"}


def test_language_server_prepare_call_hierarchy_off_identifier_returns_null(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 0, "character": 1},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_language_server_call_hierarchy_incoming_outgoing_roundtrip(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(
        app,
        "from helper import greet\n\ndef caller() -> str:\n    return greet()\n",
    )

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        prepared = server._handle_request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 3, "character": len("    return ")},
            },
        )
        assert prepared is not None and len(prepared) == 1
        incoming = server._handle_request("callHierarchy/incomingCalls", {"item": prepared[0]})
        prepared_caller = server._handle_request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 2, "character": len("def ")},
            },
        )
        assert prepared_caller is not None and len(prepared_caller) == 1
        outgoing = server._handle_request(
            "callHierarchy/outgoingCalls", {"item": prepared_caller[0]}
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert isinstance(incoming, list)
    assert len(incoming) == 1
    assert incoming[0]["from"]["name"] == "caller"
    assert incoming[0]["from"]["uri"] == app.as_uri()
    assert len(incoming[0]["fromRanges"]) == 1

    assert isinstance(outgoing, list)
    assert len(outgoing) == 1
    assert outgoing[0]["to"]["name"] == "greet"
    assert outgoing[0]["to"]["uri"] == helper.as_uri()


def test_language_server_call_hierarchy_incoming_missing_data_returns_null(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "callHierarchy/incomingCalls",
            {"item": {"name": "foo", "kind": 12, "uri": (root / "x.py").as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_call_hierarchy_dataclass_exports_are_re_exported_from_pyinc_tools() -> None:
    import pyinc_tools

    for name in (
        "CallHierarchyItem",
        "CallHierarchyCallSite",
        "CallHierarchyIncomingCall",
        "CallHierarchyOutgoingCall",
        "CallHierarchyItemKind",
    ):
        assert hasattr(pyinc_tools, name), name
    # Sanity-check we can construct a frozen item.
    item = CallHierarchyItem(
        name="f",
        kind="function",
        path="/tmp/x.py",
        qualified_name="f",
        detail=None,
        range=_range(0, 0, 1, 0),
        selection_range=_range(0, 4, 0, 5),
    )
    site = CallHierarchyCallSite(range=_range(0, 0, 0, 1))
    inc = CallHierarchyIncomingCall(caller=item, call_sites=(site,))
    out = CallHierarchyOutgoingCall(callee=item, call_sites=(site,))
    assert inc.caller is item
    assert out.callee is item


# ---------------------------------------------------------------------------
# Type hierarchy
# ---------------------------------------------------------------------------


def test_prepare_type_hierarchy_on_class_returns_class_item(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    app = root / "app.py"
    _write(app, "from base import Base\n\nclass Child(Base):\n    pass\n")

    with WorkspaceSession(root) as session:
        # Cursor on `Base` inside `class Child(Base)`.
        items = session.prepare_type_hierarchy(app, 2, len("class Child("))

    assert len(items) == 1
    item = items[0]
    assert item.name == "Base"
    assert item.kind == "class"
    assert item.qualified_name == "Base"
    assert item.path == str(base)
    assert item.detail == "base"
    # selectionRange spans the bare class name on the header line.
    assert item.selection_range.start.line == 0
    assert item.selection_range.start.character == len("class ")
    assert item.selection_range.end.character == len("class Base")
    # range covers the whole class block.
    assert item.range.start.line == 0
    assert item.range.end.line == 1


def test_prepare_type_hierarchy_on_function_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    helper = root / "helper.py"
    _write(helper, "def greet() -> str:\n    return 'hi'\n")
    app = root / "app.py"
    _write(app, "from helper import greet\n\nprint(greet())\n")

    with WorkspaceSession(root) as session:
        items = session.prepare_type_hierarchy(app, 2, len("print("))

    assert items == ()


def test_prepare_type_hierarchy_off_identifier_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "x = 1\n")
    with WorkspaceSession(root) as session:
        items = session.prepare_type_hierarchy(app, 0, 1)  # the "=" sign
    assert items == ()


def test_prepare_type_hierarchy_on_stdlib_target_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "from collections import OrderedDict\n\nOrderedDict()\n")
    with WorkspaceSession(root) as session:
        # Cursor on `OrderedDict` at the call site — its definition lives in
        # stdlib so the LSP refuses to surface an item.
        items = session.prepare_type_hierarchy(app, 2, 0)
    assert items == ()


def test_prepare_type_hierarchy_decorated_class_range_includes_decorator(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(
        base,
        "from dataclasses import dataclass\n\n@dataclass\nclass Decorated:\n    pass\n",
    )
    app = root / "app.py"
    _write(app, "from base import Decorated\n\nclass Child(Decorated):\n    pass\n")

    with WorkspaceSession(root) as session:
        items = session.prepare_type_hierarchy(app, 2, len("class Child("))

    assert len(items) == 1
    item = items[0]
    # Decorator is on line 2 (0-based) in base.py.
    assert item.range.start.line == 2
    # selectionRange is the bare-name span on the class header line.
    assert item.selection_range.start.line == 3
    assert item.selection_range.start.character == len("class ")
    assert item.selection_range.end.character == len("class Decorated")


def test_type_hierarchy_supertypes_resolves_single_base(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    app = root / "app.py"
    _write(app, "from base import Base\n\nclass Child(Base):\n    pass\n")

    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(app, "Child")

    assert len(supers) == 1
    assert supers[0].name == "Base"
    assert supers[0].qualified_name == "Base"
    assert supers[0].path == str(base)


def test_type_hierarchy_supertypes_resolves_multiple_bases_sorted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Zebra:\n    pass\n\nclass Antelope:\n    pass\n")
    app = root / "app.py"
    _write(
        app,
        "from base import Zebra, Antelope\n\nclass Hybrid(Zebra, Antelope):\n    pass\n",
    )

    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(app, "Hybrid")

    # Sorted by (path, qualified_name): both bases live in base.py, so the
    # result is alphabetical on qualified_name.
    assert tuple(s.qualified_name for s in supers) == ("Antelope", "Zebra")


def test_type_hierarchy_supertypes_unwraps_subscript_bases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Container:\n    pass\n")
    app = root / "app.py"
    _write(
        app,
        "from base import Container\n"
        "from typing import Generic, TypeVar\n"
        "\n"
        "T = TypeVar('T')\n"
        "\n"
        "class Mine(Container, Generic[T]):\n"
        "    pass\n",
    )

    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(app, "Mine")

    # `Generic[T]` is stdlib; `Container` is the only workspace base. The
    # subscript unwrap rule means `Base[T]` would still resolve — covered
    # by the next test.
    assert tuple(s.qualified_name for s in supers) == ("Container",)


def test_type_hierarchy_supertypes_generic_workspace_base_via_subscript(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Box:\n    pass\n")
    app = root / "app.py"
    _write(
        app,
        "from base import Box\n"
        "from typing import TypeVar\n"
        "\n"
        "T = TypeVar('T')\n"
        "\n"
        "class IntBox(Box[T]):\n"
        "    pass\n",
    )

    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(app, "IntBox")

    # `Box[T]` unwraps to `Box`, which resolves to the workspace class.
    assert tuple(s.qualified_name for s in supers) == ("Box",)


def test_type_hierarchy_supertypes_resolves_attribute_base(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    app = root / "app.py"
    _write(app, "import base\n\nclass Child(base.Base):\n    pass\n")

    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(app, "Child")

    # `base.Base` — LHS is the bare `base` import alias, resolves to the
    # workspace `base` module; `.Base` resolves to the workspace class.
    assert len(supers) == 1
    assert supers[0].qualified_name == "Base"


def test_type_hierarchy_supertypes_skips_stdlib_and_installed_bases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(
        app,
        "from collections import OrderedDict\n\nclass Mine(OrderedDict):\n    pass\n",
    )

    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(app, "Mine")

    # OrderedDict is stdlib — no workspace item to surface.
    assert supers == ()


def test_type_hierarchy_supertypes_proven_deep_attribute_chain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    _write(pkg / "__init__.py", "")
    _write(pkg / "inner.py", "class Inner:\n    pass\n")
    app = root / "app.py"
    _write(app, "import pkg.inner\n\nclass Child(pkg.inner.Inner):\n    pass\n")

    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(app, "Child")

    assert [item.qualified_name for item in supers] == ["Inner"]


def test_type_hierarchy_supertypes_on_non_class_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def greet() -> str:\n    return 'hi'\n")
    with WorkspaceSession(root) as session:
        supers = session.type_hierarchy_supertypes(target, "greet")
    assert supers == ()


def test_type_hierarchy_subtypes_finds_workspace_subclasses(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    a = root / "a.py"
    _write(a, "from base import Base\n\nclass A(Base):\n    pass\n")
    b = root / "b.py"
    _write(b, "import base\n\nclass B(base.Base):\n    pass\n")
    unrelated = root / "unrelated.py"
    _write(unrelated, "class Standalone:\n    pass\n")

    with WorkspaceSession(root) as session:
        subs = session.type_hierarchy_subtypes(base, "Base")

    assert tuple((s.qualified_name, s.path) for s in subs) == (
        ("A", str(a)),
        ("B", str(b)),
    )


def test_type_hierarchy_subtypes_includes_nested_classes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    app = root / "app.py"
    _write(
        app,
        "from base import Base\n\nclass Outer:\n    class Inner(Base):\n        pass\n",
    )

    with WorkspaceSession(root) as session:
        subs = session.type_hierarchy_subtypes(base, "Base")

    assert tuple(s.qualified_name for s in subs) == ("Outer.Inner",)


def test_type_hierarchy_subtypes_excludes_self(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(
        base,
        "class Base:\n    pass\n\nclass Child(Base):\n    pass\n",
    )
    with WorkspaceSession(root) as session:
        subs = session.type_hierarchy_subtypes(base, "Base")

    # Base itself shouldn't appear among its own subtypes even though it's
    # in the same file as Child.
    assert tuple(s.qualified_name for s in subs) == ("Child",)


def test_type_hierarchy_subtypes_on_non_class_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def greet() -> str:\n    return 'hi'\n")
    with WorkspaceSession(root) as session:
        subs = session.type_hierarchy_subtypes(target, "greet")
    assert subs == ()


def test_type_hierarchy_subtypes_no_subclasses_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    with WorkspaceSession(root) as session:
        subs = session.type_hierarchy_subtypes(base, "Base")
    assert subs == ()


def test_language_server_advertises_type_hierarchy_capability(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
    finally:
        if server._session is not None:
            server._session.close()
    assert init["capabilities"]["typeHierarchyProvider"] is True


def test_language_server_prepare_type_hierarchy_returns_lsp_payload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    app = root / "app.py"
    _write(app, "from base import Base\n\nclass Child(Base):\n    pass\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/prepareTypeHierarchy",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 2, "character": len("class Child(")},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    assert item["name"] == "Base"
    assert item["kind"] == _LSP_SYMBOL_KIND_CLASS
    assert item["uri"] == base.as_uri()
    assert item["selectionRange"] == {
        "start": {"line": 0, "character": len("class ")},
        "end": {"line": 0, "character": len("class Base")},
    }
    assert item["data"] == {"path": str(base), "qualified_name": "Base"}


def test_language_server_prepare_type_hierarchy_off_identifier_returns_null(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, "x = 1\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/prepareTypeHierarchy",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 0, "character": 1},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_language_server_type_hierarchy_supertypes_subtypes_roundtrip(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    base = root / "base.py"
    _write(base, "class Base:\n    pass\n")
    app = root / "app.py"
    _write(app, "from base import Base\n\nclass Child(Base):\n    pass\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # Prepare on `Child` (the declaration itself).
        prepared_child = server._handle_request(
            "textDocument/prepareTypeHierarchy",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 2, "character": len("class ")},
            },
        )
        assert prepared_child is not None and len(prepared_child) == 1
        supertypes = server._handle_request("typeHierarchy/supertypes", {"item": prepared_child[0]})
        # Prepare on `Base` (declaration site in base.py).
        prepared_base = server._handle_request(
            "textDocument/prepareTypeHierarchy",
            {
                "textDocument": {"uri": base.as_uri()},
                "position": {"line": 0, "character": len("class ")},
            },
        )
        assert prepared_base is not None and len(prepared_base) == 1
        subtypes = server._handle_request("typeHierarchy/subtypes", {"item": prepared_base[0]})
    finally:
        if server._session is not None:
            server._session.close()

    assert isinstance(supertypes, list)
    assert len(supertypes) == 1
    assert supertypes[0]["name"] == "Base"
    assert supertypes[0]["uri"] == base.as_uri()
    assert supertypes[0]["kind"] == _LSP_SYMBOL_KIND_CLASS

    assert isinstance(subtypes, list)
    assert len(subtypes) == 1
    assert subtypes[0]["name"] == "Child"
    assert subtypes[0]["uri"] == app.as_uri()
    assert subtypes[0]["kind"] == _LSP_SYMBOL_KIND_CLASS


def test_language_server_type_hierarchy_supertypes_missing_data_returns_null(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "typeHierarchy/supertypes",
            {
                "item": {
                    "name": "Foo",
                    "kind": _LSP_SYMBOL_KIND_CLASS,
                    "uri": (root / "x.py").as_uri(),
                }
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_type_hierarchy_dataclass_exports_are_re_exported_from_pyinc_tools() -> None:
    import pyinc_tools

    for name in ("TypeHierarchyItem", "TypeHierarchyItemKind"):
        assert hasattr(pyinc_tools, name), name
    # Sanity-check we can construct a frozen item.
    item = TypeHierarchyItem(
        name="C",
        kind="class",
        path="/tmp/x.py",
        qualified_name="C",
        detail=None,
        range=_range(0, 0, 1, 0),
        selection_range=_range(0, len("class "), 0, len("class C")),
    )
    assert item.kind == "class"
    assert item.qualified_name == "C"


def test_file_deletion_edit_re_exported_from_pyinc_tools() -> None:
    # Regression: `FileDeletionEdit` was added to `pyinc_tools.session` but
    # not added to `pyinc_tools.__init__`'s re-export list in the original
    # PR; ensure it ships on the public surface.
    import pyinc_tools

    assert hasattr(pyinc_tools, "FileDeletionEdit")


def test_code_action_types_re_exported_from_pyinc_tools() -> None:
    import pyinc_tools

    assert pyinc_tools.CodeAction is CodeAction
    assert pyinc_tools.CodeActionEdit is CodeActionEdit
    assert hasattr(pyinc_tools, "CodeActionKind")


def test_semantic_tokens_emits_declarations_for_function_and_parameters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def greet(first: str, second: int) -> None:\n    pass\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(target)

    assert tokens == (
        SemanticToken(
            range=_range(0, 4, 0, 9),
            token_type="function",
            token_modifiers=("declaration",),
        ),
        SemanticToken(
            range=_range(0, 10, 0, 15),
            token_type="parameter",
            token_modifiers=("declaration",),
        ),
        SemanticToken(
            range=_range(0, 22, 0, 28),
            token_type="parameter",
            token_modifiers=("declaration",),
        ),
    )


def test_semantic_tokens_method_inside_class_classified_as_method(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "class Foo:\n    def method(self, x: int) -> None:\n        pass\n",
    )
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(target)

    types = tuple((t.range.start.line, t.range.start.character, t.token_type) for t in tokens)
    # Class name + method name + self + x
    assert types == (
        (0, 6, "class"),
        (1, 8, "method"),
        (1, 15, "parameter"),
        (1, 21, "parameter"),
    )


def test_semantic_tokens_async_def_carries_async_modifier(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "async def fetch(url: str) -> None:\n    pass\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(target)

    fn_token = next(t for t in tokens if t.token_type == "function")
    assert fn_token.range.start.line == 0
    assert fn_token.token_modifiers == ("declaration", "async")


def test_semantic_tokens_use_site_classified_via_symbol_table(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "import json\n"
        "\n"
        "def greet() -> None:\n"
        "    pass\n"
        "\n"
        "class Foo:\n"
        "    pass\n"
        "\n"
        "value = 1\n"
        "greet()\n"
        "Foo()\n"
        "json.dumps({})\n"
        "value\n",
    )
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(target)

    use_tokens = [t for t in tokens if t.token_modifiers == ()]
    assert {(t.range.start.line, t.token_type) for t in use_tokens} == {
        (9, "function"),
        (10, "class"),
        (11, "namespace"),
        (12, "variable"),
    }


def test_semantic_tokens_decorator_name_resolved_via_symbol_table(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def my_decorator(fn):\n    return fn\n\n@my_decorator\ndef target():\n    pass\n",
    )
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(target)

    # The decorator-name token has no `declaration` modifier (it's a use).
    decorator_uses = [
        t
        for t in tokens
        if t.range.start.line == 3 and t.token_type == "function" and t.token_modifiers == ()
    ]
    assert len(decorator_uses) == 1
    assert decorator_uses[0].range.start.character == 1
    assert decorator_uses[0].range.end.character - decorator_uses[0].range.start.character == len(
        "my_decorator"
    )


def test_semantic_tokens_base_class_resolves_to_class_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "class Base:\n    pass\n\nclass Derived(Base):\n    pass\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(target)

    base_uses = [t for t in tokens if t.range.start.line == 3 and t.token_modifiers == ()]
    assert base_uses == [
        SemanticToken(
            range=_range(3, 14, 3, 18),
            token_type="class",
            token_modifiers=(),
        ),
    ]


def test_semantic_tokens_unparseable_file_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(target)
    assert tokens == ()


def test_semantic_tokens_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with WorkspaceSession(root) as session, pytest.raises(FileNotFoundError):
        session.semantic_tokens_for_file(root / "nope.py")


def test_semantic_tokens_from_import_alias_use_resolves_to_target_kind(
    tmp_path: Path,
) -> None:
    """`from helper import greet` makes ``greet`` a `from_import_alias` entry in
    the symbol table; following the single cross-module hop classifies the use
    site as the function it actually names.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def greet() -> None:\n    pass\n")
    consumer = root / "app.py"
    _write(consumer, "from helper import greet\n\ngreet()\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(consumer)
    uses = [t for t in tokens if t.token_modifiers == ()]
    assert [(t.range.start.line, t.token_type) for t in uses] == [(2, "function")]


def test_semantic_tokens_from_import_alias_resolves_class_kind(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "models.py", "class Box:\n    pass\n")
    consumer = root / "app.py"
    _write(consumer, "from models import Box\n\nBox()\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(consumer)
    uses = [t for t in tokens if t.token_modifiers == ()]
    assert [(t.range.start.line, t.token_type) for t in uses] == [(2, "class")]


def test_semantic_tokens_from_import_of_non_workspace_target_is_not_emitted(
    tmp_path: Path,
) -> None:
    """Only workspace declarations are classified; stdlib imports stay unstyled."""

    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "app.py"
    _write(consumer, "from json import dumps\n\ndumps({})\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_for_file(consumer)
    assert [t for t in tokens if t.token_modifiers == ()] == []


def test_semantic_tokens_range_agrees_with_full_for_from_imports(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def greet() -> None:\n    pass\n")
    consumer = root / "app.py"
    _write(consumer, "from helper import greet\n\ngreet()\n")
    with WorkspaceSession(root) as session:
        full = session.semantic_tokens_for_file(consumer)
        ranged = session.semantic_tokens_range_for_file(consumer, start_line=2)
    assert ranged == tuple(t for t in full if t.range.start.line >= 2)
    assert any(t.token_type == "function" for t in ranged)


def test_semantic_tokens_overlay_change_reflected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def first():\n    pass\n")
    with WorkspaceSession(root) as session:
        session.set_overlay(target, "def second():\n    pass\n")
        tokens = session.semantic_tokens_for_file(target)
    function_tokens = [t for t in tokens if t.token_type == "function"]
    assert function_tokens[0].range.end.character - function_tokens[0].range.start.character == len(
        "second"
    )


def test_language_server_advertises_semantic_tokens_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
    finally:
        if server._session is not None:
            server._session.close()

    provider = init["capabilities"]["semanticTokensProvider"]
    assert provider["full"] is True
    assert provider["range"] is True
    assert provider["legend"]["tokenTypes"] == [
        "namespace",
        "class",
        "function",
        "method",
        "parameter",
        "variable",
    ]
    assert provider["legend"]["tokenModifiers"] == ["declaration", "async"]


def test_language_server_semantic_tokens_full_delta_encodes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def greet(name: str) -> None:\n    pass\n\ngreet('hi')\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/semanticTokens/full",
            {"textDocument": {"uri": target.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()

    # Three tokens — `greet` (function, decl), `name` (parameter, decl),
    # `greet` use on line 3.
    # Encoding: 5 ints per token: [deltaLine, deltaStart, length, type, mods].
    # Token type indices: function=2, parameter=4. Modifier `declaration` = bit 0 (= 1).
    assert result == {
        "data": [
            # First token: greet def at (0, 4), length 5, function, declaration
            0,
            4,
            5,
            2,
            1,
            # Second token: name at (0, 10) — same line, delta_start = 10-4 = 6,
            # length 4, parameter, declaration
            0,
            6,
            4,
            4,
            1,
            # Third token: greet use at (3, 0) — delta_line = 3, delta_start = 0,
            # length 5, function, no modifiers
            3,
            0,
            5,
            2,
            0,
        ]
    }


def test_language_server_semantic_tokens_full_unparseable_returns_empty_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/semanticTokens/full",
            {"textDocument": {"uri": target.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result == {"data": []}


def test_semantic_token_exports_are_re_exported_from_pyinc_tools() -> None:
    import pyinc_tools

    for name in ("SemanticToken", "SemanticTokenType", "SemanticTokenModifier"):
        assert hasattr(pyinc_tools, name), name
    token = SemanticToken(
        range=_range(0, 0, 0, 3),
        token_type="function",
        token_modifiers=("declaration",),
    )
    assert token.token_type == "function"


def test_semantic_tokens_range_returns_only_tokens_inside_range(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def first():\n    pass\n\ndef second():\n    pass\n\ndef third():\n    pass\n",
    )
    with WorkspaceSession(root) as session:
        all_tokens = session.semantic_tokens_for_file(target)
        # Restrict to the middle `def second` block (lines 3..4 inclusive,
        # using 0-based LSP coords): end is exclusive at line 6, char 0.
        middle = session.semantic_tokens_range_for_file(
            target, start_line=3, start_character=0, end_line=6, end_character=0
        )

    # The full document has three function-declaration tokens.
    declarations = [t for t in all_tokens if t.token_type == "function"]
    assert [t.range.start.line for t in declarations] == [0, 3, 6]
    # The range filter keeps only the middle one.
    assert [
        (
            t.range.start.line,
            t.range.start.character,
            t.range.end.character - t.range.start.character,
        )
        for t in middle
    ] == [(3, 4, 6)]


def test_semantic_tokens_range_excludes_token_on_end_line_at_end_character(
    tmp_path: Path,
) -> None:
    """The range is half-open ``[start, end)``: a token whose start position
    equals the end boundary is excluded."""
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def first():\n    pass\n\ndef second():\n    pass\n")
    with WorkspaceSession(root) as session:
        excluded = session.semantic_tokens_range_for_file(
            target, start_line=0, start_character=0, end_line=3, end_character=4
        )
        included = session.semantic_tokens_range_for_file(
            target, start_line=0, start_character=0, end_line=3, end_character=5
        )
    # `def second` starts at (3, 4). With end=(3, 4) it's excluded;
    # with end=(3, 5) it's included.
    assert all(not (t.range.start.line == 3 and t.range.start.character == 4) for t in excluded)
    assert any(t.range.start.line == 3 and t.range.start.character == 4 for t in included)


def test_semantic_tokens_range_omitting_end_line_scans_through_eof(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def first():\n    pass\n\ndef second():\n    pass\n")
    with WorkspaceSession(root) as session:
        full = session.semantic_tokens_for_file(target)
        # Omit ``end_line``: scan from start through end-of-file.
        same = session.semantic_tokens_range_for_file(target)
        from_line_3 = session.semantic_tokens_range_for_file(
            target, start_line=3, start_character=0
        )
    assert same == full
    # Tokens from line 0 (the `def first` header) are excluded; the `def
    # second` header on line 3 is included.
    assert all(t.range.start.line >= 3 for t in from_line_3)
    assert any(t.range.start.line == 3 and t.token_type == "function" for t in from_line_3)


def test_semantic_tokens_range_empty_when_range_covers_no_tokens(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def first():\n    pass\n\ndef second():\n    pass\n")
    with WorkspaceSession(root) as session:
        # Line 1 is the body `pass` line — no symbol-table tokens there.
        empty = session.semantic_tokens_range_for_file(
            target, start_line=1, start_character=0, end_line=2, end_character=0
        )
    assert empty == ()


def test_semantic_tokens_range_unparseable_file_returns_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")
    with WorkspaceSession(root) as session:
        tokens = session.semantic_tokens_range_for_file(
            target, start_line=0, start_character=0, end_line=100, end_character=0
        )
    assert tokens == ()


def test_semantic_tokens_range_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with WorkspaceSession(root) as session, pytest.raises(FileNotFoundError):
        session.semantic_tokens_range_for_file(root / "nope.py")


def test_language_server_semantic_tokens_range_delta_encodes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "def first():\n    pass\n\ndef second():\n    pass\n",
    )
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/semanticTokens/range",
            {
                "textDocument": {"uri": target.as_uri()},
                "range": {
                    "start": {"line": 3, "character": 0},
                    "end": {"line": 4, "character": 0},
                },
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    # Only the `def second` header token (line 3, char 4, length 6) is in
    # range. Delta encoding resets the running cursor from (0, 0), so the
    # first emitted token's deltaLine and deltaStart are absolute.
    # Token type indices: function=2. Modifier `declaration` = bit 0 (= 1).
    assert result == {"data": [3, 4, 6, 2, 1]}


def test_language_server_semantic_tokens_range_unparseable_returns_empty_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "broken.py"
    _write(target, "def (\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/semanticTokens/range",
            {
                "textDocument": {"uri": target.as_uri()},
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 0},
                },
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result == {"data": []}


def test_language_server_semantic_tokens_range_missing_file_returns_empty_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    missing = root / "absent.py"
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/semanticTokens/range",
            {
                "textDocument": {"uri": missing.as_uri()},
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 10, "character": 0},
                },
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result == {"data": []}


def _apply_file_rename_edits(edits: tuple[FileRenameEdit, ...]) -> None:
    """Apply FileRenameEdits to disk, right-to-left within each file."""
    by_path: dict[str, list[FileRenameEdit]] = {}
    for edit in edits:
        by_path.setdefault(edit.path, []).append(edit)
    for path, file_edits in by_path.items():
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        ordered = sorted(file_edits, key=lambda e: (-e.range.start.line, -e.range.start.character))
        for edit in ordered:
            assert edit.range.start.line == edit.range.end.line, "multi-line edits unsupported"
            line = lines[edit.range.start.line]
            newline = "\n" if line.endswith("\n") else ""
            content = line[:-1] if newline else line
            patched = (
                content[: edit.range.start.character]
                + edit.new_text
                + content[edit.range.end.character :]
            )
            lines[edit.range.start.line] = patched + newline
        Path(path).write_text("".join(lines), encoding="utf-8")


def test_file_rename_rewrites_absolute_import_and_from_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(
        root / "user.py",
        "import helper\nfrom helper import foo\n\nhelper.foo()\nfoo()\n",
    )
    _write(
        root / "aliased.py",
        "import helper as h\nfrom helper import foo as f\n\nh.foo()\nf()\n",
    )

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(root / "helper.py", root / "utils.py")])

    by_file: dict[str, list[tuple[int, int, int, str]]] = {Path(e.path).name: [] for e in edits}
    for edit in edits:
        by_file[Path(edit.path).name].append(
            (
                edit.range.start.line,
                edit.range.start.character,
                edit.range.end.character,
                edit.new_text,
            )
        )
    assert by_file["user.py"] == [
        (0, 7, 13, "utils"),
        (1, 5, 11, "utils"),
    ]
    assert by_file["aliased.py"] == [
        (0, 7, 13, "utils"),
        (1, 5, 11, "utils"),
    ]

    _apply_file_rename_edits(edits)
    assert (root / "user.py").read_text() == (
        "import utils\nfrom utils import foo\n\nhelper.foo()\nfoo()\n"
    )
    # The `as` clause is preserved; only the module portion is rewritten.
    assert (root / "aliased.py").read_text() == (
        "import utils as h\nfrom utils import foo as f\n\nh.foo()\nf()\n"
    )


def test_file_rename_preserves_relative_import_when_anchor_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "sub.py", "from .helper import foo\nfoo()\n")
    _write(root / "outside.py", "from pkg.helper import foo\nfoo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(pkg / "helper.py", pkg / "utils.py")])

    by_file: dict[str, list[tuple[int, int, int, str]]] = {}
    for edit in edits:
        by_file.setdefault(Path(edit.path).name, []).append(
            (
                edit.range.start.line,
                edit.range.start.character,
                edit.range.end.character,
                edit.new_text,
            )
        )
    # Relative import inside the same package keeps the leading dot.
    assert by_file["sub.py"] == [(0, 5, 12, ".utils")]
    # Absolute import outside the package picks up the new dotted path.
    assert by_file["outside.py"] == [(0, 5, 15, "pkg.utils")]

    _apply_file_rename_edits(edits)
    assert (pkg / "sub.py").read_text() == "from .utils import foo\nfoo()\n"
    assert (root / "outside.py").read_text() == ("from pkg.utils import foo\nfoo()\n")


def test_file_rename_falls_back_to_absolute_on_cross_directory_move(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    top = root / "top"
    _write(pkg / "__init__.py", "")
    _write(top / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "user.py", "from .helper import foo\nfoo()\n")
    _write(root / "other.py", "import pkg.helper\npkg.helper.foo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(pkg / "helper.py", top / "helper.py")])

    by_file: dict[str, list[tuple[int, int, int, str]]] = {}
    for edit in edits:
        by_file.setdefault(Path(edit.path).name, []).append(
            (
                edit.range.start.line,
                edit.range.start.character,
                edit.range.end.character,
                edit.new_text,
            )
        )
    # The relative import's anchor (`pkg`) no longer contains the new
    # module, so the rewrite goes to absolute form.
    assert by_file["user.py"] == [(0, 5, 12, "top.helper")]
    assert by_file["other.py"] == [(0, 7, 17, "top.helper")]


def test_file_rename_rewrites_from_pkg_import_leaf_when_parent_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "sibling.py", "from pkg import helper\nhelper.foo()\n")
    _write(pkg / "sibling_alias.py", "from pkg import helper as h\nh.foo()\n")
    _write(pkg / "rel.py", "from . import helper\nhelper.foo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(pkg / "helper.py", pkg / "utils.py")])

    by_file: dict[str, list[tuple[int, int, int, str]]] = {}
    for edit in edits:
        by_file.setdefault(Path(edit.path).name, []).append(
            (
                edit.range.start.line,
                edit.range.start.character,
                edit.range.end.character,
                edit.new_text,
            )
        )
    # `from pkg import helper` -> `from pkg import utils` (leaf rewrite)
    assert by_file["sibling.py"] == [(0, 16, 22, "utils")]
    # `as` clause preserved on the leaf rewrite.
    assert by_file["sibling_alias.py"] == [(0, 16, 22, "utils")]
    # `from . import helper` -> `from . import utils`
    assert by_file["rel.py"] == [(0, 14, 20, "utils")]

    _apply_file_rename_edits(edits)
    assert (pkg / "sibling.py").read_text() == "from pkg import utils\nhelper.foo()\n"
    assert (pkg / "sibling_alias.py").read_text() == "from pkg import utils as h\nh.foo()\n"
    assert (pkg / "rel.py").read_text() == "from . import utils\nhelper.foo()\n"


def test_file_rename_skips_from_pkg_import_leaf_on_cross_directory_move(
    tmp_path: Path,
) -> None:
    # `from pkg import helper` cannot be cleanly rewritten when `helper.py`
    # moves out of `pkg`: it would require either rewriting usages of
    # `helper.foo()` or inserting `as helper`, neither of which is in scope.
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    top = root / "top"
    _write(pkg / "__init__.py", "")
    _write(top / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "consumer.py", "from pkg import helper\nhelper.foo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(pkg / "helper.py", top / "helper.py")])

    assert all(Path(e.path).name != "consumer.py" for e in edits)


def test_file_rename_handles_multiple_renames_in_one_call(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo(): return 1\n")
    _write(root / "b.py", "def bar(): return 2\n")
    _write(
        root / "user.py",
        "import a\nfrom b import bar\n\na.foo()\nbar()\n",
    )

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames(
            [
                (root / "a.py", root / "aa.py"),
                (root / "b.py", root / "bb.py"),
            ]
        )

    by_line = sorted(
        (edit.range.start.line, edit.range.start.character, edit.range.end.character, edit.new_text)
        for edit in edits
        if Path(edit.path).name == "user.py"
    )
    assert by_line == [
        (0, 7, 8, "aa"),
        (1, 5, 6, "bb"),
    ]


def test_file_rename_skips_init_py(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "consumer.py", "import pkg\n")

    new_init = root / "newpkg" / "__init__.py"
    new_init.parent.mkdir(parents=True, exist_ok=True)
    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(pkg / "__init__.py", new_init)])
    assert edits == ()


def test_file_rename_skips_no_op_rename(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "user.py", "import helper\nhelper.foo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(root / "helper.py", root / "helper.py")])
    assert edits == ()


def test_file_rename_skips_paths_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "user.py", "import helper\n")

    outside = tmp_path / "outside" / "helper.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_renames([(root / "helper.py", outside)])
    assert edits == ()


def test_file_rename_uses_overlay_text(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    # On disk: no import. Overlay: importing helper.
    _write(root / "user.py", "# no import\n")

    with WorkspaceSession(root) as session:
        session.set_overlay(root / "user.py", "import helper\nhelper.foo()\n")
        edits = session.import_edits_for_file_renames([(root / "helper.py", root / "utils.py")])

    user_edits = [e for e in edits if Path(e.path).name == "user.py"]
    assert len(user_edits) == 1
    assert user_edits[0].new_text == "utils"


def test_language_server_advertises_will_rename_files_capability(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
    finally:
        if server._session is not None:
            server._session.close()
    file_ops = init["capabilities"]["workspace"]["fileOperations"]
    assert "willRename" in file_ops
    filters = file_ops["willRename"]["filters"]
    assert filters[0]["pattern"]["glob"] == "**/*.py"
    assert filters[0]["pattern"]["matches"] == "file"


def test_language_server_will_rename_files_returns_workspace_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "user.py", "from .helper import foo\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "workspace/willRenameFiles",
            {
                "files": [
                    {
                        "oldUri": (pkg / "helper.py").as_uri(),
                        "newUri": (pkg / "utils.py").as_uri(),
                    }
                ]
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is not None
    user_uri = (pkg / "user.py").as_uri()
    assert user_uri in result["changes"]
    text_edits = result["changes"][user_uri]
    assert len(text_edits) == 1
    edit = text_edits[0]
    assert edit["newText"] == ".utils"
    assert edit["range"] == {
        "start": {"line": 0, "character": 5},
        "end": {"line": 0, "character": 12},
    }


def test_language_server_will_rename_files_returns_null_when_no_edits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "user.py", "import helper\nhelper.foo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # Same-name rename: no edits expected.
        result = server._handle_request(
            "workspace/willRenameFiles",
            {
                "files": [
                    {
                        "oldUri": (root / "helper.py").as_uri(),
                        "newUri": (root / "helper.py").as_uri(),
                    }
                ]
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_language_server_will_rename_files_ignores_unsafe_uris(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "user.py", "import helper\n")

    outside = tmp_path / "outside.py"
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "workspace/willRenameFiles",
            {
                "files": [
                    {
                        "oldUri": (root / "helper.py").as_uri(),
                        "newUri": outside.as_uri(),
                    }
                ]
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def _apply_file_deletion_edits(edits: tuple[FileDeletionEdit, ...]) -> None:
    """Apply FileDeletionEdits to disk, right-to-left within each file."""
    by_path: dict[str, list[FileDeletionEdit]] = {}
    for edit in edits:
        by_path.setdefault(edit.path, []).append(edit)
    for path, file_edits in by_path.items():
        text = Path(path).read_text(encoding="utf-8")
        ordered = sorted(
            file_edits,
            key=lambda e: (-e.range.start.line, -e.range.start.character),
        )
        for edit in ordered:
            start_offset = _offset(text, edit.range.start.line, edit.range.start.character)
            end_offset = _offset(text, edit.range.end.line, edit.range.end.character)
            text = text[:start_offset] + edit.new_text + text[end_offset:]
        Path(path).write_text(text, encoding="utf-8")


def _offset(text: str, line: int, character: int) -> int:
    pos = 0
    for _ in range(line):
        nl = text.find("\n", pos)
        if nl == -1:
            return len(text)
        pos = nl + 1
    return min(pos + character, len(text))


def test_file_deletion_removes_whole_import_statement(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(
        root / "user.py",
        "import helper\nfrom helper import foo\n\nhelper.foo()\nfoo()\n",
    )

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([root / "helper.py"])

    user_edits = [e for e in edits if Path(e.path).name == "user.py"]
    spans = sorted(
        (e.range.start.line, e.range.start.character, e.range.end.line, e.range.end.character)
        for e in user_edits
    )
    # Both `import helper` (line 0) and `from helper import foo` (line 1)
    # are now broken; each is removed as a whole-line edit.
    assert spans == [(0, 0, 1, 0), (1, 0, 2, 0)]
    assert all(e.new_text == "" for e in user_edits)

    _apply_file_deletion_edits(edits)
    assert (root / "user.py").read_text() == "\nhelper.foo()\nfoo()\n"


def test_file_deletion_removes_relative_from_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "user.py", "from .helper import foo\nfoo()\n")
    _write(root / "outside.py", "from pkg.helper import foo\nfoo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([pkg / "helper.py"])

    by_file: dict[str, list[FileDeletionEdit]] = {}
    for edit in edits:
        by_file.setdefault(Path(edit.path).name, []).append(edit)
    assert "user.py" in by_file
    assert "outside.py" in by_file
    assert by_file["user.py"][0].new_text == ""
    assert by_file["outside.py"][0].new_text == ""

    _apply_file_deletion_edits(edits)
    assert (pkg / "user.py").read_text() == "foo()\n"
    assert (root / "outside.py").read_text() == "foo()\n"


def test_file_deletion_removes_from_pkg_import_leaf(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "sibling.py", "from pkg import helper\nhelper.foo()\n")
    _write(pkg / "rel.py", "from . import helper\nhelper.foo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([pkg / "helper.py"])

    by_file: dict[str, list[FileDeletionEdit]] = {}
    for edit in edits:
        by_file.setdefault(Path(edit.path).name, []).append(edit)

    # `from pkg import helper` (only name) → whole statement removed.
    assert by_file["sibling.py"][0].range.start.line == 0
    assert by_file["sibling.py"][0].range.end.line == 1
    # `from . import helper` (only name) → whole statement removed.
    assert by_file["rel.py"][0].range.start.line == 0
    assert by_file["rel.py"][0].range.end.line == 1

    _apply_file_deletion_edits(edits)
    assert (pkg / "sibling.py").read_text() == "helper.foo()\n"
    assert (pkg / "rel.py").read_text() == "helper.foo()\n"


def test_file_deletion_partial_alias_in_multi_name_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo(): return 1\n")
    _write(root / "b.py", "def bar(): return 2\n")
    _write(root / "user.py", "import a, b\na.foo()\nb.bar()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([root / "a.py"])

    user_edits = [e for e in edits if Path(e.path).name == "user.py"]
    # Only the `a` alias is removed; the rest of the statement survives.
    assert len(user_edits) == 1
    edit = user_edits[0]
    # The span absorbs the trailing comma + whitespace up to `b`.
    assert edit.range.start.line == 0
    assert edit.range.start.character == 7  # column of `a` in `import a, b`
    assert edit.range.end.line == 0
    assert edit.range.end.character == 10  # column of `b` in `import a, b`

    _apply_file_deletion_edits(edits)
    assert (root / "user.py").read_text() == "import b\na.foo()\nb.bar()\n"


def test_file_deletion_partial_alias_in_multi_name_from_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "a.py", "def foo(): return 1\n")
    _write(pkg / "b.py", "def bar(): return 2\n")
    _write(pkg / "user.py", "from pkg import a, b\na.foo()\nb.bar()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([pkg / "a.py"])

    user_edits = [e for e in edits if Path(e.path).name == "user.py"]
    assert len(user_edits) == 1

    _apply_file_deletion_edits(edits)
    # The dead `a` leaf is removed; the surviving `b` stays.
    assert (pkg / "user.py").read_text() == "from pkg import b\na.foo()\nb.bar()\n"


def test_file_deletion_partial_alias_last_in_list_absorbs_preceding_comma(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo(): return 1\n")
    _write(root / "b.py", "def bar(): return 2\n")
    _write(root / "user.py", "import a, b\na.foo()\nb.bar()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([root / "b.py"])

    _apply_file_deletion_edits(edits)
    assert (root / "user.py").read_text() == "import a\na.foo()\nb.bar()\n"


def test_file_deletion_handles_multiple_deletions(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo(): return 1\n")
    _write(root / "b.py", "def bar(): return 2\n")
    _write(root / "user.py", "import a\nfrom b import bar\n\na.foo()\nbar()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([root / "a.py", root / "b.py"])

    user_edits = sorted(
        (e.range.start.line, e.range.end.line) for e in edits if Path(e.path).name == "user.py"
    )
    assert user_edits == [(0, 1), (1, 2)]

    _apply_file_deletion_edits(edits)
    assert (root / "user.py").read_text() == "\na.foo()\nbar()\n"


def test_file_deletion_skips_importer_being_deleted(tmp_path: Path) -> None:
    # A file that imports the deleted module is itself being deleted — no
    # point in editing it.
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo(): return 1\n")
    _write(root / "user.py", "import helper\nhelper.foo()\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([root / "helper.py", root / "user.py"])
    # `user.py` is being deleted, so no edits are emitted for it.
    assert all(Path(e.path).name != "user.py" for e in edits)


def test_file_deletion_skips_init_py(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo(): return 1\n")
    _write(root / "consumer.py", "import pkg\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([pkg / "__init__.py"])
    assert edits == ()


def test_file_deletion_skips_paths_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo(): return 1\n")
    _write(root / "user.py", "import helper\n")

    outside = tmp_path / "outside.py"
    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([outside])
    assert edits == ()


def test_file_deletion_skips_non_py_files(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo(): return 1\n")
    _write(root / "data.txt", "hello\n")
    _write(root / "user.py", "import helper\n")

    with WorkspaceSession(root) as session:
        edits = session.import_edits_for_file_deletions([root / "data.txt"])
    assert edits == ()


def test_file_deletion_uses_overlay_text(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    # On disk: no import. Overlay: importing helper.
    _write(root / "user.py", "# no import\n")

    with WorkspaceSession(root) as session:
        session.set_overlay(root / "user.py", "import helper\nhelper.foo()\n")
        edits = session.import_edits_for_file_deletions([root / "helper.py"])

    user_edits = [e for e in edits if Path(e.path).name == "user.py"]
    assert len(user_edits) == 1
    assert user_edits[0].new_text == ""


def test_language_server_advertises_will_delete_files_capability(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
    finally:
        if server._session is not None:
            server._session.close()
    file_ops = init["capabilities"]["workspace"]["fileOperations"]
    assert "willDelete" in file_ops
    filters = file_ops["willDelete"]["filters"]
    assert filters[0]["pattern"]["glob"] == "**/*.py"
    assert filters[0]["pattern"]["matches"] == "file"


def test_language_server_will_delete_files_returns_workspace_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "user.py", "from .helper import foo\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "workspace/willDeleteFiles",
            {"files": [{"uri": (pkg / "helper.py").as_uri()}]},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is not None
    user_uri = (pkg / "user.py").as_uri()
    assert user_uri in result["changes"]
    text_edits = result["changes"][user_uri]
    assert len(text_edits) == 1
    edit = text_edits[0]
    assert edit["newText"] == ""
    assert edit["range"] == {
        "start": {"line": 0, "character": 0},
        "end": {"line": 1, "character": 0},
    }


def test_language_server_will_delete_files_returns_null_when_no_edits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "unrelated.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        # No file actually imports helper, so deleting it produces no edits.
        result = server._handle_request(
            "workspace/willDeleteFiles",
            {"files": [{"uri": (root / "helper.py").as_uri()}]},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_language_server_will_delete_files_ignores_unsafe_uris(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(root / "user.py", "import helper\n")

    outside = tmp_path / "outside.py"
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "workspace/willDeleteFiles",
            {"files": [{"uri": outside.as_uri()}]},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is None


def test_language_server_advertises_diagnostic_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
    finally:
        if server._session is not None:
            server._session.close()
    provider = init["capabilities"]["diagnosticProvider"]
    assert provider["identifier"] == "pyinc-tools"
    assert provider["interFileDependencies"] is True
    assert provider["workspaceDiagnostics"] is True


def test_language_server_document_diagnostic_returns_full_report(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root / "user.py", "import totally_unknown_module\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        report = server._handle_request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": (root / "user.py").as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert report["kind"] == "full"
    assert isinstance(report["resultId"], str) and report["resultId"]
    codes = {item["code"] for item in report["items"]}
    assert "missing-import" in codes


def test_language_server_document_diagnostic_clean_file_is_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root / "ok.py", "x = 1\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        report = server._handle_request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": (root / "ok.py").as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert report["kind"] == "full"
    assert report["items"] == []


def test_language_server_document_diagnostic_unchanged_when_result_id_matches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root / "user.py", "import totally_unknown_module\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        uri = (root / "user.py").as_uri()
        first = server._handle_request("textDocument/diagnostic", {"textDocument": {"uri": uri}})
        second = server._handle_request(
            "textDocument/diagnostic",
            {
                "textDocument": {"uri": uri},
                "previousResultId": first["resultId"],
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert second["kind"] == "unchanged"
    assert second["resultId"] == first["resultId"]


def test_language_server_document_diagnostic_changes_after_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root / "user.py", "import totally_unknown_module\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        uri = (root / "user.py").as_uri()
        first = server._handle_request("textDocument/diagnostic", {"textDocument": {"uri": uri}})
        # Fix the import via an overlay; the stale result id must no longer match.
        server._require_session().set_overlay(str(root / "user.py"), "x = 1\n")
        second = server._handle_request(
            "textDocument/diagnostic",
            {
                "textDocument": {"uri": uri},
                "previousResultId": first["resultId"],
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert second["kind"] == "full"
    assert second["items"] == []
    assert second["resultId"] != first["resultId"]


def test_language_server_document_diagnostic_unsafe_uri_is_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    _write(outside, "import totally_unknown_module\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        report = server._handle_request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": outside.as_uri()}},
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert report["kind"] == "full"
    assert report["items"] == []


def test_language_server_workspace_diagnostic_reports_each_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root / "bad.py", "import totally_unknown_module\n")
    _write(root / "ok.py", "x = 1\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        report = server._handle_request("workspace/diagnostic", {})
    finally:
        if server._session is not None:
            server._session.close()
    by_uri = {item["uri"]: item for item in report["items"]}
    bad_uri = (root / "bad.py").as_uri()
    ok_uri = (root / "ok.py").as_uri()
    assert by_uri[bad_uri]["kind"] == "full"
    assert any(d["code"] == "missing-import" for d in by_uri[bad_uri]["items"])
    # The clean file still gets a report (with no items) so clients can clear.
    assert ok_uri in by_uri
    assert by_uri[ok_uri]["items"] == []


def test_language_server_workspace_diagnostic_unchanged_with_previous_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root / "bad.py", "import totally_unknown_module\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        first = server._handle_request("workspace/diagnostic", {})
        previous = [{"uri": item["uri"], "value": item["resultId"]} for item in first["items"]]
        second = server._handle_request("workspace/diagnostic", {"previousResultIds": previous})
    finally:
        if server._session is not None:
            server._session.close()
    assert second["items"]
    assert all(item["kind"] == "unchanged" for item in second["items"])


# ---------------------------------------------------------------------------
# textDocument/completion
# ---------------------------------------------------------------------------

_COMPLETION_HELPERS = (
    "def compute() -> int:\n"
    "    return 1\n"
    "\n"
    "class Widget:\n"
    "    size: int = 3\n"
    "    def render(self) -> str:\n"
    "        return 'w'\n"
    "\n"
    "CONST = 5\n"
)

_COMPLETION_APP = (
    "from helpers import compute, Widget\nimport helpers\n\ndef run() -> int:\n    return 1\n"
)


def _caret(text: str, marker_line: str) -> tuple[str, int, int]:
    """Return (source, line, character) for a caret at the end of ``marker_line``.

    ``marker_line`` is appended to ``text`` as a new final line; the caret sits
    at its end, mimicking a mid-edit buffer.
    """
    source = text + marker_line + "\n"
    line = source.splitlines().index(marker_line)
    return source, line, len(marker_line)


def _labels(items: tuple[CompletionItem, ...]) -> set[str]:
    return {item.label for item in items}


def test_completion_bare_name_offers_local_symbols_and_keywords(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, _COMPLETION_APP)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(_COMPLETION_APP, "    comp")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert "compute" in _labels(items)

        # A keyword prefix surfaces keyword items.
        source, line, character = _caret(_COMPLETION_APP, "    ret")
        session.set_overlay(app, source)
        keyword_items = session.completions_at(app, line, character)
        assert any(item.label == "return" and item.kind == "keyword" for item in keyword_items)


def test_completion_attribute_lists_module_and_class_members(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, _COMPLETION_APP)

    with WorkspaceSession(root) as session:
        # Module attribute access: helpers.<caret>
        source, line, character = _caret(_COMPLETION_APP, "    helpers.")
        session.set_overlay(app, source)
        module_items = session.completions_at(app, line, character)
        assert {"compute", "Widget", "CONST"} <= _labels(module_items)
        assert any(item.label == "compute" and item.kind == "function" for item in module_items)

        # Class attribute access: Widget.<caret>
        source, line, character = _caret(_COMPLETION_APP, "    Widget.")
        session.set_overlay(app, source)
        class_items = session.completions_at(app, line, character)
        assert _labels(class_items) == {"render", "size"}
        assert any(item.label == "render" and item.kind == "method" for item in class_items)


def test_completion_from_import_lists_workspace_module_members(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, _COMPLETION_APP)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(_COMPLETION_APP, "from helpers import ")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert {"compute", "Widget", "CONST"} <= _labels(items)


def test_completion_excludes_stdlib_and_strings(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, _COMPLETION_APP)

    with WorkspaceSession(root) as session:
        # Attribute access on a stdlib module yields no workspace members.
        stdlib_src = _COMPLETION_APP + "import os\n"
        source, line, character = _caret(stdlib_src, "    os.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()

        # A caret inside a string literal offers nothing.
        source, line, character = _caret(_COMPLETION_APP, "x = 'helpers.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


def test_completion_rejects_outside_and_missing_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    with WorkspaceSession(root) as session:
        # A path outside the workspace root is refused, like every other
        # position-based feature.
        outside = tmp_path / "elsewhere.py"
        _write(outside, "x = 1\n")
        with pytest.raises(ValueError):
            session.completions_at(outside, 0, 1)

        # A missing in-workspace file raises FileNotFoundError.
        with pytest.raises(FileNotFoundError):
            session.completions_at(root / "nope.py", 0, 0)


def test_completion_is_stable_when_unrelated_file_changes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    other = root / "other.py"
    _write(other, "def unrelated() -> int:\n    return 0\n")
    app = root / "app.py"
    _write(app, _COMPLETION_APP)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(_COMPLETION_APP, "    helpers.")
        session.set_overlay(app, source)
        first = session.completions_at(app, line, character)

        # Editing an unrelated file must not change app.py's completions; the
        # workspace/module symbol tables are memoized and reused across requests.
        session.set_overlay(other, "def unrelated() -> int:\n    return 999\n")
        second = session.completions_at(app, line, character)
        assert first == second
        assert {"compute", "Widget", "CONST"} <= _labels(second)


def test_language_server_advertises_and_serves_completion(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    # The on-disk buffer is mid-edit: an attribute access with no member yet.
    _write(app, _COMPLETION_APP + "\ndef edit() -> int:\n    helpers.\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        provider = init["capabilities"]["completionProvider"]
        assert provider["triggerCharacters"] == ["."]

        caret_line = _COMPLETION_APP.count("\n") + 2  # the "    helpers." line
        result = server._handle_request(
            "textDocument/completion",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": caret_line, "character": len("    helpers.")},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result["isIncomplete"] is False
    labels = {item["label"] for item in result["items"]}
    assert {"compute", "Widget", "CONST"} <= labels
    # Module kind (9) is emitted for none of these members; function/class/field
    # kinds are present and stdlib members are absent.
    assert "path" not in labels  # would only appear if os/stdlib were expanded


# ---------------------------------------------------------------------------
# Task B2 — unused-import diagnostic + textDocument/codeAction quick fixes
# ---------------------------------------------------------------------------


def test_find_references_does_not_count_the_import_binding_site(
    tmp_path: Path,
) -> None:
    # Pins the behavior the unused-import rule relies on: a `from M import name`
    # binding is an `ast.alias`, not an `ast.Name`, so the occurrence scan never
    # emits a reference for the import statement itself. An unused import
    # therefore yields zero references in its own file; a used one yields one.
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    _write(root / "unused.py", "from m import foo\n")
    _write(root / "used.py", "from m import foo\nfoo()\n")

    with WorkspaceSession(root) as session:
        unused_symbol = session.symbol_at(
            root / "unused.py", SourcePosition(0, len("from m import ") + 1)
        )
        used_symbol = session.symbol_at(root / "used.py", SourcePosition(1, 1))
        assert unused_symbol is not None
        assert used_symbol is not None
        unused_refs = session.find_references(unused_symbol)
        used_refs = session.find_references(used_symbol)

    unused_in_file = [r for r in unused_refs.references if Path(r.path).name == "unused.py"]
    used_in_file = [r for r in used_refs.references if Path(r.path).name == "used.py"]
    assert unused_in_file == []
    assert len(used_in_file) == 1


def test_analysis_diagnostic_tags_default_empty() -> None:
    diagnostic = AnalysisDiagnostic(
        path="/x.py",
        code="missing-import",
        message="boom",
        severity="error",
        source="pyinc.python_source",
    )
    assert diagnostic.tags == ()


def test_analysis_diagnostic_to_lsp_maps_unnecessary_tag(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        tagged = AnalysisDiagnostic(
            path=str(root / "mod.py"),
            code="unused-import",
            message="unused",
            severity="hint",
            source="pyinc.symbol_resolution",
            range=_range(0, 0, 0, 1),
            tags=("unnecessary",),
        )
        payload = server._analysis_diagnostic_to_lsp(tagged)
        assert payload["tags"] == [1]
        assert payload["severity"] == 4

        untagged = AnalysisDiagnostic(
            path=str(root / "mod.py"),
            code="missing-import",
            message="boom",
            severity="error",
            source="pyinc.python_source",
            range=_range(0, 0, 0, 1),
        )
        # No `tags` key at all when the diagnostic carries none.
        assert "tags" not in server._analysis_diagnostic_to_lsp(untagged)
    finally:
        if server._session is not None:
            server._session.close()


def test_diagnostic_signature_distinguishes_tags() -> None:
    from pyinc_tools.lsp import _diagnostic_signature

    base = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
        "severity": 4,
        "source": "pyinc.symbol_resolution",
        "code": "unused-import",
        "message": "unused",
    }
    tagged = {**base, "tags": [1]}
    assert _diagnostic_signature(base) != _diagnostic_signature(tagged)


def test_unused_import_diagnostic_flags_unused_workspace_from_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    _write(root / "consumer.py", "from m import foo\n\nx = 1\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "consumer.py")

    unused = [d for d in result.diagnostics if d.code == "unused-import"]
    assert len(unused) == 1
    diag = unused[0]
    assert diag.severity == "hint"
    assert diag.tags == ("unnecessary",)
    assert diag.range is not None
    assert diag.range.start.line + 1 == 1
    assert diag.range.start.character == len("from m import ")
    assert "foo" in diag.message


def test_unused_import_diagnostic_silent_when_import_is_used(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    _write(root / "consumer.py", "from m import foo\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "consumer.py")

    assert [d for d in result.diagnostics if d.code == "unused-import"] == []


def test_unused_import_diagnostic_flags_aliased_binding(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    _write(root / "consumer.py", "from m import foo as bar\n\nx = 1\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "consumer.py")

    unused = [d for d in result.diagnostics if d.code == "unused-import"]
    assert len(unused) == 1
    # The message names the local binding, not the original symbol.
    assert "bar" in unused[0].message


def test_unused_import_diagnostic_skips_self_alias_reexport(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    # `from m import foo as foo` is the canonical explicit re-export idiom.
    _write(root / "consumer.py", "from m import foo as foo\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "consumer.py")

    assert [d for d in result.diagnostics if d.code == "unused-import"] == []


def test_unused_import_diagnostic_skips_init_py(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "__init__.py", "from pkg.helper import foo\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(pkg / "__init__.py")

    assert [d for d in result.diagnostics if d.code == "unused-import"] == []


def test_unused_import_diagnostic_suppressed_by_cross_module_reexport(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    # `hub` imports foo from m but never uses it locally...
    _write(root / "hub.py", "from m import foo\n")
    # ...yet another module imports foo *from hub*, so hub re-exports it.
    _write(root / "client.py", "from hub import foo\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "hub.py")

    assert [d for d in result.diagnostics if d.code == "unused-import"] == []


def test_unused_import_diagnostic_suppressed_by_star_reexport(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    _write(root / "hub.py", "from m import foo\n")
    _write(root / "client.py", "from hub import *\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "hub.py")

    assert [d for d in result.diagnostics if d.code == "unused-import"] == []


def test_unused_import_diagnostic_not_emitted_for_broken_symbol_import(
    tmp_path: Path,
) -> None:
    # `wrong` is a real workspace module but has no `foo`. That's an
    # unresolved-symbol problem, not an unused import — the import must not be
    # double-flagged as unused just because find_references can't verify it.
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "wrong.py", "def other() -> int:\n    return 1\n")
    _write(root / "consumer.py", "from wrong import foo\n\nx = 1\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "consumer.py")

    codes = {d.code for d in result.diagnostics}
    assert "unresolved-symbol" in codes
    assert "unused-import" not in codes


def test_unused_import_diagnostic_skips_stdlib_and_plain_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    # stdlib from-import (unverifiable) + plain `import m` (attribute usage
    # under-reported) — neither should be flagged.
    _write(root / "consumer.py", "import os\nfrom json import dumps\nimport m\n\nx = 1\n")

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "consumer.py")

    assert [d for d in result.diagnostics if d.code == "unused-import"] == []


def test_unused_import_diagnostic_suppressed_by_module_all_listing(
    tmp_path: Path,
) -> None:
    # A facade module re-exports `tool` through its own static `__all__`.
    # That's an intentional public re-export — removing the import would
    # break the public API, so it must not be flagged as unused.
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helpers.py", "def tool() -> int:\n    return 1\n")
    _write(root / "facade.py", 'from helpers import tool\n\n__all__ = ["tool"]\n')

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "facade.py")

    assert [d for d in result.diagnostics if d.code == "unused-import"] == []


def test_unused_import_diagnostic_still_flagged_when_not_in_module_all(
    tmp_path: Path,
) -> None:
    # `tool` is imported but absent from `__all__` (which lists a different
    # name) and unused — the `__all__` guard must not shield it.
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helpers.py", "def tool() -> int:\n    return 1\n")
    _write(root / "facade.py", 'from helpers import tool\n\n__all__ = ["other"]\n')

    with WorkspaceSession(root) as session:
        result = session.analyze_file(root / "facade.py")

    unused = [d for d in result.diagnostics if d.code == "unused-import"]
    assert len(unused) == 1
    assert "tool" in unused[0].message


def _apply_code_action_edits(source: str, edits: tuple[CodeActionEdit, ...]) -> str:
    """Apply a single action's edits to a source string (right-to-left)."""
    ordered = sorted(edits, key=lambda e: (-e.range.start.line, -e.range.start.character))
    text = source
    for edit in ordered:
        start = _offset(text, edit.range.start.line, edit.range.start.character)
        end = _offset(text, edit.range.end.line, edit.range.end.character)
        text = text[:start] + edit.new_text + text[end:]
    return text


def test_code_actions_unused_import_sole_alias_removes_whole_statement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    src = "from m import foo\n\nx = 1\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 0, 0)

    assert len(actions) == 1
    action = actions[0]
    assert action.kind == "quickfix"
    assert action.diagnostic.code == "unused-import"
    assert "foo" in action.title
    assert _apply_code_action_edits(src, action.edits) == "\nx = 1\n"


def test_code_actions_unused_import_one_of_several_absorbs_comma(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\ndef bar() -> int:\n    return 2\n")
    src = "from m import foo, bar\n\nbar()\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 0, 0)

    unused = [a for a in actions if a.diagnostic.code == "unused-import"]
    assert len(unused) == 1
    # Only `foo` is dead; `bar` survives with the statement intact.
    assert _apply_code_action_edits(src, unused[0].edits) == "from m import bar\n\nbar()\n"


def test_code_actions_for_range_removes_unused_alias_in_multiline_import(
    tmp_path: Path,
) -> None:
    # In a parenthesised multi-line import the unused-import diagnostic anchors
    # on the *alias* line, not the statement's first line. The lookup that maps
    # a diagnostic back to its import statement has to be span-aware or no fix
    # is offered at all.
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "m.py",
        "def foo() -> int:\n    return 1\n"
        "def bar() -> int:\n    return 2\n"
        "def baz() -> int:\n    return 3\n",
    )
    src = "from m import (\n    foo,\n    bar,\n    baz,\n)\n\nfoo()\nbaz()\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 8, 0)

    unused = [a for a in actions if a.diagnostic.code == "unused-import"]
    assert len(unused) == 1
    assert "bar" in unused[0].title
    assert (
        _apply_code_action_edits(src, unused[0].edits)
        == "from m import (\n    foo,\n    baz,\n)\n\nfoo()\nbaz()\n"
    )


def test_code_actions_for_range_removes_middle_alias_keeps_others(
    tmp_path: Path,
) -> None:
    # Three aliases on one line, middle one dead → the surviving list stays
    # comma-correct with `foo` and `baz` intact.
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "m.py",
        "def foo() -> int:\n    return 1\n"
        "def bar() -> int:\n    return 2\n"
        "def baz() -> int:\n    return 3\n",
    )
    src = "from m import foo, bar, baz\n\nfoo()\nbaz()\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 0, 0)

    unused = [a for a in actions if a.diagnostic.code == "unused-import"]
    assert len(unused) == 1
    assert "bar" in unused[0].title
    assert (
        _apply_code_action_edits(src, unused[0].edits) == "from m import foo, baz\n\nfoo()\nbaz()\n"
    )


def test_code_actions_missing_import_removes_statement(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    src = "import definitely_not_a_module\n\nx = 1\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 0, 0)

    missing = [a for a in actions if a.diagnostic.code == "missing-import"]
    assert len(missing) == 1
    assert missing[0].title == "Remove unresolvable import"
    assert _apply_code_action_edits(src, missing[0].edits) == "\nx = 1\n"


def test_code_actions_unresolved_symbol_offers_removal_and_unique_retarget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    # `foo` actually lives in `home`, not `wrong` — exactly one workspace module
    # exposes a top-level `foo`, so a retarget is offered.
    _write(root / "home.py", "def foo() -> int:\n    return 1\n")
    _write(root / "wrong.py", "def other() -> int:\n    return 1\n")
    src = "from wrong import foo\n\nfoo()\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 0, 0)

    titles = [a.title for a in actions]
    assert "Remove import of 'foo'" in titles
    assert "Import 'foo' from 'home'" in titles
    retarget = next(a for a in actions if a.title == "Import 'foo' from 'home'")
    assert _apply_code_action_edits(src, retarget.edits) == "from home import foo\n\nfoo()\n"


def test_code_actions_unresolved_symbol_no_retarget_when_ambiguous(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    # Two workspace modules expose a top-level `foo` → retarget is ambiguous
    # and therefore suppressed; only the removal action remains.
    _write(root / "one.py", "def foo() -> int:\n    return 1\n")
    _write(root / "two.py", "def foo() -> int:\n    return 2\n")
    _write(root / "wrong.py", "def other() -> int:\n    return 1\n")
    src = "from wrong import foo\n\nfoo()\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 0, 0)

    titles = [a.title for a in actions]
    assert "Remove import of 'foo'" in titles
    assert not any(t.startswith("Import 'foo' from") for t in titles)


def test_code_actions_unresolved_symbol_no_retarget_when_absent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "wrong.py", "def other() -> int:\n    return 1\n")
    src = "from wrong import nowhere\n\nnowhere()\n"
    _write(root / "consumer.py", src)

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 0, 0, 0, 0)

    titles = [a.title for a in actions]
    assert "Remove import of 'nowhere'" in titles
    assert not any(t.startswith("Import 'nowhere' from") for t in titles)


def test_code_actions_empty_when_range_misses_all_diagnostics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    # unused import on line 0; ask for actions on line 2 (the `x = 1` line).
    _write(root / "consumer.py", "from m import foo\n\nx = 1\n")

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "consumer.py", 2, 0, 2, 0)

    assert actions == ()


def test_code_actions_empty_for_unparseable_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "broken.py", "import definitely_not_a_module\ndef (:\n")

    with WorkspaceSession(root) as session:
        actions = session.code_actions_for_range(root / "broken.py", 0, 0, 5, 0)

    assert actions == ()


def test_language_server_advertises_code_action_provider(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "mod.py", "x = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        init = server._handle_request("initialize", {"rootUri": root.as_uri()})
        provider = init["capabilities"]["codeActionProvider"]
        assert provider == {"codeActionKinds": ["quickfix"]}
    finally:
        if server._session is not None:
            server._session.close()


def test_language_server_code_action_returns_quickfix_with_workspace_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    target = root / "consumer.py"
    _write(target, "from m import foo\n\nx = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": target.as_uri()},
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 0},
                },
                "context": {"diagnostics": []},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert len(result) == 1
    action = result[0]
    assert action["kind"] == "quickfix"
    assert "foo" in action["title"]
    # The anchor diagnostic is echoed back, converted, with the Unnecessary tag.
    assert len(action["diagnostics"]) == 1
    assert action["diagnostics"][0]["code"] == "unused-import"
    assert action["diagnostics"][0]["tags"] == [1]
    # WorkspaceEdit shape: {"changes": {uri: [TextEdit]}}.
    changes = action["edit"]["changes"]
    edits = changes[target.as_uri()]
    assert edits[0]["newText"] == ""
    assert edits[0]["range"]["start"] == {"line": 0, "character": 0}


def test_language_server_code_action_honors_context_only_filter(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    target = root / "consumer.py"
    _write(target, "from m import foo\n\nx = 1\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})

        def code_action_params(only: list[str]) -> dict[str, object]:
            return {
                "textDocument": {"uri": target.as_uri()},
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 0},
                },
                "context": {"diagnostics": [], "only": only},
            }

        refactor_only = server._handle_request(
            "textDocument/codeAction", code_action_params(["refactor"])
        )
        quickfix_only = server._handle_request(
            "textDocument/codeAction", code_action_params(["quickfix"])
        )
    finally:
        if server._session is not None:
            server._session.close()

    # `refactor` excludes our quickfix; `quickfix` includes it.
    assert refactor_only == []
    assert len(quickfix_only) == 1


def test_language_server_code_action_sees_overlay_introduced_unused_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo() -> int:\n    return 1\n")
    target = root / "consumer.py"
    # On disk the import is used; the overlay removes the use, so the import
    # becomes unused and a quick fix must appear from the overlay text alone.
    _write(target, "from m import foo\n\nfoo()\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        server._handle_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": target.as_uri(),
                    "text": "from m import foo\n\nx = 1\n",
                }
            },
        )
        result = server._handle_request(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": target.as_uri()},
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 0},
                },
                "context": {"diagnostics": []},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert len(result) == 1
    assert result[0]["diagnostics"][0]["code"] == "unused-import"


# ---------------------------------------------------------------------------
# Task B3 — completion / signatureHelp polish
#   (b) dotted attribute owners; (d1) attribute-call signatureHelp;
#   (d2) default values in signature labels
# ---------------------------------------------------------------------------


def test_completion_dotted_module_owner_lists_exports(tmp_path: Path) -> None:
    # `import pkg.sub; pkg.sub.<caret>` — the dotted owner is itself a
    # workspace module, so its top-level exports are offered.
    root = tmp_path / "workspace"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "sub.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, "import pkg.sub\n")

    with WorkspaceSession(root) as session:
        source, line, character = _caret("import pkg.sub\n", "pkg.sub.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert {"compute", "Widget", "CONST"} <= _labels(items)
        # Class members do not leak into the module-level export list.
        assert "render" not in _labels(items)


def test_completion_dotted_module_class_owner_lists_members(tmp_path: Path) -> None:
    # `pkg.sub.Widget.<caret>` (dotted module head) and
    # `helpers.Widget.<caret>` (single-component module head) both list the
    # class's members.
    root = tmp_path / "workspace"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "sub.py", _COMPLETION_HELPERS)
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, "import pkg.sub\nimport helpers\n")

    with WorkspaceSession(root) as session:
        source, line, character = _caret("import pkg.sub\n", "pkg.sub.Widget.")
        session.set_overlay(app, source)
        dotted = session.completions_at(app, line, character)
        assert _labels(dotted) == {"render", "size"}

        source, line, character = _caret("import helpers\n", "helpers.Widget.")
        session.set_overlay(app, source)
        single = session.completions_at(app, line, character)
        assert _labels(single) == {"render", "size"}


def test_completion_stdlib_dotted_owner_is_empty(tmp_path: Path) -> None:
    # `os.path.<caret>` — the head resolves to a stdlib module, so no members.
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, "import os.path\n")

    with WorkspaceSession(root) as session:
        source, line, character = _caret("import os.path\n", "os.path.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


def test_completion_instance_chain_owner_is_empty(tmp_path: Path) -> None:
    # `w.size.<caret>` — an instance-attribute chain whose type would have to
    # be inferred stays unsupported.
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    base = "from helpers import Widget\nw = Widget()\n"
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "w.size.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


# ---------------------------------------------------------------------------
# Task B4 — self./cls. instance-member completion
# ---------------------------------------------------------------------------


_SELF_COMPLETION_SOURCE = (
    "class Widget:\n"
    "    size: int = 3\n"
    "    def __init__(self) -> None:\n"
    "        self.name = 'w'\n"
    "        self.count = 0\n"
    "    def render(self) -> str:\n"
    "        return self.name\n"
)

_CLS_COMPLETION_SOURCE = (
    "class Widget:\n"
    "    size: int = 3\n"
    "    def __init__(self) -> None:\n"
    "        self.name = 'w'\n"
    "    @classmethod\n"
    "    def make(cls) -> 'Widget':\n"
    "        return cls()\n"
)


def test_completion_self_lists_instance_and_class_members(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    app = root / "app.py"
    _write(app, _SELF_COMPLETION_SOURCE)

    with WorkspaceSession(root) as session:
        # A caret on `self.` inside a method whose first parameter is `self`
        # sees instance vars, class vars, and methods — the instance view.
        source, line, character = _caret(_SELF_COMPLETION_SOURCE, "        self.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert _labels(items) == {"size", "name", "count", "__init__", "render"}

        by_label = {item.label: item for item in items}
        assert by_label["name"].kind == "field"
        assert by_label["size"].kind == "field"
        assert by_label["render"].kind == "method"

        # Prefix filtering narrows to matching members.
        source, line, character = _caret(_SELF_COMPLETION_SOURCE, "        self.c")
        session.set_overlay(app, source)
        filtered = session.completions_at(app, line, character)
        assert _labels(filtered) == {"count"}


def test_completion_self_outside_method_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    app = root / "app.py"
    _write(app, _SELF_COMPLETION_SOURCE)

    with WorkspaceSession(root) as session:
        # `self.` at module level has no enclosing method → nothing.
        source, line, character = _caret(_SELF_COMPLETION_SOURCE, "self.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


def test_completion_self_in_closure_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    app = root / "app.py"
    closure_src = (
        "class Widget:\n"
        "    def render(self) -> str:\n"
        "        def inner() -> str:\n"
        "            return ''\n"
    )
    _write(app, closure_src)

    with WorkspaceSession(root) as session:
        # The innermost enclosing callable is the closure `inner`, not a method
        # of Widget → the self view is unavailable.
        source, line, character = _caret(closure_src, "            self.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


def test_completion_cls_lists_class_view_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    app = root / "app.py"
    _write(app, _CLS_COMPLETION_SOURCE)

    with WorkspaceSession(root) as session:
        # `cls.` in a method whose first parameter is `cls` sees class vars and
        # methods but NOT instance attributes.
        source, line, character = _caret(_CLS_COMPLETION_SOURCE, "        cls.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert _labels(items) == {"size", "__init__", "make"}
        assert "name" not in _labels(items)

        # `self.` inside a `cls`-method has no bound `self` → nothing.
        source, line, character = _caret(_CLS_COMPLETION_SOURCE, "        self.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


def test_completion_self_reflects_overlay_added_attribute(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    app = root / "app.py"
    _write(app, _SELF_COMPLETION_SOURCE)

    with WorkspaceSession(root) as session:
        edited = (
            "class Widget:\n"
            "    size: int = 3\n"
            "    def __init__(self) -> None:\n"
            "        self.name = 'w'\n"
            "        self.extra = 1\n"
            "    def render(self) -> str:\n"
            "        return self.name\n"
        )
        source, line, character = _caret(edited, "        self.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert "extra" in _labels(items)


def test_completion_self_is_stable_when_unrelated_file_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    other = root / "other.py"
    _write(other, "def unrelated() -> int:\n    return 0\n")
    app = root / "app.py"
    _write(app, _SELF_COMPLETION_SOURCE)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(_SELF_COMPLETION_SOURCE, "        self.")
        session.set_overlay(app, source)
        first = session.completions_at(app, line, character)

        session.set_overlay(other, "def unrelated() -> int:\n    return 999\n")
        second = session.completions_at(app, line, character)
        assert first == second
        assert _labels(second) == {"size", "name", "count", "__init__", "render"}


# ---------------------------------------------------------------------------
# Task B4 (stage 2) — annotated-name owner completion (Rule A)
# ---------------------------------------------------------------------------


_ANNOT_HELPERS = (
    "class Widget:\n"
    "    size: int = 3\n"
    "    def __init__(self) -> None:\n"
    "        self.name = 'w'\n"
    "    def render(self) -> str:\n"
    "        return self.name\n"
    "\n"
    "class Gadget:\n"
    "    weight: int = 1\n"
    "    def spin(self) -> str:\n"
    "        return 'g'\n"
)

# Instance view = methods + class vars + instance vars.
_WIDGET_INSTANCE_VIEW = {"size", "name", "__init__", "render"}


def test_completion_annotated_param_completes_instance_view(tmp_path: Path) -> None:
    # `def f(w: Widget): w.<caret>` — the parameter's annotation resolves to the
    # workspace class, and its instance view is offered.
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _ANNOT_HELPERS)
    app = root / "app.py"
    base = "from helpers import Widget\n\n\ndef f(w: Widget) -> None:\n    pass\n"
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "    w.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert _labels(items) == _WIDGET_INSTANCE_VIEW
        by_label = {item.label: item for item in items}
        assert by_label["name"].kind == "field"
        assert by_label["render"].kind == "method"

        # Prefix filtering narrows the instance view.
        source, line, character = _caret(base, "    w.re")
        session.set_overlay(app, source)
        assert _labels(session.completions_at(app, line, character)) == {"render"}


def test_completion_annotated_local_var_completes_instance_view(
    tmp_path: Path,
) -> None:
    # A local `w: Widget = ...` annotation resolves the same as a parameter.
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _ANNOT_HELPERS)
    app = root / "app.py"
    base = "from helpers import Widget\n\n\ndef f() -> None:\n    w: Widget = Widget()\n"
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "    w.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert _labels(items) == _WIDGET_INSTANCE_VIEW


def test_completion_annotated_nearest_declaration_wins(tmp_path: Path) -> None:
    # Two local annotations for `w`; the nearest preceding one (Widget) wins
    # over the earlier (Gadget).
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _ANNOT_HELPERS)
    app = root / "app.py"
    base = (
        "from helpers import Widget, Gadget\n\n\n"
        "def f() -> None:\n"
        "    w: Gadget = Gadget()\n"
        "    w: Widget = Widget()\n"
    )
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "    w.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert _labels(items) == _WIDGET_INSTANCE_VIEW
        assert "spin" not in _labels(items)
        assert "weight" not in _labels(items)


@pytest.mark.parametrize(
    "annotation",
    [
        "list[Widget]",
        "Optional[Widget]",
        "Widget | None",
        "dict[str, Widget]",
        "Annotated[Widget, 'meta']",
        "Callable[[], Widget]",
    ],
)
def test_completion_annotated_generic_or_union_is_empty(tmp_path: Path, annotation: str) -> None:
    # A bounded model rejects subscripted / union / callable annotations rather
    # than half-inferring the wrapped class.
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _ANNOT_HELPERS)
    app = root / "app.py"
    base = (
        "from typing import Annotated, Callable, Optional\n"
        "from helpers import Widget\n\n\n"
        f"def f(w: {annotation}) -> None:\n    pass\n"
    )
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "    w.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


@pytest.mark.parametrize(
    "declaration, expected",
    [
        ("w: Widget", _WIDGET_INSTANCE_VIEW),  # bare Name
        ('w: "Widget"', _WIDGET_INSTANCE_VIEW),  # whole-string forward ref
        ("w: helpers.Widget", _WIDGET_INSTANCE_VIEW),  # one-hop attribute
        ("w: pkg.sub.Widget", set()),  # deep dotted chain → nothing
        ("w: OrderedDict", set()),  # non-workspace (stdlib) → nothing
    ],
)
def test_completion_module_level_annotation_forms(
    tmp_path: Path, declaration: str, expected: set[str]
) -> None:
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _ANNOT_HELPERS)
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "sub.py", _ANNOT_HELPERS)
    app = root / "app.py"
    base = (
        "from collections import OrderedDict\n"
        "from helpers import Widget\n"
        "import helpers\n"
        "import pkg.sub\n"
        f"{declaration}\n"
    )
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "w.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert _labels(items) == expected


def test_completion_annotated_import_alias_precedence(tmp_path: Path) -> None:
    # A same-named local annotation must NOT shadow an import that resolves via
    # the existing attribute path: `Widget` resolves to the imported class, so
    # its class-object view (no instance-only `name`) wins — Rule A never fires.
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _ANNOT_HELPERS)
    app = root / "app.py"
    base = (
        "from helpers import Widget, Gadget\n\n\ndef f() -> None:\n    Widget: Gadget = Gadget()\n"
    )
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "    Widget.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        # Class-object view of Widget (methods + class vars), not Gadget's, and
        # not the instance-only `name`.
        assert _labels(items) == {"size", "render", "__init__"}


def test_completion_annotation_outer_function_scope_not_applied(
    tmp_path: Path,
) -> None:
    # An annotation on an OUTER function's parameter does not apply inside a
    # nested function — only the innermost enclosing function is consulted.
    root = tmp_path / "workspace"
    _write(root / "helpers.py", _ANNOT_HELPERS)
    app = root / "app.py"
    base = (
        "from helpers import Widget\n\n\n"
        "def outer(w: Widget) -> None:\n"
        "    def inner() -> None:\n"
        "        pass\n"
    )
    _write(app, base)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(base, "        w.")
        session.set_overlay(app, source)
        assert session.completions_at(app, line, character) == ()


# ---------------------------------------------------------------------------
# Task B4 (stage 3) — inherited-member completion via flattened class_model
# ---------------------------------------------------------------------------


_INHERIT_BASE = (
    "class Base:\n"
    "    kind: str = 'b'\n"
    "    def base_method(self) -> None:\n"
    "        self.base_attr = 1\n"
)

_INHERIT_DERIVED = (
    "from base import Base\n"
    "\n"
    "\n"
    "class Derived(Base):\n"
    "    size: int = 3\n"
    "    def own(self) -> None:\n"
    "        self.own_attr = 2\n"
)


def test_completion_self_includes_inherited_members(tmp_path: Path) -> None:
    # `self.` inside a method of a subclass sees the flattened instance view:
    # own members plus every inherited method / class var / instance var.
    root = tmp_path / "workspace"
    _write(root / "base.py", _INHERIT_BASE)
    app = root / "derived.py"
    _write(app, _INHERIT_DERIVED)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(_INHERIT_DERIVED, "        self.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        assert _labels(items) == {
            "size",
            "own",
            "own_attr",
            "kind",
            "base_method",
            "base_attr",
        }


def test_completion_derived_class_view_shows_inherited_methods(
    tmp_path: Path,
) -> None:
    # `Derived.` (bare-name class owner) serves the flattened CLASS view:
    # methods + class vars, own and inherited, but no instance attributes.
    root = tmp_path / "workspace"
    _write(root / "base.py", _INHERIT_BASE)
    app = root / "derived.py"
    _write(app, _INHERIT_DERIVED)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(_INHERIT_DERIVED, "Derived.")
        session.set_overlay(app, source)
        items = session.completions_at(app, line, character)
        # Inherited `base_method` / `kind` show; instance-only attrs do not.
        assert _labels(items) == {"size", "own", "base_method", "kind"}
        by_label = {item.label: item for item in items}
        assert by_label["base_method"].kind == "method"


def test_completion_inherited_reflects_overlay_edit_to_base(tmp_path: Path) -> None:
    # An overlay edit that adds a method to the BASE file flows through to the
    # subclass's inherited completions — the flattened model is per-file.
    root = tmp_path / "workspace"
    base = root / "base.py"
    _write(base, _INHERIT_BASE)
    app = root / "derived.py"
    _write(app, _INHERIT_DERIVED)

    with WorkspaceSession(root) as session:
        source, line, character = _caret(_INHERIT_DERIVED, "        self.")
        session.set_overlay(app, source)
        before = session.completions_at(app, line, character)
        assert "extra_method" not in _labels(before)

        edited_base = _INHERIT_BASE + "    def extra_method(self) -> None:\n        pass\n"
        session.set_overlay(base, edited_base)
        after = session.completions_at(app, line, character)
        assert "extra_method" in _labels(after)


def test_language_server_serves_self_completion(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    app = root / "app.py"
    # The on-disk buffer is mid-edit: a bare `self.` with no member yet.
    _write(app, _SELF_COMPLETION_SOURCE + "        self.\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        caret_line = _SELF_COMPLETION_SOURCE.count("\n")  # the "        self." line
        result = server._handle_request(
            "textDocument/completion",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": caret_line, "character": len("        self.")},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    assert result["isIncomplete"] is False
    labels = {item["label"] for item in result["items"]}
    assert {"name", "count", "size", "render"} <= labels


def test_language_server_serves_dotted_attribute_completion(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "sub.py", _COMPLETION_HELPERS)
    app = root / "app.py"
    _write(app, "import pkg.sub\npkg.sub.\n")

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/completion",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 1, "character": len("pkg.sub.")},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()

    labels = {item["label"] for item in result["items"]}
    assert {"compute", "Widget", "CONST"} <= labels


def test_signature_help_at_attribute_call(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo(x: int, y: int) -> int:\n    return x + y\n")
    consumer = root / "app.py"
    _write(consumer, "import m\n\nm.foo(1, 2)\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(consumer, line=2, character=6)
    assert signature_help is not None
    assert signature_help.label == "def foo(x: int, y: int) -> int"
    assert signature_help.active_parameter == 0


def test_signature_help_at_attribute_class_construction(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "m.py",
        "class Box:\n"
        "    def __init__(self, width: int, height: int) -> None:\n"
        "        self.width = width\n"
        "        self.height = height\n",
    )
    consumer = root / "app.py"
    _write(consumer, "import m\n\nm.Box(1, 2)\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(consumer, line=2, character=6)
    assert signature_help is not None
    assert signature_help.label == "def Box(width: int, height: int)"
    assert signature_help.active_parameter == 0


def test_signature_help_at_stdlib_attribute_call_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "app.py"
    _write(app, 'import os\n\nos.getenv("X")\n')
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(app, line=2, character=10)
    assert signature_help is None


def test_signature_help_at_proven_dotted_module_chain(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "sub.py", "def foo(x: int) -> int:\n    return x\n")
    consumer = root / "app.py"
    _write(consumer, "import pkg.sub\n\npkg.sub.foo(1)\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(consumer, line=2, character=12)
    assert signature_help is not None
    assert signature_help.label == "def foo(x: int) -> int"


def test_language_server_serves_attribute_signature_help(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "m.py", "def foo(x: int, y: int) -> int:\n    return x + y\n")
    app = root / "app.py"
    _write(app, "import m\n\nm.foo(1, )\n")
    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        result = server._handle_request(
            "textDocument/signatureHelp",
            {
                "textDocument": {"uri": app.as_uri()},
                "position": {"line": 2, "character": 9},
            },
        )
    finally:
        if server._session is not None:
            server._session.close()
    assert result is not None
    assert result["signatures"][0]["label"] == "def foo(x: int, y: int) -> int"
    assert result["activeParameter"] == 1


def test_signature_help_at_positional_default_in_label(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def f(x: int, y: int = 5) -> int:\n    return x + y\n\nf(1)\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=3, character=2)
    assert signature_help is not None
    assert signature_help.label == "def f(x: int, y: int = 5) -> int"


def test_signature_help_at_kwonly_default_in_label(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def g(a: int, *, k: int = 3) -> int:\n    return a + k\n\ng(1)\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=3, character=2)
    assert signature_help is not None
    assert signature_help.label == "def g(a: int, k: int = 3) -> int"


def test_signature_help_at_parameter_offsets_stay_aligned_with_defaults(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(target, "def f(a: int, b: int = 2) -> int:\n    return a + b\n\nf(1)\n")
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=3, character=2)
    assert signature_help is not None
    assert signature_help.label == "def f(a: int, b: int = 2) -> int"
    assert tuple(
        (p.label, p.label_offset_start, p.label_offset_end) for p in signature_help.parameters
    ) == (
        ("a: int", 6, 12),
        ("b: int = 2", 14, 24),
    )
    # The reported offsets must index the label back to the parameter text.
    for parameter in signature_help.parameters:
        assert (
            signature_help.label[parameter.label_offset_start : parameter.label_offset_end]
            == parameter.label
        )


def test_signature_help_at_class_init_defaults_strip_self(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    _write(
        target,
        "class Box:\n"
        "    def __init__(self, width: int = 10, height: int = 20) -> None:\n"
        "        self.width = width\n"
        "        self.height = height\n"
        "\n"
        "Box()\n",
    )
    with WorkspaceSession(root) as session:
        signature_help = session.signature_help_at(target, line=5, character=4)
    assert signature_help is not None
    assert signature_help.label == "def Box(width: int = 10, height: int = 20)"
    for parameter in signature_help.parameters:
        assert (
            signature_help.label[parameter.label_offset_start : parameter.label_offset_end]
            == parameter.label
        )


# ---------------------------------------------------------------------------
# CLI diagnostic reporting against a real workspace
# ---------------------------------------------------------------------------


def test_cli_text_output_uses_one_based_real_geometry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The text formatter's 1-based conversion, proven against real analysis."""

    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "app.py", "x = 1\nimport definitely_not_installed\n")

    exit_code = cli.main(["analyze", str(root), "--format", "text"])
    assert exit_code == cli.EXIT_SUCCESS

    lines = capsys.readouterr().out.splitlines()
    missing = [line for line in lines if "missing-import" in line]
    assert len(missing) == 1
    # The import sits on source line index 1, so it must render as line 2.
    assert missing[0].startswith(f"{root / 'app.py'}:2:1: error missing-import ")


def test_cli_fail_on_error_gates_a_real_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "app.py", "import definitely_not_installed\n")

    assert cli.main(["analyze", str(root), "--format", "text"]) == cli.EXIT_SUCCESS
    capsys.readouterr()
    assert (
        cli.main(["analyze", str(root), "--format", "text", "--fail-on", "error"])
        == cli.EXIT_DIAGNOSTICS
    )
    capsys.readouterr()

    clean = tmp_path / "clean"
    clean.mkdir()
    _write(clean / "ok.py", "x = 1\n")
    assert (
        cli.main(["analyze", str(clean), "--format", "text", "--fail-on", "error"])
        == cli.EXIT_SUCCESS
    )
    assert capsys.readouterr().out == ""


def test_cli_text_output_omits_position_for_rangeless_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`source-decode-error` is the real producer of a diagnostic with no range."""

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "bad.py").write_bytes(b'# -*- coding: ascii -*-\nx = "\xff\xfe"\n')

    assert cli.main(["analyze", str(root), "--format", "text"]) == cli.EXIT_SUCCESS

    lines = capsys.readouterr().out.splitlines()
    decode_errors = [line for line in lines if "source-decode-error" in line]
    assert len(decode_errors) == 1
    # No `:line:col` segment — the path is followed directly by the severity.
    assert decode_errors[0].startswith(f"{root / 'bad.py'}: error source-decode-error ")
    # The message body names the real path too, not the temporary mirror, so the
    # line is identical across runs. "pyinc-tools-" is the mirror tempdir prefix.
    assert decode_errors[0].count(str(root / "bad.py")) == 2
    assert "pyinc-tools-" not in decode_errors[0]
