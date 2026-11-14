"""Requests that read one query answer stay off the diagnostics path.

Hover, documentSymbol and workspace/symbol need the symbol tables and the
workspace symbol index. Diagnostics add the dependency check and the per-file
checks on top, which costs seconds on a mid-size workspace. The spies below
count calls on session attributes, which no query reaches, so patching them
leaves every query's identity alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import pyinc_tools.session as session_module
from pyinc.integrations import workspace_dependency_check
from pyinc_tools._document import convert_payload_positions
from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import WorkspaceSession

_CORE = '''"""Core module. \U0001f600 widens UTF-16 columns."""

LIMIT: int = 3


class Box:
    size: int = 1

    class Inner:
        def peek(self) -> None:
            pass

    def grow(self, by: int = 1) -> "Box":
        return Box()


def helper(x: int, *args: str, key: str = "k") -> int:
    return x + LIMIT


async def fetch() -> str:
    return "a"
'''

_USER = """from pkg import Crate, helper
from pkg.core import Box, LIMIT


def use() -> int:
    return helper(LIMIT) + Box().size + Crate().size
"""


def _write_workspace(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text(
        "from .core import Box as Crate, helper\n", encoding="utf-8"
    )
    (root / "pkg" / "core.py").write_text(_CORE, encoding="utf-8")
    user = root / "user.py"
    user.write_text(_USER, encoding="utf-8")
    (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (root / "notes.txt").write_text("not python\n", encoding="utf-8")
    return user


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LanguageServer]:
    root = tmp_path / "workspace"
    _write_workspace(root)
    language_server = LanguageServer(default_root=str(root))
    language_server._handle_request(
        "initialize",
        {
            "rootUri": root.as_uri(),
            "initializationOptions": {"pyinc.watcher.enabled": False},
        },
    )
    try:
        yield language_server
    finally:
        language_server._teardown_session()


def _no_calls() -> dict[str, int]:
    return {
        "analyze_file": 0,
        "analyze_workspace": 0,
        "workspace_dependency_check": 0,
        "_module_diagnostics": 0,
    }


def _spy_on_diagnostics(
    monkeypatch: pytest.MonkeyPatch, session: WorkspaceSession
) -> dict[str, int]:
    calls = _no_calls()

    def counting(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            calls[name] += 1
            return function(*args, **kwargs)

        return spy

    for name in ("analyze_file", "analyze_workspace", "_module_diagnostics"):
        monkeypatch.setattr(session, name, counting(name, getattr(session, name)))
    monkeypatch.setattr(
        session_module,
        "workspace_dependency_check",
        counting("workspace_dependency_check", workspace_dependency_check),
    )
    return calls


def _root(server: LanguageServer) -> Path:
    assert server._session is not None
    return Path(server._session.root)


def test_hover_runs_no_dependency_check_or_diagnostics(
    server: LanguageServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert server._session is not None
    calls = _spy_on_diagnostics(monkeypatch, server._session)
    hover = server._handle_request(
        "textDocument/hover",
        {
            "textDocument": {"uri": (_root(server) / "user.py").as_uri()},
            "position": {"line": 5, "character": 12},
        },
    )
    assert hover is not None
    assert "def helper(x: int, *args: str, key: str) -> int" in hover["contents"]["value"]
    assert calls == _no_calls()


def test_document_symbol_runs_no_dependency_check_or_diagnostics(
    server: LanguageServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert server._session is not None
    calls = _spy_on_diagnostics(monkeypatch, server._session)
    symbols = server._handle_request(
        "textDocument/documentSymbol",
        {"textDocument": {"uri": (_root(server) / "pkg" / "core.py").as_uri()}},
    )
    assert [item["name"] for item in symbols] == [
        "LIMIT",
        "Box",
        "Box.size",
        "Box.Inner",
        "Box.Inner.peek",
        "Box.grow",
        "helper",
        "fetch",
    ]
    assert calls == _no_calls()


def test_workspace_symbol_runs_no_dependency_check_or_diagnostics(
    server: LanguageServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert server._session is not None
    calls = _spy_on_diagnostics(monkeypatch, server._session)
    symbols = server._handle_request("workspace/symbol", {"query": "box"})
    assert sorted((item["containerName"], item["name"]) for item in symbols) == [
        ("pkg.core", "Box"),
        ("pkg.core", "Box.Inner"),
        ("pkg.core", "Box.Inner.peek"),
        ("pkg.core", "Box.grow"),
        ("pkg.core", "Box.size"),
        ("user", "Box"),
    ]
    assert calls == _no_calls()


def test_symbol_table_for_file_matches_file_analysis(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_workspace(root)
    names = ("pkg/__init__.py", "pkg/core.py", "user.py", "broken.py", "notes.txt", "gone.py")
    with WorkspaceSession(root) as session:
        session.set_overlay(root / "user.py", _USER + "\n\ndef extra() -> None:\n    pass\n")
        for name in names:
            path = root / name
            assert session._symbol_table_for_file(path) == session.analyze_file(path).symbols


def test_workspace_symbol_entries_match_workspace_analysis(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_workspace(root)
    with WorkspaceSession(root) as session:
        session.set_overlay(root / "user.py", _USER + "\n\ndef extra() -> None:\n    pass\n")
        index, module_paths = session._workspace_symbol_entries()
        result = session.analyze_workspace()
        assert index == result.symbols
        assert module_paths == {module.module: module.path for module in result.python.modules}
        assert "extra" in {entry.qualified_name for entry in index.entries}


def test_position_conversion_reads_each_document_once() -> None:
    reads: list[str] = []

    def source_for_uri(uri: str) -> str:
        reads.append(uri)
        return "\U0001f600 = 1\n" * 10

    uri = "file:///workspace/mod.py"
    payload = [
        {
            "uri": uri,
            "range": {
                "start": {"line": line, "character": 2},
                "end": {"line": line, "character": 2},
            },
        }
        for line in range(5)
    ]
    converted = convert_payload_positions(
        payload, encoding="utf-16", to_client=True, source_for_uri=source_for_uri
    )
    assert reads == [uri]
    assert converted[4]["range"]["end"] == {"line": 4, "character": 3}


def test_position_conversion_reads_each_uri_once_per_payload() -> None:
    reads: list[str] = []
    sources = {"file:///a.py": "\U0001f600x\n" * 3, "file:///b.py": "plain\n" * 3}

    def source_for_uri(uri: str) -> str | None:
        reads.append(uri)
        return sources.get(uri)

    def edit(line: int) -> dict[str, Any]:
        return {
            "range": {
                "start": {"line": line, "character": 1},
                "end": {"line": line, "character": 2},
            },
            "newText": "y",
        }

    payload = {
        "changes": {
            "file:///a.py": [edit(0), edit(1), edit(2)],
            "file:///b.py": [edit(0), edit(1)],
            "file:///missing.py": [edit(0), edit(1)],
        },
        "folds": [
            {
                "uri": "file:///a.py",
                "startLine": line,
                "startCharacter": 1,
                "endLine": line,
                "endCharacter": 2,
            }
            for line in range(3)
        ],
    }
    converted = convert_payload_positions(
        payload, encoding="utf-16", to_client=True, source_for_uri=source_for_uri
    )
    assert sorted(reads) == ["file:///a.py", "file:///b.py", "file:///missing.py"]
    assert converted["changes"]["file:///a.py"][2]["range"]["end"] == {"line": 2, "character": 3}
    assert converted["changes"]["file:///b.py"][1]["range"]["end"] == {"line": 1, "character": 2}
    assert converted["changes"]["file:///missing.py"][0] == edit(0)
    assert converted["folds"][2]["endCharacter"] == 3


def test_position_conversion_reads_again_for_the_next_payload() -> None:
    reads: list[str] = []

    def source_for_uri(uri: str) -> str:
        reads.append(uri)
        return "x = 1\n"

    position = {"uri": "file:///mod.py", "line": 0, "character": 1}
    for _ in range(2):
        convert_payload_positions(
            position, encoding="utf-16", to_client=False, source_for_uri=source_for_uri
        )
    assert reads == ["file:///mod.py", "file:///mod.py"]
