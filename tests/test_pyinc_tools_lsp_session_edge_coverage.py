from __future__ import annotations

import ast
import os
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _hostile_paths import within_budget

import pyinc_tools.lsp as lsp
import pyinc_tools.session as session_module
from pyinc.integrations import (
    DependencyCheckAnalysis,
    DependencySurface,
    PythonModuleAnalysis,
    ResolvedImportRef,
    SourcePosition,
    SourceRange,
    Symbol,
    SymbolId,
)
from pyinc_tools._document import InvalidParams
from pyinc_tools._models import AnalysisDiagnostic, DependencyInputs, ResolvedTarget
from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import CallHierarchyItem, TypeHierarchyItem, WorkspaceSession


def _range() -> SourceRange:
    return SourceRange(SourcePosition(0, 0), SourcePosition(0, 1))


def test_lsp_package_version_falls_back_when_distribution_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(lsp, "version", missing)
    assert lsp._package_version() == "0+unknown"


def test_uri_and_identifier_helpers_reject_invalid_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported URI scheme"):
        lsp._uri_to_path("https://example.test/mod.py")

    assert lsp._identifier_span_at_position("value", -1, 0) is None
    assert lsp._identifier_span_at_position("value", 0, 9) is None
    assert lsp._identifier_span_at_position(" + ", 0, 1) is None
    assert lsp._identifier_at_position("value", 0, 2) == "value"
    assert lsp._path_to_uri(str(tmp_path)).startswith("file:")


def test_symbol_formatting_handles_reexports_annotations_and_plain_names() -> None:
    imported = Symbol("alias", "from_import_alias", _range(), None, None, "pkg", "value")
    annotated = Symbol("count", "variable", _range(), "int", None, None, None)
    plain = Symbol("value", "variable", _range(), None, None, None, None)

    assert "re-exported from" in lsp._format_hover_markdown(imported)
    assert lsp._format_symbol_declaration(annotated) == "count: int"
    assert lsp._format_symbol_declaration(plain) == "value"


def test_hierarchy_payload_helpers_handle_missing_details_and_invalid_identities(
    tmp_path: Path,
) -> None:
    call_item = CallHierarchyItem(
        name="run",
        kind="function",
        path=str(tmp_path / "mod.py"),
        qualified_name="run",
        detail=None,
        range=_range(),
        selection_range=_range(),
    )
    type_item = TypeHierarchyItem(
        name="Box",
        kind="class",
        path=str(tmp_path / "mod.py"),
        qualified_name="Box",
        detail=None,
        range=_range(),
        selection_range=_range(),
    )
    assert "detail" not in lsp._call_hierarchy_item_to_lsp(call_item)
    assert "detail" not in lsp._type_hierarchy_item_to_lsp(type_item)

    invalid_items: list[object] = [
        None,
        {},
        {"data": None},
        {"data": {}},
        {"data": {"path": 1, "qualified_name": "x"}},
    ]
    for item in invalid_items:
        assert lsp._call_hierarchy_identity_from_item(item) is None
        assert lsp._type_hierarchy_identity_from_item(item) is None


def test_handle_message_maps_request_failures_and_ignores_bad_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = LanguageServer()
    errors: list[tuple[object, int, str]] = []
    monkeypatch.setattr(
        server,
        "_send_error",
        lambda request_id, code, message: errors.append((request_id, code, message)),
    )

    def rejected(_method: str, _params: object) -> None:
        raise lsp._RequestFailed("rejected")

    monkeypatch.setattr(server, "_handle_request", rejected)
    assert server._handle_message({"jsonrpc": "2.0", "id": 7, "method": "rename"})
    assert errors == [(7, lsp._LSP_REQUEST_FAILED, "rejected")]

    monkeypatch.setattr(
        server,
        "_handle_notification",
        lambda _method, _params: (_ for _ in ()).throw(InvalidParams("bad")),
    )
    assert server._handle_message({"jsonrpc": "2.0", "method": "changed"})


class _MissingSession:
    def __init__(self, root: Path, *, source: str | None = "value") -> None:
        self.root = str(root)
        self.source = source

    def source_text(self, _path: str) -> str | None:
        return self.source

    def analyze_workspace(self) -> SimpleNamespace:
        return SimpleNamespace(diagnostics=())

    def import_edits_for_file_renames(self, _renames: object) -> tuple[()]:
        return ()

    def import_edits_for_file_deletions(self, _deletions: object) -> tuple[()]:
        return ()

    def __getattr__(self, _name: str) -> Any:
        def missing(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError

        return missing


def _server_with_session(root: Path, *, source: str | None = "value") -> LanguageServer:
    server = LanguageServer(default_root=str(root))
    server._initialized = True
    server._session = _MissingSession(root, source=source)  # type: ignore[assignment]
    return server


def _text_document_params(path: Path) -> dict[str, Any]:
    return {
        "textDocument": {"uri": path.as_uri()},
        "position": {"line": 0, "character": 1},
        "positions": [{"line": 0, "character": 1}],
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
        "context": {},
        "newName": "renamed",
    }


@pytest.mark.parametrize(
    ("handler_name", "expected"),
    [
        ("_hover", None),
        ("_completion", {"isIncomplete": False, "items": []}),
        ("_definition", []),
        ("_declaration", []),
        ("_type_definition", []),
        ("_references", []),
        ("_document_highlight", []),
        ("_linked_editing_range", None),
        ("_prepare_rename", None),
        ("_rename", None),
        ("_signature_help", None),
        ("_folding_range", []),
        (
            "_selection_range",
            [
                {
                    "range": {
                        "start": {"line": 0, "character": 1},
                        "end": {"line": 0, "character": 1},
                    }
                }
            ],
        ),
        ("_document_link", []),
        ("_code_lens", []),
        ("_prepare_call_hierarchy", None),
        ("_prepare_type_hierarchy", None),
        ("_inlay_hint", []),
        ("_semantic_tokens_full", {"data": []}),
        ("_semantic_tokens_range", {"data": []}),
    ],
)
def test_lsp_handlers_translate_missing_files_to_empty_results(
    handler_name: str, expected: object, tmp_path: Path
) -> None:
    target = tmp_path / "mod.py"
    server = _server_with_session(tmp_path)
    try:
        handler = getattr(server, handler_name)
        assert handler(_text_document_params(target)) == expected
    finally:
        server._session = None


@pytest.mark.parametrize(
    ("handler_name", "expected"),
    [
        ("_declaration", []),
        ("_type_definition", []),
        ("_references", []),
        ("_document_highlight", []),
        ("_linked_editing_range", None),
        ("_prepare_rename", None),
        ("_rename", None),
    ],
)
def test_lsp_navigation_handlers_reject_missing_source(
    handler_name: str, expected: object, tmp_path: Path
) -> None:
    server = _server_with_session(tmp_path, source=None)
    try:
        assert getattr(server, handler_name)(_text_document_params(tmp_path / "mod.py")) == expected
    finally:
        server._session = None


@pytest.mark.parametrize(
    "handler_name",
    [
        "_call_hierarchy_incoming_calls",
        "_call_hierarchy_outgoing_calls",
        "_type_hierarchy_supertypes",
        "_type_hierarchy_subtypes",
    ],
)
def test_hierarchy_handlers_translate_missing_files_to_none(
    handler_name: str, tmp_path: Path
) -> None:
    server = _server_with_session(tmp_path)
    params = {"item": {"data": {"path": str(tmp_path / "mod.py"), "qualified_name": "name"}}}
    try:
        assert getattr(server, handler_name)(params) is None
    finally:
        server._session = None


def test_notification_lifecycle_and_diagnostic_cache_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = LanguageServer()
    with pytest.raises(InvalidParams, match="must be an object"):
        server._handle_notification("initialized", [])
    assert server._handle_notification("unknown", {})

    server._initialized = True
    published: list[bool] = []
    monkeypatch.setattr(server, "publish_workspace_diagnostics", lambda: published.append(True))
    assert server._handle_notification("initialized", {})
    assert published == [True]

    server = _server_with_session(tmp_path)
    stale = str(tmp_path / "stale.py")
    server._published_paths = {stale}
    server._published_signatures = {stale: ()}
    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        server, "_send_notification", lambda method, params: sent.append((method, params))
    )
    server.publish_workspace_diagnostics()
    assert sent == []
    assert server._published_paths == set()
    assert server._published_signatures == {}
    server._session = None


def test_initialize_cleans_up_after_session_construction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = LanguageServer(default_root=str(tmp_path))
    cleaned: list[bool] = []
    monkeypatch.setattr(
        lsp,
        "WorkspaceSession",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad workspace")),
    )
    monkeypatch.setattr(server, "_teardown_session", lambda: cleaned.append(True))

    with pytest.raises(ValueError, match="bad workspace"):
        server._initialize({"initializationOptions": {"pyinc.watcher.enabled": False}})
    assert cleaned == [True]


def test_file_operation_handlers_ignore_malformed_and_unsafe_entries(tmp_path: Path) -> None:
    server = _server_with_session(tmp_path)
    try:
        assert (
            server._will_rename_files(
                {
                    "files": [
                        None,
                        {},
                        {"oldUri": 1, "newUri": 2},
                        {
                            "oldUri": "https://example.test/old.py",
                            "newUri": "https://example.test/new.py",
                        },
                    ]
                }
            )
            is None
        )
        assert (
            server._will_delete_files(
                {"files": [None, {}, {"uri": 1}, {"uri": "https://example.test/mod.py"}]}
            )
            is None
        )
    finally:
        server._session = None


def test_session_requirement_and_source_helpers_without_initialized_session(tmp_path: Path) -> None:
    server = LanguageServer()
    with pytest.raises(RuntimeError, match="not been initialized"):
        server._require_session()
    assert server._default_uri(None) is None
    assert server._source_for_uri((tmp_path / "mod.py").as_uri()) is None

    server = _server_with_session(tmp_path)
    try:
        assert server._source_for_uri("https://example.test/mod.py") is None
    finally:
        server._session = None


def test_workspace_session_rejects_invalid_roots_and_close_is_idempotent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an existing workspace"):
        WorkspaceSession(tmp_path / "missing")
    regular_file = tmp_path / "file"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="not an existing workspace"):
        WorkspaceSession(regular_file)

    session = WorkspaceSession(tmp_path)
    session.close()
    session.close()


def test_workspace_refresh_deduplicates_paths_and_preserves_overlays(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("disk = 1\n", encoding="utf-8")
    with WorkspaceSession(tmp_path) as session:
        session.set_overlay(target, "overlay = 2\n")
        target.write_text("disk = 3\n", encoding="utf-8")

        assert session.refresh_paths([target, target]) == (str(target),)
        assert session.source_text(target) == "overlay = 2\n"


def test_workspace_source_text_answers_a_pipe_rather_than_waiting_on_it(tmp_path: Path) -> None:
    # A workspace is a directory the editor pointed at, so anything at all can
    # be sitting inside it. Reading a source decodes it, which means opening it
    # and waiting for bytes -- and a pipe with no writer never sends one. The
    # kind is asked first so a source that is not a file reads as no source,
    # which is the same answer this already gave for one that cannot be decoded.
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable on this platform")
    target = tmp_path / "mod.py"
    target.write_text("value = 1\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        assert session.source_text(target) == "value = 1\n"

        target.unlink()
        os.mkfifo(target)

        assert within_budget(lambda: session.source_text(target)) == "returned"
        assert session.source_text(target) is None


def test_workspace_navigation_methods_reject_non_python_targets(tmp_path: Path) -> None:
    text = tmp_path / "data.txt"
    text.write_text("value", encoding="utf-8")
    symbol = SymbolId(str(text), "module", "value", _range())
    position = SourcePosition(0, 0)

    with WorkspaceSession(tmp_path) as session:
        calls: list[Callable[[], object]] = [
            lambda: session.symbol_at(text, position),
            lambda: session._local_symbol_at(text, position),
            lambda: session._local_binding_at(text, position),
            lambda: session.find_references(symbol),
            lambda: session._find_references_by_name(text, "value"),
            lambda: session.find_document_highlights(text, symbol),
            lambda: session.signature_help_at(text, 0, 0),
        ]
        for call in calls:
            with pytest.raises(FileNotFoundError):
                call()


def test_source_dependent_workspace_features_return_empty_when_source_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mod.py"
    target.write_text("class Box:\n    pass\n", encoding="utf-8")
    with WorkspaceSession(tmp_path) as session:
        monkeypatch.setattr(session, "source_text", lambda _path: None)
        assert session.folding_ranges_for_file(target) == ()
        assert session.selection_ranges_at(target, 0, 0) == ()
        assert session.document_links_for_file(target) == ()
        assert session.semantic_tokens_for_file(target) == ()
        assert session.call_hierarchy_outgoing_calls(target, "Box") == ()
        assert session.type_hierarchy_subtypes(target, "Box") == ()


def _diagnostic(code: str = "unused-import") -> AnalysisDiagnostic:
    return AnalysisDiagnostic(
        path="/workspace/mod.py",
        code=code,
        message="detail",
        severity="warning",
        source="test",
        range=_range(),
    )


def test_code_action_helpers_handle_wrong_nodes_and_incomplete_ast_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WorkspaceSession.__new__(WorkspaceSession)
    import_node = ast.parse("import one").body[0]
    from_node = ast.parse("from pkg import one").body[0]
    assert isinstance(import_node, ast.Import)
    assert isinstance(from_node, ast.ImportFrom)

    assert (
        session._unused_import_actions("mod.py", "import one\n", import_node, _diagnostic()) == []
    )
    assert (
        session._unresolved_symbol_actions(
            "mod.py",
            "/mirror/mod.py",
            "import one\n",
            import_node,
            _diagnostic("unresolved-symbol"),
        )
        == []
    )

    unmatched = AnalysisDiagnostic(
        path="mod.py",
        code="unused-import",
        message="detail",
        severity="hint",
        source="test",
        range=SourceRange(SourcePosition(9, 0), SourcePosition(9, 1)),
    )
    assert (
        session._unused_import_actions("mod.py", "from pkg import one\n", from_node, unmatched)
        == []
    )

    from_node.end_lineno = None
    assert session._whole_statement_edit("mod.py", "from pkg import one", from_node) is None
    assert session._alias_removal_edits("mod.py", "from pkg import one", from_node, 0) == []

    assert (
        session._missing_import_actions(
            "mod.py", None, "import one\n", import_node, _diagnostic("missing-import")
        )
        == []
    )
    module = PythonModuleAnalysis("mod.py", "mod", (), (), (), (), ())
    assert (
        session._missing_import_actions(
            "mod.py", module, "import one\n", import_node, _diagnostic("missing-import")
        )
        == []
    )

    star_node = ast.parse("from pkg import *").body[0]
    assert isinstance(star_node, ast.ImportFrom)
    session.db = object()  # type: ignore[assignment]
    session.mirror_root = "/mirror"
    assert (
        session._unresolved_symbol_actions(
            "mod.py",
            "/mirror/mod.py",
            "from pkg import *\n",
            star_node,
            _diagnostic("unresolved-symbol"),
        )
        == []
    )

    monkeypatch.setattr(
        session_module,
        "_resolve_target",
        lambda *_args: ResolvedTarget(
            "mod", "one", "workspace", "pkg", "/pkg.py", _range(), None, None, 0, ()
        ),
    )
    from_node = ast.parse("from pkg import one").body[0]
    assert isinstance(from_node, ast.ImportFrom)
    assert (
        session._unresolved_symbol_actions(
            "mod.py",
            "/mirror/mod.py",
            "from pkg import one\n",
            from_node,
            _diagnostic("unresolved-symbol"),
        )
        == []
    )

    multi = ast.parse("from pkg import one, two").body[0]
    assert isinstance(multi, ast.ImportFrom)
    assert (
        session._retarget_from_module_action(
            "mod.py", "from pkg import one, two\n", multi, "one", _diagnostic()
        )
        is None
    )


def test_import_deletion_helpers_handle_no_matches_and_missing_statement_geometry() -> None:
    session = WorkspaceSession.__new__(WorkspaceSession)
    import_node = ast.parse("import one").body[0]
    from_node = ast.parse("from pkg import *").body[0]
    assert isinstance(import_node, ast.Import)
    assert isinstance(from_node, ast.ImportFrom)

    assert (
        session._delete_edits_for_import(
            importer_path="mod.py", source="import one\n", node=import_node, deleted_modules=set()
        )
        == []
    )
    import_node.end_lineno = None
    assert (
        session._delete_edits_for_import(
            importer_path="mod.py", source="import one", node=import_node, deleted_modules={"one"}
        )
        == []
    )

    assert (
        session._delete_edits_for_from_aliases(
            importer_path="mod.py",
            source="from pkg import *\n",
            node=from_node,
            resolved_module="pkg",
            deleted_modules=set(),
        )
        == []
    )
    one_only = ast.parse("from pkg import one").body[0]
    assert isinstance(one_only, ast.ImportFrom)
    one_only.end_lineno = None
    assert (
        session._delete_edits_for_from_aliases(
            importer_path="mod.py",
            source="from pkg import one",
            node=one_only,
            resolved_module="pkg",
            deleted_modules={"pkg.one"},
        )
        == []
    )


def _resolved_import(
    resolution: str,
    *,
    imported_name: str | None = None,
    distribution_name: str | None = None,
) -> ResolvedImportRef:
    return ResolvedImportRef(
        module="dep",
        kind="from",
        range=_range(),
        imported_name=imported_name,
        resolved_module="dep",
        resolved_path="/workspace/dep.py",
        resolution=resolution,  # type: ignore[arg-type]
        distribution_name=distribution_name,
        distribution_version="1",
    )


def test_module_diagnostics_cover_ambiguous_imports_dependencies_and_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mod.py"
    target.write_text("from dep import value\n", encoding="utf-8")
    module = PythonModuleAnalysis(
        str(target),
        "mod",
        (),
        (),
        (),
        (
            _resolved_import("ambiguous"),
            _resolved_import("installed", distribution_name="distribution"),
            _resolved_import("workspace", imported_name="value"),
        ),
        (DependencySurface("dep", str(tmp_path / "dep.py"), ("value",)),),
    )
    check = DependencyCheckAnalysis((), (), ())
    ambiguous = ResolvedTarget("mod", "value", "ambiguous", None, None, None, None, None, 0, ())

    with WorkspaceSession(tmp_path) as session:
        monkeypatch.setattr(session_module, "_resolve_target", lambda *_args: ambiguous)
        monkeypatch.setattr(session, "_unused_import_diagnostics", lambda *_args: [])
        diagnostics = session._module_diagnostics(
            str(target), str(session._mirror_path_for_real(str(target))), module, check
        )

    assert {item.code for item in diagnostics} == {
        "ambiguous-import",
        "undeclared-import",
        "ambiguous-symbol",
    }


def test_hierarchy_item_builders_and_signature_helpers_handle_unusable_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid = tmp_path / "invalid.py"
    invalid.write_text("def broken(", encoding="utf-8")
    plain = tmp_path / "plain.py"
    plain.write_text("value = 1\n", encoding="utf-8")
    valid = tmp_path / "valid.py"
    valid.write_text("class Box:\n    pass\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        assert session._build_type_hierarchy_item(str(tmp_path / "missing.py"), "Box", None) is None
        assert session._build_type_hierarchy_item(str(invalid), "Box", None) is None
        assert session._build_type_hierarchy_item(str(plain), "Box", None) is None
        monkeypatch.setattr(session, "_locate_def_class_name_offsets", lambda *_args: None)
        assert session._build_type_hierarchy_item(str(valid), "Box", None) is None

        assert session._build_call_hierarchy_item(str(tmp_path / "missing.py"), "run", None) is None
        assert session._build_call_hierarchy_item(str(invalid), "run", None) is None
        assert session._build_call_hierarchy_item(str(plain), "run", None) is None

        subscript = ast.parse("factory[T]()", mode="eval").body
        constant = ast.Constant(value=1)
        assert session._resolve_call_target(Path(session.mirror_root), subscript) is None
        assert session._resolve_call_target(Path(session.mirror_root), constant) is None

        incomplete = ResolvedTarget("mod", "run", "workspace", None, None, None, None, None, 0, ())
        assert session._lookup_callable_signature(incomplete) is None
        assert session._signature_defaults(incomplete, "run") is None
        unavailable = ResolvedTarget(
            "mod",
            "run",
            "workspace",
            "mod",
            str(tmp_path / "missing.py"),
            _range(),
            None,
            None,
            0,
            (),
        )
        assert session._lookup_callable_signature(unavailable) is None
        assert session._signature_defaults(unavailable, "run") is None


def test_build_file_result_for_non_python_path_has_no_module(tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    target.write_text("data", encoding="utf-8")
    check = DependencyCheckAnalysis((), (), ())
    inputs = DependencyInputs(None, None, ())
    with WorkspaceSession(tmp_path) as session:
        result = session._build_file_result(str(target), inputs, check)
    assert result.module is None
    assert result.symbols is None
