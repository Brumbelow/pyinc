from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from pyinc_tools.lsp import LanguageServer, _RequestFailed
from pyinc_tools.session import (
    PollingWorkspaceWatcher,
    RenameEdit,
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
            with contextlib.suppress(
                Exception
            ):  # pragma: no cover - best-effort teardown
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

        assert any(
            diagnostic.code == "syntax-error" for diagnostic in edited.diagnostics
        )
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
        assert not any(
            diagnostic.code == "syntax-error" for diagnostic in saved.diagnostics
        )


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
        assert not any(
            diagnostic.code == "unresolved-symbol" for diagnostic in clean.diagnostics
        )

        session.set_overlay(provider, "def bar() -> int:\n    return 1\n")
        broken = session.analyze_file(consumer)
        assert any(
            diagnostic.code == "unresolved-symbol" for diagnostic in broken.diagnostics
        )

        workspace = session.analyze_workspace()
        module_by_path = {module.path: module for module in workspace.python.modules}
        consumer_module = module_by_path[str(consumer)]

        assert workspace.python.root == str(root)
        assert all(
            not module.path.startswith(session.mirror_root)
            for module in workspace.python.modules
        )
        assert consumer_module.resolved_imports[0].resolved_path == str(provider)
        assert consumer_module.dependencies[0].path == str(provider)


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
        "class Box:\n" "    pass\n" "\n" "def helper() -> int:\n" "    return 1\n",
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

        workspace_symbols = server._handle_request(
            "workspace/symbol", {"query": "help"}
        )
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


def test_resolve_symbol_reference_wildcard_chain_is_bounded_by_intermediate_surface(
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
    _write(outer, "from middle import *\n")

    with WorkspaceSession(root) as session:
        resolved = session.resolve_symbol_reference(outer, "foo")
        assert resolved.resolution == "missing"


def test_resolve_symbol_reference_max_follow_depth_boundary(tmp_path: Path) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    entry_inside = _write_reexport_chain(inside, length=7, symbol="target")

    with WorkspaceSession(inside) as session:
        resolved = session.resolve_symbol_reference(entry_inside, "target")
        assert resolved.resolution == "workspace"
        assert resolved.defining_path == str(inside / "hop_07.py")
        assert resolved.defining_lineno == 1

    outside = tmp_path / "outside"
    outside.mkdir()
    entry_outside = _write_reexport_chain(outside, length=8, symbol="target")

    with WorkspaceSession(outside) as session:
        too_deep = session.resolve_symbol_reference(entry_outside, "target")
        assert too_deep.resolution == "ambiguous"
        assert too_deep.defining_path is None

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


def test_resolve_symbol_reference_cyclic_reexport_returns_ambiguous(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "from b import foo\n")
    _write(root / "b.py", "from a import foo\n")

    with WorkspaceSession(root) as session:
        resolved = session.resolve_symbol_reference(root / "a.py", "foo")
        assert resolved.resolution == "ambiguous"
        assert resolved.defining_path is None


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
        resolved = session.resolve_symbol_reference(consumer, "foo")
        assert resolved.resolution == "ambiguous"

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
        assert init["serverInfo"]["version"] == "2.0.0"

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
            loc["range"]["start"]["line"]
            for loc in locations
            if loc["uri"] == consumer.as_uri()
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
        highlights = session.find_document_highlights(target, "foo")
        kinds = sorted(h.kind for h in highlights)
        # Exactly one declaration ("write") and one call site ("text").
        assert kinds == ["text", "write"]
        decl = next(h for h in highlights if h.kind == "write")
        assert decl.lineno == 1
        assert decl.col_offset == len("def ")
        assert decl.end_col_offset == len("def ") + len("foo")
        call = next(h for h in highlights if h.kind == "text")
        assert call.lineno == 4
        assert call.col_offset == 0
        assert call.end_col_offset == len("foo")


def test_workspace_session_find_document_highlights_non_workspace_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    _write(consumer, "from json import JSONDecoder\n\nJSONDecoder()\n")

    with WorkspaceSession(root) as session:
        highlights = session.find_document_highlights(consumer, "JSONDecoder")
        assert highlights == ()


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


def test_watcher_double_start_raises(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
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


def test_watcher_double_stop_is_noop(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
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


def test_watcher_poll_after_start_raises(
    tmp_path: Path, watcher_factory: _WatcherFactory
) -> None:
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


def _apply_rename_edits(edits: tuple[RenameEdit, ...]) -> None:
    """Apply RenameEdits to disk, right-to-left within each file."""
    by_path: dict[str, list[RenameEdit]] = {}
    for edit in edits:
        by_path.setdefault(edit.path, []).append(edit)
    for path, file_edits in by_path.items():
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        ordered = sorted(file_edits, key=lambda e: (-e.lineno, -e.col_offset))
        for edit in ordered:
            line = lines[edit.lineno - 1]
            newline = "\n" if line.endswith("\n") else ""
            content = line[:-1] if newline else line
            patched = (
                content[: edit.col_offset]
                + edit.new_text
                + content[edit.end_col_offset :]
            )
            lines[edit.lineno - 1] = patched + newline
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
        result = session.rename_symbol(root / "b.py", "foo", "bar")
        assert result.status == "ok"
        edits_by_file: dict[str, list[tuple[int, int, int, str]]] = {}
        for edit in result.edits:
            edits_by_file.setdefault(Path(edit.path).name, []).append(
                (edit.lineno, edit.col_offset, edit.end_col_offset, edit.new_text)
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
    assert (
        root / "c.py"
    ).read_text() == "from a import bar as aliased\n\naliased()\n"


def test_rename_symbol_class_locates_def_offset_in_decorated_line(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "x.py", "class Widget:\n    pass\n")
    _write(root / "y.py", "from x import Widget\n\nWidget()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(root / "x.py", "Widget", "Gadget")
        assert result.status == "ok"
        per_file = sorted(
            (Path(e.path).name, e.lineno, e.col_offset, e.end_col_offset)
            for e in result.edits
        )
        assert per_file == [
            ("x.py", 1, 6, 12),
            ("y.py", 1, 14, 20),
            ("y.py", 3, 0, 6),
        ]
        _apply_rename_edits(result.edits)

    assert (root / "x.py").read_text() == "class Gadget:\n    pass\n"
    assert (
        root / "y.py"
    ).read_text() == "from x import Gadget\n\nGadget()\n"


def test_rename_symbol_rejects_invalid_identifier(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(root / "a.py", "foo", "1bad")
        assert result.status == "invalid_identifier"
        assert result.edits == ()


def test_rename_symbol_rejects_python_keyword(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(root / "a.py", "foo", "class")
        assert result.status == "keyword_identifier"
        assert result.edits == ()


def test_rename_symbol_same_name_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(root / "a.py", "foo", "foo")
        assert result.status == "same_name"
        assert result.edits == ()


def test_rename_symbol_refuses_non_workspace_target(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(
        root / "consumer.py",
        "from json import JSONDecoder\n\nJSONDecoder()\n",
    )

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(
            root / "consumer.py", "JSONDecoder", "MyDecoder"
        )
        assert result.status == "non_workspace_target"
        assert result.edits == ()


def test_rename_symbol_refuses_alias_rename(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "def foo() -> int:\n    return 1\n")
    _write(root / "c.py", "from a import foo as aliased\n\naliased()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(root / "c.py", "aliased", "quux")
        assert result.status == "alias_rename_unsupported"
        assert result.edits == ()


def test_rename_symbol_rewrites_sibling_relative_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(pkg / "sub.py", "from .helper import foo\n\nfoo()\n")

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(pkg / "helper.py", "foo", "bar")
        assert result.status == "ok"
        sub_edits = sorted(
            (edit.lineno, edit.col_offset, edit.end_col_offset)
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
        result = session.rename_symbol(sub / "helper.py", "foo", "bar")
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
        result = session.rename_symbol(pkg / "helper.py", "foo", "bar")
        assert result.status == "ok"
        _apply_rename_edits(result.edits)

    assert (sub / "user.py").read_text() == "from ..helper import bar\n\nbar()\n"


def test_rename_symbol_relative_import_preserves_as_alias(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "helper.py", "def foo() -> int:\n    return 1\n")
    _write(
        pkg / "sub.py", "from .helper import foo as aliased\n\naliased()\n"
    )

    with WorkspaceSession(root) as session:
        result = session.rename_symbol(pkg / "helper.py", "foo", "bar")
        assert result.status == "ok"
        sub_edits = [e for e in result.edits if Path(e.path).name == "sub.py"]
        # Exactly one edit on sub.py — the import-site `foo`. The `as aliased`
        # clause is preserved and `aliased()` is not a reference to `foo`.
        assert len(sub_edits) == 1
        assert sub_edits[0].col_offset == 20
        assert sub_edits[0].end_col_offset == 23
        _apply_rename_edits(result.edits)

    assert (
        pkg / "sub.py"
    ).read_text() == "from .helper import bar as aliased\n\naliased()\n"


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
        result = session.rename_symbol(root / "a.py", "foo", "bar")
        assert result.status == "ok"
        per_file = sorted(
            (Path(e.path).name, e.lineno, e.col_offset, e.end_col_offset, e.new_text)
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
        session.set_overlay(
            root / "b.py", "from a import foo\n\nfoo()\nfoo()\n"
        )
        result = session.rename_symbol(root / "b.py", "foo", "bar")
        assert result.status == "ok"
        b_edits = sorted(
            edit.lineno
            for edit in result.edits
            if Path(edit.path).name == "b.py"
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
