from __future__ import annotations

import ast
import os
from pathlib import Path

from pyfoundinc.integrations.python_source import (
    DependencySurface,
    DefinitionRef,
    Diagnostic,
    ImportRef,
    PythonFileAnalysis,
    PythonModuleAnalysis,
    PythonWorkspaceAnalysis,
    ResolvedImportRef,
)


def _normalize_path(path: str | os.PathLike[str]) -> str:
    return os.fspath(path)


def _relative_import_module(module: str | None, level: int) -> str:
    prefix = "." * level
    return f"{prefix}{module or ''}"


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


def _resolve_relative_base(current_module: str, current_path: str, request_module: str) -> str | None:
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
    return ".".join(anchor + base_parts)


def _index_groups(index: tuple[tuple[str, str], ...]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for module, path in index:
        grouped.setdefault(module, []).append(path)
    return {module: tuple(sorted(paths)) for module, paths in grouped.items()}


def _resolve_workspace_module(
    module: str,
    index_groups: dict[str, tuple[str, ...]],
) -> tuple[str | None, str | None, str | None]:
    paths = index_groups.get(module, ())
    if len(paths) == 1:
        return module, paths[0], "workspace"
    if len(paths) > 1:
        return None, None, "ambiguous"
    return None, None, None


def _missing_resolution(
    requested_module: str,
    index_groups: dict[str, tuple[str, ...]],
    *,
    prefer_external: bool,
) -> str:
    top_level = _top_level_module_name(requested_module)
    if top_level is None:
        return "missing"
    if top_level in index_groups:
        return "missing"
    return "external" if prefer_external else "missing"


def _import_statements(path: str) -> tuple[tuple[str, str, int, tuple[str, ...]], ...]:
    source = Path(path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return tuple()

    statements: list[tuple[str, str, int, tuple[str, ...]]] = []
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


def _resolve_import_reference(
    *,
    current_module: str,
    current_path: str,
    request_module: str,
    kind: str,
    imported_name: str | None,
    index_groups: dict[str, tuple[str, ...]],
) -> tuple[str | None, str | None, str]:
    if kind == "import":
        resolved_module, resolved_path, resolution = _resolve_workspace_module(request_module, index_groups)
        if resolution is not None:
            return resolved_module, resolved_path, resolution
        return None, None, _missing_resolution(request_module, index_groups, prefer_external=True)

    absolute_base = _resolve_relative_base(current_module, current_path, request_module)
    if absolute_base is None:
        return None, None, "missing"

    candidates: list[str] = []
    if imported_name is not None and imported_name != "*":
        candidates.append(f"{absolute_base}.{imported_name}" if absolute_base else imported_name)
    if absolute_base:
        candidates.append(absolute_base)

    for candidate in candidates:
        resolved_module, resolved_path, resolution = _resolve_workspace_module(candidate, index_groups)
        if resolution is not None:
            return resolved_module, resolved_path, resolution

    requested_target = candidates[0] if candidates else absolute_base
    return None, None, _missing_resolution(
        requested_target,
        index_groups,
        prefer_external=not request_module.startswith("."),
    )


def _workspace_python_files(root: str) -> tuple[str, ...]:
    normalized_root = _normalize_path(root)
    base = Path(normalized_root)
    if not base.is_dir():
        return tuple()
    return tuple(str(path) for path in sorted(base.rglob("*.py")))


def _workspace_module_index(root: str) -> tuple[tuple[str, str], ...]:
    return tuple((_module_name_for_path(root, path), path) for path in _workspace_python_files(root))


def file_analysis(path: str | os.PathLike[str]) -> PythonFileAnalysis:
    normalized_path = _normalize_path(path)
    source = Path(normalized_path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return PythonFileAnalysis(
            path=normalized_path,
            imports=(),
            definitions=(),
            diagnostics=(
                Diagnostic(
                    code="syntax-error",
                    message=exc.msg,
                    lineno=exc.lineno,
                    col_offset=exc.offset,
                ),
            ),
        )

    imports: list[ImportRef] = []
    definitions: list[DefinitionRef] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(ImportRef(module=alias.name, kind="import", lineno=node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(ImportRef(module=_relative_import_module(node.module, node.level), kind="from", lineno=node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append(DefinitionRef(name=node.name, kind="function", lineno=node.lineno))
        elif isinstance(node, ast.ClassDef):
            definitions.append(DefinitionRef(name=node.name, kind="class", lineno=node.lineno))

    return PythonFileAnalysis(
        path=normalized_path,
        imports=tuple(imports),
        definitions=tuple(definitions),
        diagnostics=(),
    )


def directory_analysis(root: str | os.PathLike[str]) -> tuple[PythonFileAnalysis, ...]:
    normalized_root = _normalize_path(root)
    base = Path(normalized_root)
    if not base.exists():
        return tuple()
    python_files = tuple(str(base / name) for name in sorted(child.name for child in base.iterdir()) if name.endswith(".py"))
    return tuple(file_analysis(path) for path in python_files)


def module_analysis(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> PythonModuleAnalysis:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    workspace_files = _workspace_python_files(normalized_root)
    if normalized_path not in workspace_files:
        raise ValueError(f"{normalized_path!r} is not a Python source file under {normalized_root!r}.")

    file_view = file_analysis(normalized_path)
    module = _module_name_for_path(normalized_root, normalized_path)
    index_groups = _index_groups(_workspace_module_index(normalized_root))

    resolved_imports: list[ResolvedImportRef] = []
    dependencies: dict[tuple[str, str], DependencySurface] = {}
    for request_module, kind, lineno, imported_names in _import_statements(normalized_path):
        names = (None,) if kind == "import" else imported_names
        for imported_name in names:
            resolved_module, resolved_path, resolution = _resolve_import_reference(
                current_module=module,
                current_path=normalized_path,
                request_module=request_module,
                kind=kind,
                imported_name=imported_name,
                index_groups=index_groups,
            )
            resolved_imports.append(
                ResolvedImportRef(
                    module=request_module,
                    kind=kind,
                    lineno=lineno,
                    imported_name=imported_name,
                    resolved_module=resolved_module,
                    resolved_path=resolved_path,
                    resolution=resolution,
                )
            )
            if resolution != "workspace" or resolved_module is None or resolved_path is None:
                continue
            dependency_file = file_analysis(resolved_path)
            exports = tuple(sorted(definition.name for definition in dependency_file.definitions))
            dependencies[(resolved_module, resolved_path)] = DependencySurface(
                module=resolved_module,
                path=resolved_path,
                exports=exports,
            )

    return PythonModuleAnalysis(
        path=normalized_path,
        module=module,
        imports=file_view.imports,
        definitions=file_view.definitions,
        diagnostics=file_view.diagnostics,
        resolved_imports=tuple(resolved_imports),
        dependencies=tuple(sorted(dependencies.values(), key=lambda item: (item.module, item.path))),
    )


def workspace_analysis(root: str | os.PathLike[str]) -> PythonWorkspaceAnalysis:
    normalized_root = _normalize_path(root)
    modules = tuple(module_analysis(normalized_root, path) for path in _workspace_python_files(normalized_root))
    return PythonWorkspaceAnalysis(root=normalized_root, modules=modules)


__all__ = ["directory_analysis", "file_analysis", "module_analysis", "workspace_analysis"]
