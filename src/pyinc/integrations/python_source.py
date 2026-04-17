from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pyinc.core import query
from pyinc.integrations.deep_module_resolution import resolve_module_location
from pyinc.integrations.installed_packages import environment_index
from pyinc.resources import DirectoryResource, _file_read_snapshot
from pyinc.runtime import Database
from pyinc.value import thaw

ImportKind: TypeAlias = Literal["import", "from"]
DefinitionKind: TypeAlias = Literal["function", "class"]
ImportResolution: TypeAlias = Literal[
    "workspace", "external", "stdlib", "installed", "missing", "ambiguous",
]

ModuleBindingAnalysisPayload: TypeAlias = tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]
ImportPayload: TypeAlias = tuple[str, ImportKind, int]
DefinitionPayload: TypeAlias = tuple[str, DefinitionKind, int]
DiagnosticPayload: TypeAlias = tuple[str, str, int | None, int | None]
ImportStatementPayload: TypeAlias = tuple[str, ImportKind, int, tuple[str, ...]]
ResolvedImportPayload: TypeAlias = tuple[
    str,
    ImportKind,
    int,
    str | None,
    str | None,
    str | None,
    ImportResolution,
    str | None,
    str | None,
]
DependencySurfacePayload: TypeAlias = tuple[str, str, tuple[str, ...]]
ModuleIndexEntryPayload: TypeAlias = tuple[str, str]
ModuleAnalysisPayload: TypeAlias = tuple[
    str,
    str,
    tuple[ImportPayload, ...],
    tuple[DefinitionPayload, ...],
    tuple[DiagnosticPayload, ...],
    tuple[ResolvedImportPayload, ...],
    tuple[DependencySurfacePayload, ...],
]
WorkspaceAnalysisPayload: TypeAlias = tuple[str, tuple[ModuleAnalysisPayload, ...]]
FileAnalysisPayload: TypeAlias = tuple[
    str,
    tuple[ImportPayload, ...],
    tuple[DefinitionPayload, ...],
    tuple[DiagnosticPayload, ...],
]
DirectoryAnalysisPayload: TypeAlias = tuple[FileAnalysisPayload, ...]


@dataclass(frozen=True)
class ImportRef:
    module: str
    kind: ImportKind
    lineno: int


@dataclass(frozen=True)
class DefinitionRef:
    name: str
    kind: DefinitionKind
    lineno: int


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    lineno: int | None
    col_offset: int | None


@dataclass(frozen=True)
class PythonFileAnalysis:
    path: str
    imports: tuple[ImportRef, ...]
    definitions: tuple[DefinitionRef, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class ResolvedImportRef:
    module: str
    kind: ImportKind
    lineno: int
    imported_name: str | None
    resolved_module: str | None
    resolved_path: str | None
    resolution: ImportResolution
    distribution_name: str | None
    distribution_version: str | None


@dataclass(frozen=True)
class DependencySurface:
    module: str
    path: str
    exports: tuple[str, ...]


@dataclass(frozen=True)
class PythonModuleAnalysis:
    path: str
    module: str
    imports: tuple[ImportRef, ...]
    definitions: tuple[DefinitionRef, ...]
    diagnostics: tuple[Diagnostic, ...]
    resolved_imports: tuple[ResolvedImportRef, ...]
    dependencies: tuple[DependencySurface, ...]


@dataclass(frozen=True)
class PythonWorkspaceAnalysis:
    root: str
    modules: tuple[PythonModuleAnalysis, ...]


@dataclass(frozen=True)
class _SourceTextResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"sourcefile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        file_path = Path(path)
        if not file_path.exists():
            return ("missing",)
        return ("present", hashlib.sha256(file_path.read_bytes()).hexdigest())

    def load(self, db: Database, path: str) -> str:
        file_path = Path(path)
        if not file_path.exists():
            return ""
        with db._allow_raw_open():
            return file_path.read_text(encoding=self.encoding)

    def probe_and_load(
        self, db: Database, path: str
    ) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = _file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _SourceTextResource()
_DIRECTORIES = DirectoryResource()


def _normalize_path(path: str | os.PathLike[str]) -> str:
    return os.fspath(path)


def _canonical_path(path: str) -> str:
    return str(Path(path).resolve(strict=False))


def _is_within_root(path: str, root: str) -> bool:
    try:
        Path(path).relative_to(Path(root))
    except ValueError:
        return False
    return True


def _relative_import_module(module: str | None, level: int) -> str:
    prefix = "." * level
    return f"{prefix}{module or ''}"


def _source_cutoff_token(source: str) -> tuple[str, str]:
    try:
        return ("ast", ast.dump(ast.parse(source), include_attributes=True))
    except SyntaxError:
        return ("source", source)


def _try_parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _bound_name_for_import(alias: ast.alias) -> str:
    if alias.asname is not None:
        return alias.asname
    return alias.name.split(".", 1)[0]


def _bound_name_for_from_import(alias: ast.alias) -> str:
    return alias.asname or alias.name


def _target_bound_names(target: ast.expr) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Starred):
        return _target_bound_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        names: list[str] = []
        for item in target.elts:
            names.extend(_target_bound_names(item))
        return tuple(names)
    return tuple()


def _literal_string_collection(value: ast.expr | None) -> tuple[str, ...] | None:
    if value is None or not isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        return None

    items: list[str] = []
    for item in value.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        items.append(item.value)
    return tuple(sorted(set(items)))


def _contains_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(node))


def _module_binding_analysis(tree: ast.Module) -> ModuleBindingAnalysisPayload:
    bound_names: set[str] = set()
    static_all_names: tuple[str, ...] | None = None
    impurity_reasons: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
            bound_names.add(node.name)
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_names.add(_bound_name_for_import(alias))
            continue

        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                _append_unique(impurity_reasons, "top-level wildcard re-export")
                continue
            for alias in node.names:
                bound_names.add(_bound_name_for_from_import(alias))
            continue

        if isinstance(node, ast.Assign):
            target_names = {name for target in node.targets for name in _target_bound_names(target)}
            bound_names.update(target_names)
            if "__all__" in target_names:
                literal_names = _literal_string_collection(node.value)
                if literal_names is None:
                    static_all_names = None
                    _append_unique(impurity_reasons, "dynamic __all__")
                elif "dynamic __all__" not in impurity_reasons:
                    static_all_names = literal_names
            continue

        if isinstance(node, ast.AnnAssign):
            target_names = set(_target_bound_names(node.target))
            if node.value is not None:
                bound_names.update(target_names)
            if "__all__" in target_names:
                literal_names = _literal_string_collection(node.value)
                if literal_names is None:
                    static_all_names = None
                    _append_unique(impurity_reasons, "dynamic __all__")
                elif "dynamic __all__" not in impurity_reasons:
                    static_all_names = literal_names
            continue

        if isinstance(node, ast.AugAssign):
            if "__all__" in _target_bound_names(node.target) or _contains_name(node.value, "__all__"):
                static_all_names = None
                _append_unique(impurity_reasons, "dynamic __all__")
            continue

        if isinstance(node, ast.Delete):
            deleted_names = {name for target in node.targets for name in _target_bound_names(target)}
            bound_names.difference_update(deleted_names)
            if "__all__" in deleted_names:
                static_all_names = None
                _append_unique(impurity_reasons, "dynamic __all__")
            continue

        if isinstance(node, ast.Expr):
            if _contains_name(node.value, "__all__"):
                static_all_names = None
                _append_unique(impurity_reasons, "dynamic __all__")
            continue

        if isinstance(node, ast.Pass):
            continue

        if _contains_name(node, "__all__"):
            static_all_names = None
            _append_unique(impurity_reasons, "dynamic __all__")
        _append_unique(impurity_reasons, f"unsupported top-level {type(node).__name__}")

    explicit_exports = tuple(sorted(bound_names))
    if static_all_names is not None:
        wildcard_exports = static_all_names
    else:
        wildcard_exports = tuple(name for name in explicit_exports if not name.startswith("_"))
    return explicit_exports, wildcard_exports, tuple(impurity_reasons)


def _module_name_for_path(root: str, path: str) -> str:
    relative_path = Path(path).relative_to(Path(root))
    if relative_path.suffix != ".py":
        raise ValueError(f"{path!r} is not a Python source file under {root!r}.")
    if relative_path.name == "__init__.py":
        module_parts = relative_path.parts[:-1]
    else:
        module_parts = relative_path.parts[:-1] + (relative_path.stem,)
    return ".".join(module_parts)


def _is_package_path(path: str) -> bool:
    return Path(path).name == "__init__.py"


def _top_level_module_name(module: str) -> str | None:
    if not module:
        return None
    return module.split(".", 1)[0]


def _index_groups(
    index: tuple[ModuleIndexEntryPayload, ...],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for module, path in index:
        grouped.setdefault(module, []).append(path)
    return {
        module: tuple(sorted(paths))
        for module, paths in grouped.items()
    }


def _build_environment_lookup(
    env_data: tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]],
) -> tuple[frozenset[str], dict[str, tuple[str, str]]]:
    stdlib_modules, package_entries = env_data
    stdlib_set = frozenset(stdlib_modules)
    pkg_map = {name: (dist, ver) for name, dist, ver in package_entries}
    return stdlib_set, pkg_map


def _resolve_relative_base(
    current_module: str,
    current_path: str,
    request_module: str,
) -> str | None:
    level = 0
    for char in request_module:
        if char != ".":
            break
        level += 1
    base = request_module[level:]
    if level == 0:
        return request_module

    package_parts = [part for part in current_module.split(".") if part]
    if package_parts and not _is_package_path(current_path):
        package_parts = package_parts[:-1]
    if level - 1 > len(package_parts):
        return None
    anchor = package_parts[: len(package_parts) - (level - 1)]
    base_parts = [part for part in base.split(".") if part]
    combined = anchor + base_parts
    return ".".join(combined)


def _missing_resolution(
    requested_module: str,
    index_groups: dict[str, tuple[str, ...]],
    *,
    prefer_external: bool,
    stdlib_modules: frozenset[str],
    package_top_levels: dict[str, tuple[str, str]],
) -> tuple[ImportResolution, str | None, str | None]:
    top_level = _top_level_module_name(requested_module)
    if top_level is None:
        return "missing", None, None
    if top_level in index_groups:
        return "missing", None, None
    if prefer_external:
        if top_level in stdlib_modules:
            return "stdlib", None, None
        if top_level in package_top_levels:
            dist_name, dist_ver = package_top_levels[top_level]
            return "installed", dist_name, dist_ver
    return "missing", None, None


def _resolve_workspace_module(
    module: str,
    index_groups: dict[str, tuple[str, ...]],
) -> tuple[str | None, str | None, ImportResolution | None]:
    ancestor_blocked = False
    parts = module.split(".")
    for index in range(1, len(parts)):
        prefix = ".".join(parts[:index])
        prefix_paths = index_groups.get(prefix, ())
        if len(prefix_paths) > 1:
            return None, None, "ambiguous"
        if len(prefix_paths) == 1 and not _is_package_path(prefix_paths[0]):
            ancestor_blocked = True

    paths = index_groups.get(module, ())
    if len(paths) == 1:
        if ancestor_blocked:
            return None, None, "ambiguous"
        return module, paths[0], "workspace"
    if len(paths) > 1:
        return None, None, "ambiguous"
    return None, None, None


def _installed_module_candidates(
    *,
    request_module: str,
    kind: ImportKind,
    imported_name: str | None,
    current_module: str,
    current_path: str,
) -> tuple[str, ...]:
    if kind == "import":
        return (request_module,) if request_module else ()
    absolute_base = _resolve_relative_base(current_module, current_path, request_module)
    if absolute_base is None:
        return ()
    candidates: list[str] = []
    if imported_name is not None and imported_name != "*":
        candidate = f"{absolute_base}.{imported_name}" if absolute_base else imported_name
        candidates.append(candidate)
    if absolute_base:
        candidates.append(absolute_base)
    return tuple(candidates)


def _enrich_installed_path(
    db: Database,
    *,
    request_module: str,
    kind: ImportKind,
    imported_name: str | None,
    current_module: str,
    current_path: str,
) -> tuple[str | None, str | None]:
    for candidate in _installed_module_candidates(
        request_module=request_module,
        kind=kind,
        imported_name=imported_name,
        current_module=current_module,
        current_path=current_path,
    ):
        payload = resolve_module_location(db, candidate)
        _, loc_kind, file_path, _, _, _, _ = payload
        if loc_kind == "regular-module" or loc_kind == "regular-package":
            return candidate, file_path
        if loc_kind == "namespace-package":
            return candidate, None
    return None, None


def _resolve_import_reference(
    *,
    current_module: str,
    current_path: str,
    request_module: str,
    kind: ImportKind,
    imported_name: str | None,
    index_groups: dict[str, tuple[str, ...]],
    stdlib_modules: frozenset[str],
    package_top_levels: dict[str, tuple[str, str]],
) -> tuple[str | None, str | None, ImportResolution, str | None, str | None]:
    if kind == "import":
        resolved_module, resolved_path, resolution = _resolve_workspace_module(request_module, index_groups)
        if resolution is not None:
            return resolved_module, resolved_path, resolution, None, None
        return None, None, *_missing_resolution(
            request_module, index_groups, prefer_external=True,
            stdlib_modules=stdlib_modules, package_top_levels=package_top_levels,
        )

    absolute_base = _resolve_relative_base(current_module, current_path, request_module)
    if absolute_base is None:
        return None, None, "missing", None, None

    candidates: list[str] = []
    if imported_name is not None and imported_name != "*":
        candidate = f"{absolute_base}.{imported_name}" if absolute_base else imported_name
        candidates.append(candidate)
    if absolute_base:
        candidates.append(absolute_base)

    for candidate in candidates:
        resolved_module, resolved_path, resolution = _resolve_workspace_module(candidate, index_groups)
        if resolution is not None:
            return resolved_module, resolved_path, resolution, None, None

    requested_target = candidates[0] if candidates else absolute_base
    return None, None, *_missing_resolution(
        requested_target,
        index_groups,
        prefer_external=not request_module.startswith("."),
        stdlib_modules=stdlib_modules,
        package_top_levels=package_top_levels,
    )


def _collect_python_files(
    db: Database,
    directory: str,
    entries: tuple[str, ...],
    *,
    canonical_root: str,
    visited_directories: set[str],
) -> tuple[str, ...]:
    python_files: list[str] = []
    base = Path(directory)
    for name in entries:
        child = str(base / name)
        canonical_child = _canonical_path(child)
        if not _is_within_root(canonical_child, canonical_root):
            continue
        try:
            child_entries = _DIRECTORIES.read(db, child)
        except NotADirectoryError:
            if name.endswith(".py"):
                python_files.append(child)
            continue
        if canonical_child in visited_directories:
            continue
        visited_directories.add(canonical_child)
        python_files.extend(
            _collect_python_files(
                db,
                child,
                child_entries,
                canonical_root=canonical_root,
                visited_directories=visited_directories,
            )
        )
    return tuple(python_files)


@query(cutoff=_source_cutoff_token)
def source_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def import_statements_for_file(db: Database, path: str) -> tuple[ImportStatementPayload, ...]:
    tree = _try_parse(source_text(db, path))
    if tree is None:
        return tuple()

    statements: list[ImportStatementPayload] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            statements.extend((alias.name, "import", node.lineno, tuple()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            statements.append(
                (
                    _relative_import_module(node.module, node.level),
                    "from",
                    node.lineno,
                    tuple(alias.name for alias in node.names),
                )
            )
    return tuple(statements)


@query
def imports_for_file(db: Database, path: str) -> tuple[ImportPayload, ...]:
    statements = import_statements_for_file(db, path)
    return tuple((module, kind, lineno) for module, kind, lineno, _ in statements)


@query
def definitions_for_file(db: Database, path: str) -> tuple[DefinitionPayload, ...]:
    tree = _try_parse(source_text(db, path))
    if tree is None:
        return tuple()

    definitions: list[DefinitionPayload] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append((node.name, "function", node.lineno))
        elif isinstance(node, ast.ClassDef):
            definitions.append((node.name, "class", node.lineno))
    return tuple(definitions)


@query
def syntax_diagnostics_for_file(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    source = source_text(db, path)
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return (
            (
                "syntax-error",
                exc.msg,
                exc.lineno,
                exc.offset,
            ),
        )
    return tuple()


@query
def module_binding_analysis_payload(db: Database, path: str) -> ModuleBindingAnalysisPayload:
    tree = _try_parse(source_text(db, path))
    if tree is None:
        return (tuple(), tuple(), tuple())
    return _module_binding_analysis(tree)


@query
def file_analysis_payload(db: Database, path: str) -> FileAnalysisPayload:
    return (
        path,
        imports_for_file(db, path),
        definitions_for_file(db, path),
        syntax_diagnostics_for_file(db, path),
    )


@query
def workspace_python_files(db: Database, root: str) -> tuple[str, ...]:
    try:
        entries = _DIRECTORIES.read(db, root)
    except NotADirectoryError:
        return tuple()
    canonical_root = _canonical_path(root)
    files = _collect_python_files(
        db,
        root,
        entries,
        canonical_root=canonical_root,
        visited_directories={canonical_root},
    )
    return tuple(sorted(files))


@query
def workspace_module_index(db: Database, root: str) -> tuple[ModuleIndexEntryPayload, ...]:
    return tuple(
        (_module_name_for_path(root, path), path)
        for path in workspace_python_files(db, root)
    )


@query
def resolved_imports_for_file(db: Database, root: str, path: str) -> tuple[ResolvedImportPayload, ...]:
    current_module = _module_name_for_path(root, path)
    index_groups = _index_groups(workspace_module_index(db, root))
    statements = import_statements_for_file(db, path)
    env_data = environment_index(db)
    stdlib_modules, pkg_map = _build_environment_lookup(env_data)

    resolved: list[ResolvedImportPayload] = []
    for request_module, kind, lineno, imported_names in statements:
        if kind == "import":
            resolved_module, resolved_path, resolution, dist_name, dist_ver = _resolve_import_reference(
                current_module=current_module,
                current_path=path,
                request_module=request_module,
                kind=kind,
                imported_name=None,
                index_groups=index_groups,
                stdlib_modules=stdlib_modules,
                package_top_levels=pkg_map,
            )
            if resolution == "installed" and resolved_path is None:
                enriched_module, enriched_path = _enrich_installed_path(
                    db,
                    request_module=request_module,
                    kind=kind,
                    imported_name=None,
                    current_module=current_module,
                    current_path=path,
                )
                if enriched_path is not None:
                    resolved_module = enriched_module
                    resolved_path = enriched_path
                elif enriched_module is not None:
                    resolved_module = enriched_module
            resolved.append(
                (request_module, kind, lineno, None, resolved_module, resolved_path, resolution, dist_name, dist_ver)
            )
            continue

        for imported_name in imported_names:
            resolved_module, resolved_path, resolution, dist_name, dist_ver = _resolve_import_reference(
                current_module=current_module,
                current_path=path,
                request_module=request_module,
                kind=kind,
                imported_name=imported_name,
                index_groups=index_groups,
                stdlib_modules=stdlib_modules,
                package_top_levels=pkg_map,
            )
            if resolution == "installed" and resolved_path is None:
                enriched_module, enriched_path = _enrich_installed_path(
                    db,
                    request_module=request_module,
                    kind=kind,
                    imported_name=imported_name,
                    current_module=current_module,
                    current_path=path,
                )
                if enriched_path is not None:
                    resolved_module = enriched_module
                    resolved_path = enriched_path
                elif enriched_module is not None:
                    resolved_module = enriched_module
            resolved.append(
                (
                    request_module,
                    kind,
                    lineno,
                    imported_name,
                    resolved_module,
                    resolved_path,
                    resolution,
                    dist_name,
                    dist_ver,
                )
            )
    return tuple(resolved)


@query
def module_export_surface(db: Database, root: str, path: str) -> DependencySurfacePayload:
    module = _module_name_for_path(root, path)
    exports, _, impurity_reasons = module_binding_analysis_payload(db, path)
    for reason in impurity_reasons:
        db.report_untracked_read(f"{module} export surface: {reason}")
    return (module, path, exports)


@query
def module_wildcard_export_surface(db: Database, root: str, path: str) -> DependencySurfacePayload:
    module = _module_name_for_path(root, path)
    _, exports, impurity_reasons = module_binding_analysis_payload(db, path)
    for reason in impurity_reasons:
        db.report_untracked_read(f"{module} wildcard export surface: {reason}")
    return (module, path, exports)


@query
def module_analysis_payload(db: Database, root: str, path: str) -> ModuleAnalysisPayload:
    workspace_files = workspace_python_files(db, root)
    if path not in workspace_files:
        return _empty_module_analysis_payload(root, path)

    resolved_imports = resolved_imports_for_file(db, root, path)
    dependencies: dict[tuple[str, str], tuple[DependencySurfacePayload, bool]] = {}
    for _, kind, _, imported_name, resolved_module, resolved_path, resolution, _, _ in resolved_imports:
        if resolution != "workspace" or resolved_module is None or resolved_path is None:
            continue
        _, _, impurity_reasons = module_binding_analysis_payload(db, resolved_path)
        for reason in impurity_reasons:
            db.report_untracked_read(f"{resolved_module} dependency surface: {reason}")
        use_wildcard_surface = kind == "from" and imported_name == "*"
        if use_wildcard_surface:
            surface = module_wildcard_export_surface(db, root, resolved_path)
        else:
            surface = module_export_surface(db, root, resolved_path)
        key = (surface[0], surface[1])
        existing = dependencies.get(key)
        if existing is None or use_wildcard_surface:
            dependencies[key] = (surface, use_wildcard_surface)

    return (
        path,
        _module_name_for_path(root, path),
        imports_for_file(db, path),
        definitions_for_file(db, path),
        syntax_diagnostics_for_file(db, path),
        resolved_imports,
        tuple(sorted((item[0] for item in dependencies.values()), key=lambda item: (item[0], item[1]))),
    )


@query
def workspace_analysis_payload(db: Database, root: str) -> WorkspaceAnalysisPayload:
    files = workspace_python_files(db, root)
    return (root, tuple(module_analysis_payload(db, root, path) for path in files))


@query
def directory_analysis_payload(db: Database, root: str) -> DirectoryAnalysisPayload:
    entries = _DIRECTORIES.read(db, root)
    base = Path(root)
    python_files = tuple(str(base / name) for name in entries if name.endswith(".py"))
    return tuple(file_analysis_payload(db, path) for path in python_files)


def _decode_import(payload: ImportPayload) -> ImportRef:
    module, kind, lineno = payload
    return ImportRef(module=module, kind=kind, lineno=lineno)


def _decode_definition(payload: DefinitionPayload) -> DefinitionRef:
    name, kind, lineno = payload
    return DefinitionRef(name=name, kind=kind, lineno=lineno)


def _decode_diagnostic(payload: DiagnosticPayload) -> Diagnostic:
    code, message, lineno, col_offset = payload
    return Diagnostic(code=code, message=message, lineno=lineno, col_offset=col_offset)


def _decode_resolved_import(payload: ResolvedImportPayload) -> ResolvedImportRef:
    module, kind, lineno, imported_name, resolved_module, resolved_path, resolution, dist_name, dist_ver = payload
    return ResolvedImportRef(
        module=module,
        kind=kind,
        lineno=lineno,
        imported_name=imported_name,
        resolved_module=resolved_module,
        resolved_path=resolved_path,
        resolution=resolution,
        distribution_name=dist_name,
        distribution_version=dist_ver,
    )


def _decode_dependency_surface(payload: DependencySurfacePayload) -> DependencySurface:
    module, path, exports = payload
    return DependencySurface(module=module, path=path, exports=exports)


def _empty_module_analysis_payload(root: str, path: str) -> ModuleAnalysisPayload:
    return (
        path,
        _module_name_for_path(root, path),
        tuple(),
        tuple(),
        tuple(),
        tuple(),
        tuple(),
    )


def _decode_file_analysis(payload: FileAnalysisPayload) -> PythonFileAnalysis:
    path, imports, definitions, diagnostics = payload
    return PythonFileAnalysis(
        path=path,
        imports=tuple(_decode_import(item) for item in imports),
        definitions=tuple(_decode_definition(item) for item in definitions),
        diagnostics=tuple(_decode_diagnostic(item) for item in diagnostics),
    )


def _decode_module_analysis(payload: ModuleAnalysisPayload) -> PythonModuleAnalysis:
    path, module, imports, definitions, diagnostics, resolved_imports, dependencies = payload
    return PythonModuleAnalysis(
        path=path,
        module=module,
        imports=tuple(_decode_import(item) for item in imports),
        definitions=tuple(_decode_definition(item) for item in definitions),
        diagnostics=tuple(_decode_diagnostic(item) for item in diagnostics),
        resolved_imports=tuple(_decode_resolved_import(item) for item in resolved_imports),
        dependencies=tuple(_decode_dependency_surface(item) for item in dependencies),
    )


def file_analysis(db: Database, path: str | os.PathLike[str]) -> PythonFileAnalysis:
    normalized_path = _normalize_path(path)
    payload = cast(FileAnalysisPayload, thaw(db.get(file_analysis_payload, normalized_path)))
    return _decode_file_analysis(payload)


def directory_analysis(db: Database, root: str | os.PathLike[str]) -> tuple[PythonFileAnalysis, ...]:
    normalized_root = _normalize_path(root)
    payload = cast(DirectoryAnalysisPayload, thaw(db.get(directory_analysis_payload, normalized_root)))
    return tuple(_decode_file_analysis(item) for item in payload)


def module_analysis(db: Database, root: str | os.PathLike[str], path: str | os.PathLike[str]) -> PythonModuleAnalysis:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    workspace_files = cast(tuple[str, ...], thaw(db.get(workspace_python_files, normalized_root)))
    if normalized_path not in workspace_files:
        raise ValueError(f"{normalized_path!r} is not a Python source file under {normalized_root!r}.")
    payload = cast(
        ModuleAnalysisPayload,
        thaw(db.get(module_analysis_payload, normalized_root, normalized_path)),
    )
    return _decode_module_analysis(payload)


def workspace_analysis(db: Database, root: str | os.PathLike[str]) -> PythonWorkspaceAnalysis:
    normalized_root = _normalize_path(root)
    payload = cast(
        WorkspaceAnalysisPayload,
        thaw(db.get(workspace_analysis_payload, normalized_root)),
    )
    workspace_root, modules = payload
    return PythonWorkspaceAnalysis(
        root=workspace_root,
        modules=tuple(_decode_module_analysis(item) for item in modules),
    )


__all__ = [
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
]
