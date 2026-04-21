from __future__ import annotations

from pathlib import Path

from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import PollingWorkspaceWatcher, WorkspaceSession


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
