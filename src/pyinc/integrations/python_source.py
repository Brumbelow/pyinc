from __future__ import annotations

import ast
import hashlib
import io
import os
import tokenize
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pyinc._python_lexing import identifier_tokens
from pyinc.core import query
from pyinc.integrations.deep_module_resolution import resolve_module_location
from pyinc.integrations.installed_packages import environment_index
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database

from ._decoding import decoded, once_per_request
from ._resources import file_bytes, file_probe
from .source_geometry import (
    DocumentMap,
    SourcePosition,
    SourceRange,
    identifier_range_in_tokens,
)

ImportKind: TypeAlias = Literal["import", "from"]
DefinitionKind: TypeAlias = Literal["function", "class"]
ImportResolution: TypeAlias = Literal[
    "workspace",
    "external",
    "stdlib",
    "installed",
    "missing",
    "ambiguous",
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
SourceTextPayload: TypeAlias = tuple[str, str | None]
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
RangePayload: TypeAlias = tuple[int, int, int, int]
FileSourceRangesPayload: TypeAlias = tuple[
    tuple[tuple[int, RangePayload], ...],
    tuple[tuple[int, str, RangePayload], ...],
    RangePayload | None,
]


@dataclass(frozen=True)
class ImportRef:
    module: str
    kind: ImportKind
    range: SourceRange


@dataclass(frozen=True)
class DefinitionRef:
    name: str
    kind: DefinitionKind
    range: SourceRange


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    range: SourceRange | None


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
    range: SourceRange
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
    def read(self, db: Database, path: str | os.PathLike[str]) -> SourceTextPayload:
        return cast(SourceTextPayload, db.read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"sourcefile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        return file_probe(path)

    def load(self, db: Database, path: str) -> SourceTextPayload:
        data = file_bytes(path)
        if data is None:
            return "", None
        return _decode_python_source(data, path)

    def probe_and_load(
        self, db: Database, path: str
    ) -> tuple[tuple[str, str] | tuple[str], SourceTextPayload]:
        data = file_bytes(path)
        if data is None:
            return ("missing",), ("", None)
        probe = ("present", hashlib.sha256(data).hexdigest())
        return probe, _decode_python_source(data, path)

    def identity(self) -> str:
        return "python-source-pep263-v1"


def _decode_python_source(data: bytes, path: str) -> SourceTextPayload:
    """Decode source according to the BOM and PEP 263 encoding cookie."""

    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError as exc:
        return "", f"{path}: {exc}"
    try:
        return data.decode(encoding), None
    except (LookupError, UnicodeError) as exc:
        return "", f"{path}: {exc}"


_FILES = _SourceTextResource()
_DIRECTORIES = DirectoryResource()
_AST_TYPE_ALIAS = getattr(ast, "TypeAlias", None)


def _is_type_alias(node: ast.AST) -> bool:
    return isinstance(_AST_TYPE_ALIAS, type) and isinstance(node, _AST_TYPE_ALIAS)


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

        if _is_type_alias(node):
            name = getattr(node, "name", None)
            if isinstance(name, ast.Name):
                bound_names.add(name.id)
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
            if "__all__" in _target_bound_names(node.target) or _contains_name(
                node.value, "__all__"
            ):
                static_all_names = None
                _append_unique(impurity_reasons, "dynamic __all__")
            continue

        if isinstance(node, ast.Delete):
            deleted_names = {
                name for target in node.targets for name in _target_bound_names(target)
            }
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

        if isinstance(node, ast.Try) and _has_import_error_handler(node.handlers):
            for stmt in node.body:
                if isinstance(stmt, ast.Import):
                    for alias in stmt.names:
                        bound_names.add(_bound_name_for_import(alias))
                elif isinstance(stmt, ast.ImportFrom) and not any(
                    alias.name == "*" for alias in stmt.names
                ):
                    for alias in stmt.names:
                        bound_names.add(_bound_name_for_from_import(alias))
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
    return {module: tuple(sorted(paths)) for module, paths in grouped.items()}


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
        resolved_module, resolved_path, resolution = _resolve_workspace_module(
            request_module, index_groups
        )
        if resolution is not None:
            return resolved_module, resolved_path, resolution, None, None
        resolution, dist_name, dist_ver = _missing_resolution(
            request_module,
            index_groups,
            prefer_external=True,
            stdlib_modules=stdlib_modules,
            package_top_levels=package_top_levels,
        )
        return None, None, resolution, dist_name, dist_ver

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
        resolved_module, resolved_path, resolution = _resolve_workspace_module(
            candidate, index_groups
        )
        if resolution is not None:
            return resolved_module, resolved_path, resolution, None, None

    requested_target = candidates[0] if candidates else absolute_base
    resolution, dist_name, dist_ver = _missing_resolution(
        requested_target,
        index_groups,
        prefer_external=not request_module.startswith("."),
        stdlib_modules=stdlib_modules,
        package_top_levels=package_top_levels,
    )
    return None, None, resolution, dist_name, dist_ver


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
    # Deliberately not memoized per request: query bodies read the source
    # through here, and answering one from an earlier call would rob the second
    # query of the resource dependency the kernel needs to invalidate it. The
    # kernel enforces this -- `once_per_request` closes over a ContextVar, which
    # it refuses to source-pin, so any query that reached it would fail loudly.
    return _FILES.read(db, path)[0]


def _encode_range(value: SourceRange) -> RangePayload:
    return (value.start.line, value.start.character, value.end.line, value.end.character)


def _decode_range(payload: RangePayload) -> SourceRange:
    return SourceRange(
        SourcePosition(payload[0], payload[1]), SourcePosition(payload[2], payload[3])
    )


@query
def source_ranges_for_file(db: Database, path: str) -> FileSourceRangesPayload:
    source = source_text(db, path)
    document = DocumentMap(source)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        if exc.lineno is None:
            return ((), (), None)
        start = SourcePosition(max(exc.lineno - 1, 0), max((exc.offset or 1) - 1, 0))
        end = SourcePosition(
            max((exc.end_lineno or exc.lineno) - 1, 0),
            max((exc.end_offset or exc.offset or 1) - 1, 0),
        )
        if end <= start:
            end = SourcePosition(start.line, start.character + 1)
        return ((), (), _encode_range(SourceRange(start, end)))

    normalized_source = "\n".join(document.lines)
    tokens = identifier_tokens(normalized_source)
    import_ranges: dict[int, RangePayload] = {}
    definition_ranges: dict[tuple[int, str], RangePayload] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_ranges.setdefault(node.lineno, _encode_range(document.ast_range(node)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definition_ranges[(node.lineno, node.name)] = _encode_range(
                identifier_range_in_tokens(document, tokens, node, node.name)
            )
    return (
        tuple(sorted(import_ranges.items())),
        tuple(
            (line, name, payload)
            for (line, name), payload in sorted(definition_ranges.items())
        ),
        None,
    )


def _is_type_checking_test(test: ast.expr) -> bool:
    """Return True if *test* is ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def _has_import_error_handler(handlers: list[ast.ExceptHandler]) -> bool:
    """Return True if any handler catches ImportError or ModuleNotFoundError."""
    for handler in handlers:
        exc_type = handler.type
        if exc_type is None:
            continue
        if isinstance(exc_type, ast.Name) and exc_type.id in (
            "ImportError",
            "ModuleNotFoundError",
        ):
            return True
        if isinstance(exc_type, ast.Tuple) and any(
            isinstance(elt, ast.Name) and elt.id in ("ImportError", "ModuleNotFoundError")
            for elt in exc_type.elts
        ):
            return True
    return False


def _collect_import_statements(
    body: list[ast.stmt], statements: list[ImportStatementPayload]
) -> None:
    for node in body:
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
        elif (isinstance(node, ast.If) and _is_type_checking_test(node.test)) or (
            isinstance(node, ast.Try) and _has_import_error_handler(node.handlers)
        ):
            _collect_import_statements(node.body, statements)
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
    source, decode_error = _FILES.read(db, path)
    if decode_error is not None:
        return (("source-decode-error", decode_error, None, None),)
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
        (_module_name_for_path(root, path), path) for path in workspace_python_files(db, root)
    )


@query
def resolved_imports_for_file(
    db: Database, root: str, path: str
) -> tuple[ResolvedImportPayload, ...]:
    current_module = _module_name_for_path(root, path)
    index_groups = _index_groups(workspace_module_index(db, root))
    statements = import_statements_for_file(db, path)
    env_data = environment_index(db)
    stdlib_modules, pkg_map = _build_environment_lookup(env_data)

    resolved: list[ResolvedImportPayload] = []
    for request_module, kind, lineno, imported_names in statements:
        if kind == "import":
            resolved_module, resolved_path, resolution, dist_name, dist_ver = (
                _resolve_import_reference(
                    current_module=current_module,
                    current_path=path,
                    request_module=request_module,
                    kind=kind,
                    imported_name=None,
                    index_groups=index_groups,
                    stdlib_modules=stdlib_modules,
                    package_top_levels=pkg_map,
                )
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
                (
                    request_module,
                    kind,
                    lineno,
                    None,
                    resolved_module,
                    resolved_path,
                    resolution,
                    dist_name,
                    dist_ver,
                )
            )
            continue

        for imported_name in imported_names:
            resolved_module, resolved_path, resolution, dist_name, dist_ver = (
                _resolve_import_reference(
                    current_module=current_module,
                    current_path=path,
                    request_module=request_module,
                    kind=kind,
                    imported_name=imported_name,
                    index_groups=index_groups,
                    stdlib_modules=stdlib_modules,
                    package_top_levels=pkg_map,
                )
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
    for (
        _,
        kind,
        _,
        imported_name,
        resolved_module,
        resolved_path,
        resolution,
        _,
        _,
    ) in resolved_imports:
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
        tuple(
            sorted(
                (item[0] for item in dependencies.values()),
                key=lambda item: (item[0], item[1]),
            )
        ),
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


def _payload_range(lineno: int, width: int = 0) -> SourceRange:
    start = SourcePosition(max(lineno - 1, 0), 0)
    return SourceRange(start, SourcePosition(start.line, width))


def _decode_import(payload: ImportPayload) -> ImportRef:
    module, kind, lineno = payload
    return ImportRef(module=module, kind=kind, range=_payload_range(lineno))


def _decode_definition(payload: DefinitionPayload) -> DefinitionRef:
    name, kind, lineno = payload
    return DefinitionRef(name=name, kind=kind, range=_payload_range(lineno))


def _decode_diagnostic(payload: DiagnosticPayload) -> Diagnostic:
    code, message, _lineno, _col_offset = payload
    return Diagnostic(code=code, message=message, range=None)


def _decode_resolved_import(payload: ResolvedImportPayload) -> ResolvedImportRef:
    (
        module,
        kind,
        lineno,
        imported_name,
        resolved_module,
        resolved_path,
        resolution,
        dist_name,
        dist_ver,
    ) = payload
    return ResolvedImportRef(
        module=module,
        kind=kind,
        range=_payload_range(lineno),
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


def _apply_file_source_ranges(
    analysis: PythonFileAnalysis, ranges: FileSourceRangesPayload
) -> PythonFileAnalysis:
    import_payloads, definition_payloads, error_range = ranges
    if error_range is not None:
        diagnostics = analysis.diagnostics
        if diagnostics:
            diagnostics = (
                replace(diagnostics[0], range=_decode_range(error_range)),
                *diagnostics[1:],
            )
        return replace(analysis, diagnostics=diagnostics)
    import_ranges = {line: _decode_range(item) for line, item in import_payloads}
    definition_ranges = {
        (line, name): _decode_range(item) for line, name, item in definition_payloads
    }
    return replace(
        analysis,
        imports=tuple(
            replace(item, range=import_ranges.get(item.range.start.line + 1, item.range))
            for item in analysis.imports
        ),
        definitions=tuple(
            replace(
                item,
                range=definition_ranges.get(
                    (item.range.start.line + 1, item.name), item.range
                ),
            )
            for item in analysis.definitions
        ),
    )


def _apply_module_source_ranges(
    analysis: PythonModuleAnalysis, ranges: FileSourceRangesPayload
) -> PythonModuleAnalysis:
    file_part = _apply_file_source_ranges(
        PythonFileAnalysis(
            analysis.path,
            analysis.imports,
            analysis.definitions,
            analysis.diagnostics,
        ),
        ranges,
    )
    ranges_by_line = {item.range.start.line: item.range for item in file_part.imports}
    return replace(
        analysis,
        imports=file_part.imports,
        definitions=file_part.definitions,
        diagnostics=file_part.diagnostics,
        resolved_imports=tuple(
            replace(item, range=ranges_by_line.get(item.range.start.line, item.range))
            for item in analysis.resolved_imports
        ),
    )


def _decoded_file_analysis(
    db: Database, payload: FileAnalysisPayload, ranges: FileSourceRangesPayload
) -> PythonFileAnalysis:
    return decoded(
        db,
        "file_analysis",
        (payload, ranges),
        lambda: _apply_file_source_ranges(_decode_file_analysis(payload), ranges),
    )


def _decoded_module_analysis(
    db: Database, payload: ModuleAnalysisPayload, ranges: FileSourceRangesPayload
) -> PythonModuleAnalysis:
    return decoded(
        db,
        "module_analysis",
        (payload, ranges),
        lambda: _apply_module_source_ranges(_decode_module_analysis(payload), ranges),
    )


# Every payload below is declared as nested tuples of primitives, and `freeze`
# leaves such a value as plain tuples, so what `db.get` hands back in any mode is
# already the payload. Thawing it again only walks and copies the whole tree --
# on a workspace-sized request that copy dominated the cost of decoding.
def file_analysis(db: Database, path: str | os.PathLike[str]) -> PythonFileAnalysis:
    normalized_path = _normalize_path(path)
    payload = db.get(file_analysis_payload, normalized_path)
    return _decoded_file_analysis(db, payload, source_ranges_for_file(db, normalized_path))


def directory_analysis(
    db: Database, root: str | os.PathLike[str]
) -> tuple[PythonFileAnalysis, ...]:
    normalized_root = _normalize_path(root)
    payload = db.get(directory_analysis_payload, normalized_root)
    return tuple(
        _decoded_file_analysis(db, item, source_ranges_for_file(db, item[0]))
        for item in payload
    )


def module_analysis(
    db: Database, root: str | os.PathLike[str], path: str | os.PathLike[str]
) -> PythonModuleAnalysis:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    return once_per_request(
        db,
        "module_analysis",
        (normalized_root, normalized_path),
        lambda: _module_analysis(db, normalized_root, normalized_path),
    )


def _module_analysis(db: Database, normalized_root: str, normalized_path: str) -> PythonModuleAnalysis:
    workspace_files = db.get(workspace_python_files, normalized_root)
    if normalized_path not in workspace_files:
        raise ValueError(
            f"{normalized_path!r} is not a Python source file under {normalized_root!r}."
        )
    payload = db.get(module_analysis_payload, normalized_root, normalized_path)
    return _decoded_module_analysis(db, payload, source_ranges_for_file(db, normalized_path))


def workspace_analysis(db: Database, root: str | os.PathLike[str]) -> PythonWorkspaceAnalysis:
    normalized_root = _normalize_path(root)
    return once_per_request(
        db,
        "workspace_analysis",
        (normalized_root,),
        lambda: _workspace_analysis(db, normalized_root),
    )


def _workspace_analysis(db: Database, normalized_root: str) -> PythonWorkspaceAnalysis:
    payload = db.get(workspace_analysis_payload, normalized_root)
    workspace_root, modules = payload
    return PythonWorkspaceAnalysis(
        root=workspace_root,
        modules=tuple(
            _decoded_module_analysis(db, item, source_ranges_for_file(db, item[0]))
            for item in modules
        ),
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
    "module_binding_analysis_payload",
    "module_wildcard_export_surface",
    "resolved_imports_for_file",
    "source_text",
    "workspace_analysis",
    "workspace_python_files",
]
