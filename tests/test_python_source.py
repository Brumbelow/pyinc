from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import pyfoundinc.integrations as integrations
from pyfoundinc import Database
from pyfoundinc.integrations.python_source import (
    DefinitionRef,
    DependencySurface,
    ImportRef,
    PythonFileAnalysis,
    PythonWorkspaceAnalysis,
    directory_analysis,
    file_analysis,
    file_analysis_payload,
    imports_for_file,
    module_analysis,
    module_analysis_payload,
    module_export_surface,
    module_wildcard_export_surface,
    source_text,
    workspace_analysis,
    workspace_analysis_payload,
)

Operation = tuple[Literal["write", "delete"], str, str | None]


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")


def test_package_namespace_exports_only_stable_python_source_api() -> None:
    assert set(integrations.__all__) == {
        "DependencySurface",
        "DefinitionRef",
        "Diagnostic",
        "ImportRef",
        "PythonFileAnalysis",
        "PythonModuleAnalysis",
        "PythonWorkspaceAnalysis",
        "ResolvedImportRef",
        "directory_analysis",
        "file_analysis",
        "module_analysis",
        "workspace_analysis",
    }
    assert hasattr(integrations, "file_analysis")
    assert hasattr(integrations, "directory_analysis")
    assert hasattr(integrations, "module_analysis")
    assert hasattr(integrations, "workspace_analysis")
    assert hasattr(integrations, "PythonFileAnalysis")
    assert not hasattr(integrations, "source_text")
    assert not hasattr(integrations, "imports_for_file")
    assert not hasattr(integrations, "file_analysis_payload")
    assert not hasattr(integrations, "workspace_analysis_payload")


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
            ImportRef(module="os", kind="import", lineno=1),
            ImportRef(module="pkg.sub", kind="from", lineno=2),
        ),
        definitions=(
            DefinitionRef(name="alpha", kind="function", lineno=3),
            DefinitionRef(name="beta", kind="function", lineno=5),
            DefinitionRef(name="Gamma", kind="class", lineno=7),
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
    assert analysis.diagnostics[0].lineno == 1
    assert analysis.diagnostics[0].col_offset is not None


def test_comment_only_edit_backdates_source_and_reuses_downstream(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")

    first = file_analysis(db, path)
    assert first.imports == (ImportRef(module="os", kind="import", lineno=1),)

    path.write_text("import os\n# trailing comment\n", encoding="utf-8")
    second = file_analysis(db, path)

    assert second.imports == (ImportRef(module="os", kind="import", lineno=1),)
    assert db.inspect(source_text, str(path)).last_recompute == "backdated"
    assert db.inspect(imports_for_file, str(path)).last_decision == "reused"
    assert db.inspect(file_analysis_payload, str(path)).last_decision == "reused"


def test_semantic_edit_invalidates_downstream_analysis(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")

    assert file_analysis(db, path).imports == (ImportRef(module="os", kind="import", lineno=1),)

    path.write_text("import sys\n", encoding="utf-8")
    updated = file_analysis(db, path)

    assert updated.imports == (ImportRef(module="sys", kind="import", lineno=1),)
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
    assert analyses[0].imports == (ImportRef(module="os", kind="import", lineno=1),)
    assert analyses[1].imports == (ImportRef(module="sys", kind="import", lineno=1),)


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
def test_directory_analysis_matches_fresh_recomputation_over_changes(mode: str, tmp_path: Path) -> None:
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


def test_workspace_analysis_discovers_recursive_modules_and_derives_names(tmp_path: Path) -> None:
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
    assert tuple(item.module for item in analysis.modules) == ("main", "pkg", "pkg.nested.util")


def test_workspace_analysis_ignores_symlink_cycles_and_outside_workspace(tmp_path: Path) -> None:
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

    assert tuple(item.module for item in analysis.modules) == ("main", "pkg", "pkg.helper")
    assert all("external_link" not in item.path for item in analysis.modules)
    assert all(".loop." not in item.module for item in analysis.modules)


def test_workspace_analysis_reuses_when_only_outside_symlink_target_changes(tmp_path: Path) -> None:
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
        "import provider\n"
        "import os\n"
        "from provider import exported\n",
        encoding="utf-8",
    )

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert tuple(item.module for item in analysis.imports) == ("provider", "os", "provider")
    assert tuple(item.resolution for item in analysis.resolved_imports) == (
        "workspace",
        "external",
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
        "from . import helper\n"
        "from .helper import util\n",
        encoding="utf-8",
    )

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert tuple((item.module, item.imported_name, item.resolved_module) for item in analysis.resolved_imports) == (
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


def test_module_analysis_dependency_surface_tracks_reexport_aliases_and_assignments(tmp_path: Path) -> None:
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
        "from impl import exported as alias\n"
        "import pkg.sub as submod\n"
        "value = 1\n",
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
        "shown = 1\n"
        "_hidden = 2\n"
        "__all__ = ['shown']\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import *\n", encoding="utf-8")

    analysis = module_analysis(Database(mode="strict"), root, consumer)

    assert analysis.resolved_imports == (
        integrations.ResolvedImportRef(
            module="provider",
            kind="from",
            lineno=1,
            imported_name="*",
            resolved_module="provider",
            resolved_path=str(provider),
            resolution="workspace",
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
        "shown = 1\n"
        "_hidden = 2\n",
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


def test_module_analysis_prefers_workspace_submodule_for_package_imports(tmp_path: Path) -> None:
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
            lineno=1,
            imported_name="helper",
            resolved_module="pkg.helper",
            resolved_path=str(helper),
            resolution="workspace",
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


def test_module_analysis_marks_file_package_conflicts_as_ambiguous(tmp_path: Path) -> None:
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

    assert tuple(item.resolution for item in analysis.resolved_imports) == ("ambiguous", "ambiguous")
    assert tuple(item.resolved_module for item in analysis.resolved_imports) == (None, None)
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


def test_workspace_analysis_reuses_wildcard_consumer_when_exports_do_not_change(tmp_path: Path) -> None:
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
    assert db.inspect(module_wildcard_export_surface, str(root), str(provider)).last_decision == "reused"
    assert db.inspect(module_analysis_payload, str(root), str(consumer)).last_decision == "reused"
    assert db.inspect(workspace_analysis_payload, str(root)).last_decision == "reused"


def test_workspace_analysis_executes_dependents_when_provider_exports_change(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text("def exported() -> int:\n    return 1\n", encoding="utf-8")
    consumer.write_text("from provider import exported\n", encoding="utf-8")
    db = Database(mode="strict")

    first = workspace_analysis(db, root)

    provider.write_text(
        "def exported() -> int:\n    return 1\n\n"
        "def exported_two() -> int:\n    return 2\n",
        encoding="utf-8",
    )
    second = workspace_analysis(db, root)

    assert second != first
    assert db.inspect(module_analysis_payload, str(root), str(provider)).last_recompute == "executed"
    assert db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


def test_workspace_analysis_executes_wildcard_consumer_when_wildcard_exports_change(tmp_path: Path) -> None:
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
    assert db.inspect(module_wildcard_export_surface, str(root), str(provider)).last_recompute == "executed"
    assert db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


def test_dynamic_all_marks_wildcard_consumers_untracked(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "shown = 1\n"
        "extra = 2\n"
        "__all__ = ['shown']\n"
        "__all__ += ['extra']\n",
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


def test_provider_wildcard_reexport_marks_wildcard_consumers_untracked(tmp_path: Path) -> None:
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


def test_workspace_analysis_reexecutes_consumer_when_missing_module_is_added(tmp_path: Path) -> None:
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
    assert db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


def test_workspace_analysis_reexecutes_consumer_when_dependency_module_is_deleted(tmp_path: Path) -> None:
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
    assert db.inspect(module_analysis_payload, str(root), str(consumer)).last_recompute == "executed"
    assert db.inspect(workspace_analysis_payload, str(root)).last_recompute == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_workspace_analysis_matches_fresh_recomputation_over_changes(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    steps: tuple[Operation, ...] = (
        ("write", "provider.py", "def exported() -> int:\n    return 1\n"),
        ("write", "consumer.py", "from provider import exported\n"),
        ("write", "provider.py", "def exported() -> int:\n    return 2\n"),
        ("write", "provider.py", "def exported() -> int:\n    return 2\n\ndef extra() -> int:\n    return 3\n"),
        ("write", "pkg/__init__.py", ""),
        ("write", "pkg/helper.py", "def helper() -> int:\n    return 1\n"),
        ("write", "consumer.py", "from provider import exported\nfrom pkg import helper\n"),
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
