from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations import SourcePosition, SourceRange
from pyinc.integrations.python_source import (
    DefinitionRef,
    DependencySurface,
    ImportRef,
    PythonFileAnalysis,
    PythonWorkspaceAnalysis,
    directory_analysis,
    file_analysis,
    file_analysis_payload,
    import_statements_for_file,
    imports_for_file,
    module_analysis,
    module_analysis_payload,
    module_binding_analysis_payload,
    module_export_surface,
    module_wildcard_export_surface,
    source_text,
    workspace_analysis,
    workspace_analysis_payload,
)

Operation = tuple[Literal["write", "delete"], str, str | None]


def _range(line: int, start: int, end: int) -> SourceRange:
    return SourceRange(SourcePosition(line, start), SourcePosition(line, end))


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")


def test_package_namespace_exports_only_stable_api() -> None:
    assert set(integrations.__all__) == {
        # _decoding
        "once_per_request",
        "request_inputs_changed",
        "request_scope",
        # csv_data
        "CsvAnalysis",
        "CsvColumn",
        "csv_analysis",
        "workspace_csv_analysis",
        # deep_module_resolution
        "DeepModuleResolutionAnalysis",
        "ModulePathEntry",
        "NamespacePackage",
        "PthDirective",
        "ResolvedModuleLocation",
        "deep_module_resolution_analysis",
        "resolve_module_path",
        # dependency_check
        "DependencyCheckAnalysis",
        "DependencyStatus",
        "UndeclaredImport",
        "dependency_check_analysis",
        "workspace_dependency_check",
        # env_file
        "EnvEntry",
        "EnvFileAnalysis",
        "env_analysis",
        "workspace_env_analysis",
        # installed_packages
        "ImportNameResolution",
        "InstalledPackageRef",
        "InstalledPackagesAnalysis",
        "installed_packages_analysis",
        "resolve_import_name",
        # json_config
        "JsonAnalysis",
        "JsonKey",
        "JsonSection",
        "json_analysis",
        "workspace_json_analysis",
        # notebook
        "NotebookAnalysis",
        "NotebookCell",
        "NotebookDefinition",
        "NotebookDiagnostic",
        "NotebookImport",
        "notebook_analysis",
        "workspace_notebook_analysis",
        # python_source
        "DependencySurface",
        "DefinitionRef",
        "Diagnostic",
        "ImportRef",
        "PythonFileAnalysis",
        "PythonModuleAnalysis",
        "PythonWorkspaceAnalysis",
        "ResolvedImportRef",
        "DocumentMap",
        "PositionEncoding",
        "SourcePosition",
        "SourceRange",
        "directory_analysis",
        "file_analysis",
        "module_analysis",
        "workspace_analysis",
        # requirement_evaluation
        "ApplicableRequirement",
        "ApplicableRequirementsAnalysis",
        "MarkerEvaluation",
        "PythonEnvironmentSnapshot",
        "VersionSpecifierEvaluation",
        "applicable_requirements",
        "evaluate_markers",
        "evaluate_version_specifier",
        "workspace_applicable_requirements",
        # requirements_txt
        "FileReference",
        "IndexDirective",
        "RequirementRef",
        "RequirementsAnalysis",
        "deep_requirements_analysis",
        "requirements_analysis",
        "workspace_requirements_analysis",
        # symbol_resolution
        "ClassMember",
        "ClassModel",
        "Binding",
        "ModuleSymbolTable",
        "Parameter",
        "Reference",
        "ReferenceQueryResult",
        "Scope",
        "ScopeTree",
        "Signature",
        "Symbol",
        "SymbolId",
        "WorkspaceSymbolEntry",
        "WorkspaceSymbolIndex",
        "class_model",
        "find_references",
        "module_symbol_table",
        "scope_tree",
        "symbol_at",
        "workspace_symbol_index",
        # toml_config
        "ConfigAnalysis",
        "ConfigKey",
        "ConfigSection",
        "config_analysis",
        "workspace_config_analysis",
        # xml_config
        "XmlAnalysis",
        "XmlAttribute",
        "XmlElement",
        "xml_analysis",
        "workspace_xml_analysis",
    }
    assert hasattr(integrations, "file_analysis")
    assert hasattr(integrations, "directory_analysis")
    assert hasattr(integrations, "module_analysis")
    assert hasattr(integrations, "workspace_analysis")
    assert hasattr(integrations, "PythonFileAnalysis")
    assert hasattr(integrations, "config_analysis")
    assert hasattr(integrations, "workspace_config_analysis")
    assert hasattr(integrations, "ConfigAnalysis")
    # Experimental helpers must not leak.
    assert not hasattr(integrations, "source_text")
    assert not hasattr(integrations, "dependency_check_payload")
    assert not hasattr(integrations, "imports_for_file")
    assert not hasattr(integrations, "file_analysis_payload")
    assert not hasattr(integrations, "workspace_analysis_payload")
    assert not hasattr(integrations, "config_file_text")
    assert not hasattr(integrations, "config_sections_payload")
    assert not hasattr(integrations, "config_analysis_payload")
    assert not hasattr(integrations, "module_symbol_table_payload")
    assert not hasattr(integrations, "resolve_symbol_payload")
    assert not hasattr(integrations, "workspace_symbol_index_payload")


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_analysis_reports_top_level_symbols_by_mode(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text(
        "import os\n"
        "from pkg.sub import thing\n"
        "def alpha():\n"
        "    return 1\n"
        "async def beta():\n"
        "    return 2\n"
        "class Gamma:\n"
        "    pass\n",
        encoding="utf-8",
    )

    analysis = file_analysis(Database(mode=mode), path)

    assert analysis == PythonFileAnalysis(
        path=str(path),
        imports=(
            ImportRef(module="os", kind="import", range=_range(0, 0, 9)),
            ImportRef(module="pkg.sub", kind="from", range=_range(1, 0, 25)),
        ),
        definitions=(
            DefinitionRef(name="alpha", kind="function", range=_range(2, 4, 9)),
            DefinitionRef(name="beta", kind="function", range=_range(4, 10, 14)),
            DefinitionRef(name="Gamma", kind="class", range=_range(6, 6, 11)),
        ),
        diagnostics=(),
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_analysis_reports_syntax_errors(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def broken(\n", encoding="utf-8")

    analysis = file_analysis(Database(mode=mode), path)

    assert analysis.imports == ()
    assert analysis.definitions == ()
    assert len(analysis.diagnostics) == 1
    assert analysis.diagnostics[0].code == "syntax-error"
    assert analysis.diagnostics[0].message
    assert analysis.diagnostics[0].range is not None
    assert analysis.diagnostics[0].range.start.line == 0


def test_file_analysis_reports_invalid_source_encoding(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_bytes(b"# coding: not-a-real-codec\nvalue = 1\n")

    analysis = file_analysis(Database(mode="strict"), path)

    assert analysis.imports == ()
    assert analysis.definitions == ()
    assert len(analysis.diagnostics) == 1
    assert analysis.diagnostics[0].code == "source-decode-error"
    assert "not-a-real-codec" in analysis.diagnostics[0].message


def test_file_analysis_decodes_pep263_source_and_preserves_identifier_width(
    tmp_path: Path,
) -> None:
    path = tmp_path / "latin1.py"
    path.write_bytes("# coding: latin-1\ndef café():\n    pass\n".encode("latin-1"))

    analysis = file_analysis(Database(mode="strict"), path)

    assert analysis.diagnostics == ()
    assert analysis.definitions == (
        DefinitionRef(name="café", kind="function", range=_range(1, 4, 8)),
    )


def test_definition_range_uses_decomposed_source_spelling(tmp_path: Path) -> None:
    path = tmp_path / "normalized.py"
    path.write_text("def e\u0301():\n    pass\n", encoding="utf-8")

    analysis = file_analysis(Database(mode="strict"), path)

    assert analysis.definitions == (
        DefinitionRef(name="é", kind="function", range=_range(0, 4, 6)),
    )


def test_comment_only_edit_backdates_source_and_reuses_downstream(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sample.py"
    path.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")

    first = file_analysis(db, path)
    assert first.imports == (ImportRef(module="os", kind="import", range=_range(0, 0, 9)),)

    path.write_text("import os\n# trailing comment\n", encoding="utf-8")
    second = file_analysis(db, path)

    assert second.imports == (ImportRef(module="os", kind="import", range=_range(0, 0, 9)),)
    assert db.inspect(source_text, str(path)).last_recompute == "backdated"
    assert db.inspect(imports_for_file, str(path)).last_decision == "reused"
    assert db.inspect(file_analysis_payload, str(path)).last_decision == "reused"


def test_semantic_edit_invalidates_downstream_analysis(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")

    assert file_analysis(db, path).imports == (
        ImportRef(module="os", kind="import", range=_range(0, 0, 9)),
    )

    path.write_text("import sys\n", encoding="utf-8")
    updated = file_analysis(db, path)

    assert updated.imports == (ImportRef(module="sys", kind="import", range=_range(0, 0, 10)),)
    assert db.inspect(source_text, str(path)).last_recompute == "executed"
    assert db.inspect(imports_for_file, str(path)).last_recompute == "executed"
    assert db.inspect(file_analysis_payload, str(path)).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_directory_analysis_is_non_recursive_and_sorted(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()

    (root / "b.py").write_text("import sys\n", encoding="utf-8")
    (root / "a.py").write_text("import os\n", encoding="utf-8")
    (root / "notes.txt").write_text("ignored\n", encoding="utf-8")
    (nested / "inner.py").write_text("import json\n", encoding="utf-8")

    analyses = directory_analysis(Database(mode=mode), root)

    assert tuple(Path(item.path).name for item in analyses) == ("a.py", "b.py")
    assert analyses[0].imports == (ImportRef(module="os", kind="import", range=_range(0, 0, 9)),)
    assert analyses[1].imports == (ImportRef(module="sys", kind="import", range=_range(0, 0, 10)),)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_analysis_matches_fresh_recomputation_over_edits(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    contents = (
        "import os\n",
        "import os\n# trailing comment\n",
        "import sys\n",
        "def broken(\n",
        "class Example:\n    pass\n",
    )

    incremental = Database(mode=mode)
    for content in contents:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert file_analysis(incremental, path) == file_analysis(fresh, path)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_directory_analysis_matches_fresh_recomputation_over_changes(
    mode: str, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    steps: tuple[Operation, ...] = (
        ("write", "b.py", "import sys\n"),
        ("write", "a.py", "import os\n"),
        ("write", "a.py", "import os\n# trailing comment\n"),
        ("write", "notes.txt", "ignored\n"),
        ("delete", "b.py", None),
        ("write", "c.py", "def broken(\n"),
    )

    incremental = Database(mode=mode)
    for operation, name, content in steps:
        target = root / name
        if operation == "write":
            assert content is not None
            target.write_text(content, encoding="utf-8")
        else:
            target.unlink()

        fresh = Database(mode=mode)
        assert directory_analysis(incremental, root) == directory_analysis(fresh, root)


def test_workspace_analysis_discovers_recursive_modules_and_derives_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    nested = pkg / "nested"
    nested.mkdir(parents=True)
    (root / "main.py").write_text("import pkg\n", encoding="utf-8")
    (pkg / "__init__.py").write_text("from .nested import util\n", encoding="utf-8")
    (nested / "util.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")

    analysis = workspace_analysis(Database(mode="strict"), root)

    assert analysis == PythonWorkspaceAnalysis(
        root=str(root),
        modules=(
            module_analysis(Database(mode="strict"), root, root / "main.py"),
            module_analysis(Database(mode="strict"), root, pkg / "__init__.py"),
            module_analysis(Database(mode="strict"), root, nested / "util.py"),
        ),
    )
    assert tuple(item.module for item in analysis.modules) == (
        "main",
        "pkg",
        "pkg.nested.util",
    )


def test_workspace_analysis_ignores_symlink_cycles_and_outside_workspace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    root.mkdir()
    pkg.mkdir()
    (root / "main.py").write_text("import pkg\n", encoding="utf-8")
    (pkg / "__init__.py").write_text("from .helper import util\n", encoding="utf-8")
    (pkg / "helper.py").write_text("def util() -> int:\n    return 1\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.py").write_text("value = 1\n", encoding="utf-8")

    _symlink_or_skip(root / "external_link", outside)
    _symlink_or_skip(pkg / "loop", root)

    analysis = workspace_analysis(Database(mode="strict"), root)

    assert tuple(item.module for item in analysis.modules) == (
        "main",
        "pkg",
        "pkg.helper",
    )
    assert all("external_link" not in item.path for item in analysis.modules)
    assert all(".loop." not in item.module for item in analysis.modules)


def test_workspace_analysis_reuses_when_only_outside_symlink_target_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.py").write_text("value = 1\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "external.py"
    outside_file.write_text("value = 1\n", encoding="utf-8")

    _symlink_or_skip(root / "external_link", outside)

    db = Database(mode="strict")
    first = workspace_analysis(db, root)

    outside_file.write_text("value = 2\n", encoding="utf-8")
    second = workspace_analysis(db, root)

    assert second == first
    assert db.inspect(workspace_analysis_payload, str(root)).last_decision == "reused"


def test_module_analysis_resolves_absolute_and_external_imports(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("def exported() -> int:\n    return 1\n", encoding="utf-8")
    consumer.write_text(
        "import provider\nimport os\nfrom provider import exported\n",
        encoding="utf-8",
    )

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert tuple(item.module for item in analysis.imports) == (
        "provider",
        "os",
        "provider",
    )
    assert tuple(item.resolution for item in analysis.resolved_imports) == (
        "workspace",
        "stdlib",
        "workspace",
    )
    assert analysis.resolved_imports[2].imported_name == "exported"
    assert analysis.dependencies == (
        DependencySurface(
            module="provider",
            path=str(provider),
            exports=("exported",),
        ),
    )


def test_module_analysis_resolves_relative_imports(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    helper = pkg / "helper.py"
    consumer = pkg / "consumer.py"
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    helper.write_text("def util() -> int:\n    return 1\n", encoding="utf-8")
    consumer.write_text(
        "from . import helper\nfrom .helper import util\n",
        encoding="utf-8",
    )

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert tuple(
        (item.module, item.imported_name, item.resolved_module)
        for item in analysis.resolved_imports
    ) == (
        (".", "helper", "pkg.helper"),
        (".helper", "util", "pkg.helper"),
    )
    assert analysis.dependencies == (
        DependencySurface(
            module="pkg.helper",
            path=str(helper),
            exports=("util",),
        ),
    )


def test_module_analysis_dependency_surface_tracks_reexport_aliases_and_assignments(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    root.mkdir()
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub.py").write_text("value = 1\n", encoding="utf-8")

    (root / "impl.py").write_text("def exported() -> int:\n    return 1\n", encoding="utf-8")
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "from impl import exported as alias\nimport pkg.sub as submod\nvalue = 1\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import alias\n", encoding="utf-8")

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert analysis.dependencies == (
        DependencySurface(
            module="provider",
            path=str(provider),
            exports=("alias", "submod", "value"),
        ),
    )


def test_module_analysis_wildcard_dependency_uses_static_all(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "shown = 1\n_hidden = 2\n__all__ = ['shown']\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import *\n", encoding="utf-8")

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert analysis.resolved_imports == (
        integrations.ResolvedImportRef(
            module="provider",
            kind="from",
            range=_range(0, 0, 22),
            imported_name="*",
            resolved_module="provider",
            resolved_path=str(provider),
            resolution="workspace",
            distribution_name=None,
            distribution_version=None,
        ),
    )
    assert analysis.dependencies == (
        DependencySurface(
            module="provider",
            path=str(provider),
            exports=("shown",),
        ),
    )


def test_module_analysis_wildcard_dependency_excludes_underscore_names_without_static_all(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "shown = 1\n_hidden = 2\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import *\n", encoding="utf-8")

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert analysis.dependencies == (
        DependencySurface(
            module="provider",
            path=str(provider),
            exports=("shown",),
        ),
    )


def test_module_analysis_prefers_workspace_submodule_for_package_imports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    helper = pkg / "helper.py"
    consumer = root / "consumer.py"
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    helper.write_text("def util() -> int:\n    return 1\n", encoding="utf-8")
    consumer.write_text("from pkg import helper\n", encoding="utf-8")

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert analysis.resolved_imports == (
        integrations.ResolvedImportRef(
            module="pkg",
            kind="from",
            range=_range(0, 0, 22),
            imported_name="helper",
            resolved_module="pkg.helper",
            resolved_path=str(helper),
            resolution="workspace",
            distribution_name=None,
            distribution_version=None,
        ),
    )
    assert analysis.dependencies == (
        DependencySurface(
            module="pkg.helper",
            path=str(helper),
            exports=("util",),
        ),
    )


def test_module_analysis_rejects_non_workspace_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")

    db = Database(mode="strict")
    with pytest.raises(ValueError):
        module_analysis(db, root, outside)


def test_module_analysis_marks_file_package_conflicts_as_ambiguous(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (root / "pkg.py").write_text("value = 1\n", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    submodule = pkg / "sub.py"
    submodule.write_text("def thing() -> int:\n    return 1\n", encoding="utf-8")
    consumer = root / "consumer.py"
    consumer.write_text("import pkg.sub\nfrom pkg.sub import thing\n", encoding="utf-8")

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert tuple(item.resolution for item in analysis.resolved_imports) == (
        "ambiguous",
        "ambiguous",
    )
    assert tuple(item.resolved_module for item in analysis.resolved_imports) == (
        None,
        None,
    )
    assert analysis.dependencies == ()


def test_workspace_analysis_reuses_dependents_when_provider_internal_edit_preserves_exports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("def exported() -> int:\n    return 1\n", encoding="utf-8")
    consumer.write_text("from provider import exported\n", encoding="utf-8")
    db = Database(mode="strict")

    first = workspace_analysis(db, root)

    provider.write_text("def exported() -> int:\n    return 2\n", encoding="utf-8")
    second = workspace_analysis(db, root)

    assert second == first
    assert db.inspect(source_text, str(provider)).last_recompute == "executed"
    assert db.inspect(module_export_surface, str(root), str(provider)).last_decision == "reused"
    assert db.inspect(module_analysis_payload, str(root), str(consumer)).last_decision == "reused"
    assert db.inspect(workspace_analysis_payload, str(root)).last_decision == "reused"


def test_workspace_analysis_reuses_wildcard_consumer_when_exports_do_not_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("shown = 1\n_hidden = 2\n", encoding="utf-8")
    consumer.write_text("from provider import *\n", encoding="utf-8")
    db = Database(mode="strict")

    first = workspace_analysis(db, root)

    provider.write_text("shown = 10\n_hidden = 20\n", encoding="utf-8")
    second = workspace_analysis(db, root)

    assert second == first
    assert (
        db.inspect(module_wildcard_export_surface, str(root), str(provider)).last_decision
        == "reused"
    )
    assert db.inspect(module_analysis_payload, str(root), str(consumer)).last_decision == "reused"
    assert db.inspect(workspace_analysis_payload, str(root)).last_decision == "reused"


def test_workspace_analysis_executes_dependents_when_provider_exports_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("def exported() -> int:\n    return 1\n", encoding="utf-8")
    consumer.write_text("from provider import exported\n", encoding="utf-8")
    db = Database(mode="strict")

    first = workspace_analysis(db, root)

    provider.write_text(
        "def exported() -> int:\n    return 1\n\ndef exported_two() -> int:\n    return 2\n",
        encoding="utf-8",
    )
    second = workspace_analysis(db, root)

    assert second != first
    assert (
        db.inspect(module_analysis_payload, str(root), str(provider)).last_recompute == "executed"
    )
    assert (
        db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    )
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


def test_workspace_analysis_executes_wildcard_consumer_when_wildcard_exports_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("shown = 1\n", encoding="utf-8")
    consumer.write_text("from provider import *\n", encoding="utf-8")
    db = Database(mode="strict")

    first = workspace_analysis(db, root)

    provider.write_text("shown = 1\nextra = 2\n", encoding="utf-8")
    second = workspace_analysis(db, root)

    assert second != first
    assert (
        db.inspect(module_wildcard_export_surface, str(root), str(provider)).last_recompute
        == "executed"
    )
    assert (
        db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    )
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


def test_dynamic_all_marks_wildcard_consumers_untracked(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "shown = 1\nextra = 2\n__all__ = ['shown']\n__all__ += ['extra']\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import *\n", encoding="utf-8")
    db = Database(mode="strict")

    first = workspace_analysis(db, root)
    second = workspace_analysis(db, root)

    assert second == first
    wildcard_surface = db.inspect(module_wildcard_export_surface, str(root), str(provider))
    consumer_view = db.inspect(module_analysis_payload, str(root), str(consumer))
    assert wildcard_surface.is_untracked
    assert consumer_view.is_untracked
    assert wildcard_surface.last_recompute == "executed"
    assert consumer_view.last_recompute == "executed"


def test_provider_wildcard_reexport_marks_wildcard_consumers_untracked(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "impl.py").write_text("shown = 1\n", encoding="utf-8")
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("from impl import *\n", encoding="utf-8")
    consumer.write_text("from provider import *\n", encoding="utf-8")
    db = Database(mode="strict")

    first = workspace_analysis(db, root)
    second = workspace_analysis(db, root)

    assert second == first
    wildcard_surface = db.inspect(module_wildcard_export_surface, str(root), str(provider))
    consumer_view = db.inspect(module_analysis_payload, str(root), str(consumer))
    assert wildcard_surface.is_untracked
    assert consumer_view.is_untracked
    assert wildcard_surface.last_recompute == "executed"
    assert consumer_view.last_recompute == "executed"


def test_workspace_analysis_reexecutes_consumer_when_missing_module_is_added(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    consumer = root / "consumer.py"
    consumer.write_text("from pkg import helper\n", encoding="utf-8")
    helper = pkg / "helper.py"
    db = Database(mode="strict")

    initial = workspace_analysis(db, root)
    initial_consumer = next(item for item in initial.modules if item.module == "consumer")
    assert initial_consumer.resolved_imports[0].resolution == "workspace"
    assert initial_consumer.resolved_imports[0].resolved_module == "pkg"
    assert initial_consumer.dependencies == (
        DependencySurface(
            module="pkg",
            path=str(pkg / "__init__.py"),
            exports=(),
        ),
    )

    helper.write_text("def util() -> int:\n    return 1\n", encoding="utf-8")
    updated = workspace_analysis(db, root)

    consumer_view = next(item for item in updated.modules if item.module == "consumer")
    assert consumer_view.resolved_imports[0].resolution == "workspace"
    assert consumer_view.resolved_imports[0].resolved_module == "pkg.helper"
    assert consumer_view.dependencies == (
        DependencySurface(
            module="pkg.helper",
            path=str(helper),
            exports=("util",),
        ),
    )
    assert (
        db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    )
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


def test_workspace_analysis_reexecutes_consumer_when_dependency_module_is_deleted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    helper = pkg / "helper.py"
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    helper.write_text("def util() -> int:\n    return 1\n", encoding="utf-8")
    consumer = root / "consumer.py"
    consumer.write_text("from pkg import helper\n", encoding="utf-8")
    db = Database(mode="strict")

    initial = workspace_analysis(db, root)
    consumer_view = next(item for item in initial.modules if item.module == "consumer")
    assert consumer_view.resolved_imports[0].resolution == "workspace"
    assert consumer_view.dependencies == (
        DependencySurface(
            module="pkg.helper",
            path=str(helper),
            exports=("util",),
        ),
    )

    helper.unlink()
    updated = workspace_analysis(db, root)

    updated_consumer = next(item for item in updated.modules if item.module == "consumer")
    assert updated_consumer.resolved_imports[0].resolution == "workspace"
    assert updated_consumer.resolved_imports[0].resolved_module == "pkg"
    assert updated_consumer.dependencies == (
        DependencySurface(
            module="pkg",
            path=str(pkg / "__init__.py"),
            exports=(),
        ),
    )
    assert (
        db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    )
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_workspace_analysis_matches_fresh_recomputation_over_changes(
    mode: str, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    steps: tuple[Operation, ...] = (
        ("write", "provider.py", "def exported() -> int:\n    return 1\n"),
        ("write", "consumer.py", "from provider import exported\n"),
        ("write", "provider.py", "def exported() -> int:\n    return 2\n"),
        (
            "write",
            "provider.py",
            "def exported() -> int:\n    return 2\n\ndef extra() -> int:\n    return 3\n",
        ),
        ("write", "pkg/__init__.py", ""),
        ("write", "pkg/helper.py", "def helper() -> int:\n    return 1\n"),
        (
            "write",
            "consumer.py",
            "from provider import exported\nfrom pkg import helper\n",
        ),
        ("delete", "pkg/helper.py", None),
    )

    incremental = Database(mode=mode)
    for operation, name, content in steps:
        target = root / name
        if operation == "write":
            assert content is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        else:
            target.unlink()

        fresh = Database(mode=mode)
        assert workspace_analysis(incremental, root) == workspace_analysis(fresh, root)


# ---------------------------------------------------------------------------
# Environment composition tests (python_source + installed_packages)
# ---------------------------------------------------------------------------


def test_import_resolution_classifies_stdlib_and_installed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    mod = root / "app.py"
    mod.write_text(
        "import os\nimport pytest\nimport nonexistent_xyz_abc\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")
    analysis = module_analysis(db, root, mod)
    resolutions = {r.module: r for r in analysis.resolved_imports}
    assert resolutions["os"].resolution == "stdlib"
    assert resolutions["os"].distribution_name is None
    assert resolutions["pytest"].resolution == "installed"
    assert resolutions["pytest"].distribution_name is not None
    assert resolutions["pytest"].distribution_version is not None
    assert resolutions["nonexistent_xyz_abc"].resolution == "missing"
    assert resolutions["nonexistent_xyz_abc"].distribution_name is None


def test_from_import_stdlib_resolution(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    mod = root / "app.py"
    mod.write_text("from collections import OrderedDict\n", encoding="utf-8")
    db = Database(mode="strict")
    analysis = module_analysis(db, root, mod)
    ri = analysis.resolved_imports[0]
    assert ri.resolution == "stdlib"
    assert ri.imported_name == "OrderedDict"
    assert ri.distribution_name is None


def test_workspace_import_preferred_over_environment(tmp_path: Path) -> None:
    """A workspace module named 'os' should resolve as workspace, not stdlib."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "os.py").write_text("x = 1\n", encoding="utf-8")
    consumer = root / "app.py"
    consumer.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")
    analysis = module_analysis(db, root, consumer)
    assert analysis.resolved_imports[0].resolution == "workspace"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_installed_import_resolves_to_file_via_deep_module_resolution(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: installed-package imports should have resolved_path populated."""
    site = tmp_path / "site-packages"
    site.mkdir()
    pkg_dir = site / "fake_installed"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg_dir / "submod.py").write_text("EXTRA = 2\n", encoding="utf-8")

    # Stage dist-info so environment_index classifies `fake_installed` as installed.
    dist_info = site / "fake_installed-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fake_installed\nVersion: 1.0.0\nSummary: Fake\n",
        encoding="utf-8",
    )
    (dist_info / "top_level.txt").write_text("fake_installed\n", encoding="utf-8")

    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site),),
    )
    monkeypatch.setattr(
        "pyinc.integrations.deep_module_resolution._get_sys_path_entries",
        lambda: (str(site),),
    )

    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "app.py"
    consumer.write_text(
        "import fake_installed\n"
        "from fake_installed import submod\n"
        "from fake_installed import VALUE\n",
        encoding="utf-8",
    )

    db = Database(mode=mode)
    analysis = module_analysis(db, root, consumer)
    by_module = {(r.module, r.imported_name): r for r in analysis.resolved_imports}

    top = by_module[("fake_installed", None)]
    assert top.resolution == "installed"
    assert top.distribution_name == "fake_installed"
    assert top.resolved_path is not None
    assert Path(top.resolved_path).resolve() == (pkg_dir / "__init__.py").resolve()

    submod_ref = by_module[("fake_installed", "submod")]
    assert submod_ref.resolution == "installed"
    assert submod_ref.resolved_path is not None
    assert Path(submod_ref.resolved_path).resolve() == (pkg_dir / "submod.py").resolve()

    # `from fake_installed import VALUE` (a plain symbol, not a submodule):
    # Expected to fall back to the package __init__.py since VALUE has no file.
    value_ref = by_module[("fake_installed", "VALUE")]
    assert value_ref.resolution == "installed"
    assert value_ref.resolved_path is not None
    assert Path(value_ref.resolved_path).resolve() == (pkg_dir / "__init__.py").resolve()


def test_relative_import_failure_stays_missing(tmp_path: Path) -> None:
    """Relative imports with excessive nesting are 'missing', not checked against env."""
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    consumer = pkg / "consumer.py"
    # Three levels up from pkg.consumer goes beyond the workspace root
    consumer.write_text("from ...deep import something\n", encoding="utf-8")
    db = Database(mode="strict")
    analysis = module_analysis(db, root, consumer)
    assert analysis.resolved_imports[0].resolution == "missing"
    assert analysis.resolved_imports[0].distribution_name is None


def test_resolved_import_ref_distribution_fields_none_for_non_installed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("x = 1\n", encoding="utf-8")
    consumer.write_text("import provider\nimport os\n", encoding="utf-8")
    db = Database(mode="strict")
    analysis = module_analysis(db, root, consumer)
    workspace_ref = analysis.resolved_imports[0]
    stdlib_ref = analysis.resolved_imports[1]
    assert workspace_ref.resolution == "workspace"
    assert workspace_ref.distribution_name is None
    assert workspace_ref.distribution_version is None
    assert stdlib_ref.resolution == "stdlib"
    assert stdlib_ref.distribution_name is None
    assert stdlib_ref.distribution_version is None


# --- try/except ImportError import support (v2.0.0) --------------------------


def test_import_statements_for_file_collects_try_except_import_error(
    tmp_path: Path,
) -> None:
    mod = tmp_path / "mod.py"
    mod.write_text(
        "import os\ntry:\n    import ujson\nexcept ImportError:\n    pass\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")
    stmts = import_statements_for_file(db, str(mod))
    modules = [s[0] for s in stmts]
    assert "os" in modules
    assert "ujson" in modules


def test_import_statements_for_file_collects_try_except_module_not_found(
    tmp_path: Path,
) -> None:
    mod = tmp_path / "mod.py"
    mod.write_text(
        "try:\n    from fast_lib import speed\nexcept ModuleNotFoundError:\n    speed = None\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")
    stmts = import_statements_for_file(db, str(mod))
    assert len(stmts) == 1
    assert stmts[0][0] == "fast_lib"
    assert stmts[0][1] == "from"


def test_module_binding_analysis_no_impurity_for_try_except_import_error(
    tmp_path: Path,
) -> None:
    mod = tmp_path / "mod.py"
    mod.write_text(
        "x = 1\ntry:\n    import ujson as json\nexcept ImportError:\n    pass\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")
    explicit, _wildcard, impurity = module_binding_analysis_payload(db, str(mod))
    assert "json" in explicit
    assert not any("unsupported" in r for r in impurity), f"unexpected impurity reasons: {impurity}"


def test_import_statements_for_file_collects_tuple_handler_try_block(
    tmp_path: Path,
) -> None:
    mod = tmp_path / "mod.py"
    mod.write_text(
        "try:\n"
        "    from fast_lib import Encoder\n"
        "except (ImportError, ModuleNotFoundError):\n"
        "    pass\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")
    stmts = import_statements_for_file(db, str(mod))
    collected_modules = {s[0] for s in stmts}
    assert "fast_lib" in collected_modules


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_module_swapped_for_a_directory_matches_a_fresh_database(mode: str, tmp_path: Path) -> None:
    # The workspace walk only collects regular files, so a fresh database never
    # sees the swapped module. A warm one re-probes the path it already knows,
    # and has to reach the same answer rather than the read error a directory
    # would raise: a directory is not a source file, exactly as an absent one
    # is not.
    (tmp_path / "mod.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")

    db = Database(mode=mode)
    warm = workspace_analysis(db, str(tmp_path))
    assert sorted(module.path for module in warm.modules) == [
        str(tmp_path / "mod.py"),
        str(tmp_path / "other.py"),
    ]

    (tmp_path / "mod.py").unlink()
    (tmp_path / "mod.py").mkdir()

    after = workspace_analysis(db, str(tmp_path))
    assert after == workspace_analysis(Database(mode=mode), str(tmp_path))
    assert sorted(module.path for module in after.modules) == [str(tmp_path / "other.py")]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_module_restored_after_a_directory_swap_matches_a_fresh_database(
    mode: str, tmp_path: Path
) -> None:
    (tmp_path / "mod.py").write_text("import os\n", encoding="utf-8")

    db = Database(mode=mode)
    assert workspace_analysis(db, str(tmp_path)).modules != ()

    (tmp_path / "mod.py").unlink()
    (tmp_path / "mod.py").mkdir()
    assert workspace_analysis(db, str(tmp_path)).modules == ()

    (tmp_path / "mod.py").rmdir()
    (tmp_path / "mod.py").write_text("import sys\n", encoding="utf-8")
    restored = workspace_analysis(db, str(tmp_path))
    assert restored == workspace_analysis(Database(mode=mode), str(tmp_path))
    assert tuple(ref.module for ref in restored.modules[0].imports) == ("sys",)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_package_swapped_for_a_module_matches_a_fresh_database(mode: str, tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("import os\n", encoding="utf-8")

    db = Database(mode=mode)
    assert len(workspace_analysis(db, str(tmp_path)).modules) == 1

    (package / "__init__.py").unlink()
    package.rmdir()
    package.write_text("import sys\n", encoding="utf-8")

    after = workspace_analysis(db, str(tmp_path))
    assert after == workspace_analysis(Database(mode=mode), str(tmp_path))
