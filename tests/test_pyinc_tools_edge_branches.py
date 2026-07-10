from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import pyinc_tools._analysis as analysis
from pyinc import Database
from pyinc.integrations import (
    ConfigAnalysis,
    DependencyCheckAnalysis,
    DependencyStatus,
    DependencySurface,
    ModuleSymbolTable,
    PythonModuleAnalysis,
    RequirementRef,
    RequirementsAnalysis,
    ResolvedImportRef,
    SourcePosition,
    SourceRange,
    Symbol,
)
from pyinc_tools._models import DependencyInputs
from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import WorkspaceSession


def _range(start: int = 0, end: int = 1) -> SourceRange:
    return SourceRange(SourcePosition(0, start), SourcePosition(0, end))


@pytest.mark.parametrize(
    ("source", "line", "character", "expected"),
    [
        ("alpha = 1\n", 0, 0, "alpha"),
        ("alpha = 1\n", 0, 5, "alpha"),
        ("alpha = 1\n", 0, 6, None),
        ("123 = 1\n", 0, 1, None),
        ("_private = 1\n", 0, 4, "_private"),
        ("alpha = 1\n", -1, 0, None),
        ("alpha = 1\n", 1, 0, None),
        ("alpha = 1\n", 0, -1, None),
        ("alpha = 1\n", 0, 12, None),
    ],
)
def test_identifier_at_source_position_validates_and_expands_identifier(
    source: str, line: int, character: int, expected: str | None
) -> None:
    assert analysis._identifier_at_source_position(source, line, character) == expected


def _wildcard_import(path: str | None, module: str) -> ResolvedImportRef:
    return ResolvedImportRef(
        module=module,
        kind="from",
        range=_range(),
        imported_name="*",
        resolved_module=module,
        resolved_path=path,
        resolution="workspace",
        distribution_name=None,
        distribution_version=None,
    )


def _module(
    path: str,
    module: str,
    *,
    imports: tuple[ResolvedImportRef, ...] = (),
    dependencies: tuple[DependencySurface, ...] = (),
) -> PythonModuleAnalysis:
    return PythonModuleAnalysis(
        path=path,
        module=module,
        imports=(),
        definitions=(),
        diagnostics=(),
        resolved_imports=imports,
        dependencies=dependencies,
    )


def _table(
    path: str,
    module: str,
    *,
    symbols: tuple[Symbol, ...] = (),
    impurity_reasons: tuple[str, ...] = (),
) -> ModuleSymbolTable:
    return ModuleSymbolTable(module, path, symbols, impurity_reasons)


def test_resolve_target_filters_unusable_wildcard_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = "/workspace/consumer.py"
    dynamic = "/workspace/dynamic.py"
    absent_dependency = "/workspace/no_dependency.py"
    missing_symbol = "/workspace/missing_symbol.py"
    imports = (
        replace(_wildcard_import("/not-used.py", "named"), imported_name="named"),
        _wildcard_import(None, "unresolved"),
        _wildcard_import(dynamic, "dynamic"),
        _wildcard_import(absent_dependency, "no_dependency"),
        _wildcard_import(missing_symbol, "missing_symbol"),
    )
    modules = {
        consumer: _module(
            consumer,
            "consumer",
            imports=imports,
            dependencies=(DependencySurface("missing_symbol", missing_symbol, ("wanted",)),),
        ),
        dynamic: _module(dynamic, "dynamic"),
        absent_dependency: _module(absent_dependency, "no_dependency"),
        missing_symbol: _module(missing_symbol, "missing_symbol"),
    }
    tables = {
        consumer: _table(consumer, "consumer"),
        dynamic: _table(dynamic, "dynamic", impurity_reasons=("dynamic __all__",)),
        absent_dependency: _table(absent_dependency, "no_dependency"),
        missing_symbol: _table(missing_symbol, "missing_symbol"),
    }
    monkeypatch.setattr(analysis, "module_analysis", lambda _db, _root, path: modules[path])
    monkeypatch.setattr(analysis, "module_symbol_table", lambda _db, _root, path: tables[path])
    monkeypatch.setattr(
        analysis, "_resolve_at_known_positions", lambda _db, _root, _path, _name, _symbol: None
    )

    result = analysis.resolve_target(cast(Database, object()), "/workspace", consumer, "wanted")

    assert result.resolution == "ambiguous"


def test_resolve_target_accepts_one_exporting_wildcard_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = "/workspace/consumer.py"
    provider = "/workspace/provider.py"
    symbol = Symbol(
        qualified_name="wanted",
        kind="function",
        range=_range(4, 10),
        annotation=None,
        signature=None,
        import_source_module=None,
        import_source_name=None,
    )
    modules = {
        consumer: _module(
            consumer,
            "consumer",
            imports=(_wildcard_import(provider, "provider"),),
            dependencies=(DependencySurface("provider", provider, ("wanted",)),),
        ),
        provider: _module(provider, "provider"),
    }
    tables = {
        consumer: _table(consumer, "consumer"),
        provider: _table(provider, "provider", symbols=(symbol,)),
    }
    monkeypatch.setattr(analysis, "module_analysis", lambda _db, _root, path: modules[path])
    monkeypatch.setattr(analysis, "module_symbol_table", lambda _db, _root, path: tables[path])
    monkeypatch.setattr(
        analysis, "_resolve_at_known_positions", lambda _db, _root, _path, _name, _symbol: None
    )

    result = analysis.resolve_target(cast(Database, object()), "/workspace", consumer, "wanted")

    assert result.resolution == "workspace"
    assert result.defining_path == provider
    assert result.range == _range(4, 10)


def _session_without_workspace() -> WorkspaceSession:
    return WorkspaceSession.__new__(WorkspaceSession)


def test_dependency_status_diagnostics_route_to_requirement_and_config_sources() -> None:
    session = _session_without_workspace()
    requirement_range = SourceRange(SourcePosition(2, 0), SourcePosition(2, 12))
    requirements = RequirementsAnalysis(
        path="/workspace/requirements.txt",
        requirements=(
            RequirementRef(
                name="missing-dep",
                raw_line="missing-dep>=1",
                range=requirement_range,
                extras=(),
                version_spec=">=1",
                markers="",
                is_editable=False,
            ),
        ),
        file_references=(),
        index_directives=(),
        diagnostics=(("requirements-warning", "requirements detail"),),
    )
    config = ConfigAnalysis(
        path="/workspace/pyproject.toml",
        sections=(),
        dependencies=(),
        optional_dependency_groups=(),
        tool_configs=(),
        diagnostics=(),
    )
    inputs = DependencyInputs(config, requirements, ())
    check = DependencyCheckAnalysis(
        statuses=(
            DependencyStatus("ok", ">=1", "2", "satisfied", ""),
            DependencyStatus("missing-dep", ">=1", "", "missing", "not installed"),
            DependencyStatus("other-dep", ">=2", "1", "version_mismatch", "1 is too old"),
        ),
        undeclared_imports=(),
        diagnostics=(("dependency-warning", "dependency detail"),),
    )

    diagnostics = session._dependency_status_diagnostics(inputs, check)

    by_code = {item.code: item for item in diagnostics}
    assert by_code["requirements-warning"].severity == "error"
    assert by_code["dependency-missing"].path == requirements.path
    assert by_code["dependency-missing"].range == requirement_range
    assert by_code["dependency-version_mismatch"].path == config.path
    assert "does not match installed version '1'" in by_code["dependency-version_mismatch"].message
    assert by_code["dependency-warning"].path == requirements.path
    assert session._dedupe_diagnostics((diagnostics[0], diagnostics[0])) == (diagnostics[0],)

    config_only = session._dependency_status_diagnostics(
        DependencyInputs(config, None, ()),
        DependencyCheckAnalysis((), (), (("config-check", "config detail"),)),
    )
    assert config_only[0].path == config.path


def test_dependency_status_diagnostics_honor_path_filter_and_missing_sources() -> None:
    session = _session_without_workspace()
    requirements = RequirementsAnalysis(
        path="/workspace/requirements.txt",
        requirements=(RequirementRef("dep", "dep", _range(), (), "", "", False),),
        file_references=(),
        index_directives=(),
        diagnostics=(("requirements-warning", "detail"),),
    )
    config = ConfigAnalysis("/workspace/pyproject.toml", (), (), (), (), ())
    check = DependencyCheckAnalysis(
        statuses=(DependencyStatus("dep", "", "", "missing", ""),),
        undeclared_imports=(),
        diagnostics=(("dependency-warning", "detail"),),
    )

    assert (
        session._dependency_status_diagnostics(
            DependencyInputs(config, requirements, ()), check, only_path="/workspace/unrelated.py"
        )
        == ()
    )
    assert session._dependency_status_diagnostics(DependencyInputs(None, None, ()), check) == ()
    assert (
        session._dependency_status_diagnostics(
            DependencyInputs(None, None, ()), DependencyCheckAnalysis((), (), ())
        )
        == ()
    )
    assert (
        session._dependency_status_message(
            DependencyStatus("unclear", "name @ url", "", "ambiguous", "cannot evaluate")
        )
        == "Declared dependency 'unclear' could not be evaluated: 'cannot evaluate'"
    )


def test_file_deletion_coalesces_adjacent_aliases_into_one_edit(tmp_path: Path) -> None:
    for name in ("one", "two", "three", "four"):
        (tmp_path / f"{name}.py").write_text(f"value = {name!r}\n", encoding="utf-8")
    importer = tmp_path / "user.py"
    source = "import one, two, three, four\n"
    importer.write_text(source, encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        edits = session.import_edits_for_file_deletions(
            [tmp_path / "two.py", tmp_path / "three.py"]
        )

    importer_edits = [edit for edit in edits if edit.path == str(importer)]
    assert len(importer_edits) == 1
    edit = importer_edits[0]
    assert edit.range.start == SourcePosition(0, 12)
    assert edit.range.end == SourcePosition(0, 24)
    repaired = source[: edit.range.start.character] + source[edit.range.end.character :]
    assert repaired == "import one, four\n"


class _NotificationSession:
    def __init__(self, root: Path) -> None:
        self.root = str(root)
        self.cleared: list[str] = []
        self.refreshed: list[tuple[str, ...]] = []

    def source_text(self, _path: str) -> None:
        return None

    def clear_overlay(self, path: str) -> None:
        self.cleared.append(path)

    def refresh_paths(self, paths: list[str]) -> tuple[str, ...]:
        refreshed = tuple(paths)
        self.refreshed.append(refreshed)
        return refreshed


def test_lsp_close_and_watched_file_notifications_refresh_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("pass\n", encoding="utf-8")
    fake_session = _NotificationSession(tmp_path)
    server = LanguageServer(default_root=str(tmp_path))
    server._initialized = True
    server._session = fake_session  # type: ignore[assignment]
    published: list[bool] = []
    server.publish_workspace_diagnostics = lambda: published.append(True)  # type: ignore[method-assign]
    try:
        assert server._handle_notification(
            "textDocument/didClose", {"textDocument": {"uri": path.as_uri()}}
        )
        assert fake_session.cleared == [str(path)]

        assert server._handle_notification(
            "workspace/didChangeWatchedFiles",
            {"changes": [{}, {"uri": path.as_uri()}]},
        )
        assert fake_session.refreshed == [(str(path),)]

        assert server._handle_notification("workspace/didChangeWatchedFiles", {"changes": []})
        assert server._handle_notification("unknown/notification", {})
        assert len(published) == 2
    finally:
        server._session = None


def test_lsp_workspace_root_fallback_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_uri = tmp_path / "root-uri"
    folder = tmp_path / "folder"
    root_path = tmp_path / "root-path"
    default = tmp_path / "default"
    cwd = tmp_path / "cwd"
    for path in (root_uri, folder, root_path, default, cwd):
        path.mkdir()

    server = LanguageServer(default_root=str(default))
    assert server._workspace_root_from_params(
        {
            "rootUri": root_uri.as_uri(),
            "workspaceFolders": [{"uri": folder.as_uri()}],
            "rootPath": str(root_path),
        }
    ) == str(root_uri)
    assert server._workspace_root_from_params(
        {"workspaceFolders": [{"uri": folder.as_uri()}], "rootPath": str(root_path)}
    ) == str(folder)
    assert server._workspace_root_from_params(
        {"workspaceFolders": [None], "rootPath": str(root_path)}
    ) == str(root_path)
    assert server._workspace_root_from_params({"workspaceFolders": []}) == str(default)

    monkeypatch.chdir(cwd)
    without_default = LanguageServer()
    assert without_default._workspace_root_from_params(None) == str(cwd)
