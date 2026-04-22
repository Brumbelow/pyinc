from __future__ import annotations

from pathlib import Path

from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import PollingWorkspaceWatcher, WorkspaceSession

_LSP_SYMBOL_KIND_FUNCTION = 12
_LSP_SYMBOL_KIND_METHOD = 6
_LSP_SYMBOL_KIND_CLASS = 5
_LSP_SYMBOL_KIND_FIELD = 8
_LSP_SYMBOL_KIND_VARIABLE = 13


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


def test_workspace_session_cross_file_invalidation_and_path_remap(tmp_path: Path) -> None:
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
        assert all(not module.path.startswith(session.mirror_root) for module in workspace.python.modules)
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
        assert {module.path for module in workspace.python.modules} == {str(first), str(second)}


def test_language_server_reports_document_and_workspace_symbols(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "symbols.py"
    _write(
        target,
        "class Box:\n"
        "    pass\n"
        "\n"
        "def helper() -> int:\n"
        "    return 1\n",
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


def test_language_server_hover_local_function_includes_signature(tmp_path: Path) -> None:
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


def test_language_server_definition_local_function_returns_same_file(tmp_path: Path) -> None:
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


def test_language_server_definition_on_unknown_identifier_returns_empty(tmp_path: Path) -> None:
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


def test_language_server_definition_follows_single_level_wildcard_import(tmp_path: Path) -> None:
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


def test_resolve_symbol_reference_cyclic_reexport_returns_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "a.py", "from b import foo\n")
    _write(root / "b.py", "from a import foo\n")

    with WorkspaceSession(root) as session:
        resolved = session.resolve_symbol_reference(root / "a.py", "foo")
        assert resolved.resolution == "ambiguous"
        assert resolved.defining_path is None


def test_language_server_hover_on_ambiguous_wildcard_returns_none(tmp_path: Path) -> None:
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


def test_language_server_document_symbol_surfaces_every_symbol_kind(tmp_path: Path) -> None:
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
        "    attr: str = \"\"\n"
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


def test_module_symbol_table_flags_type_checking_import_as_impurity(tmp_path: Path) -> None:
    """Pins current behavior: ``if TYPE_CHECKING:`` imports are a conditional top-level
    binding, so they are not walked into ``ModuleSymbolTable.symbols`` and instead are
    recorded in ``impurity_reasons``. The LSP hover and goto-definition handlers
    therefore cannot currently resolve a type-only import. This is a known limitation
    to revisit if we want editor support for annotations that only exist under
    ``TYPE_CHECKING``.
    """

    root = tmp_path / "workspace"
    root.mkdir()
    _write(root / "helper.py", "class Foo:\n    pass\n")
    consumer = root / "consumer.py"
    _write(
        consumer,
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from helper import Foo\n"
        "\n"
        "def g(a: \"Foo\") -> \"Foo\":\n"
        "    return a\n",
    )

    with WorkspaceSession(root) as session:
        analysis = session.analyze_file(consumer)
        assert analysis.symbols is not None
        qualified_names = {symbol.qualified_name for symbol in analysis.symbols.symbols}
        assert "Foo" not in qualified_names
        assert "conditional top-level binding" in analysis.symbols.impurity_reasons

    server = LanguageServer(default_root=str(root))
    try:
        server._handle_request("initialize", {"rootUri": root.as_uri()})
        hover = server._handle_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 5, "character": 10},
            },
        )
        assert hover is None

        locations = server._handle_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": consumer.as_uri()},
                "position": {"line": 5, "character": 10},
            },
        )
        assert locations == []
    finally:
        if server._session is not None:
            server._session.close()
