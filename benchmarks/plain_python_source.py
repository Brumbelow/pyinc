from __future__ import annotations

import ast
import os
from pathlib import Path

from pyfoundinc.integrations.python_source import (
    DefinitionRef,
    Diagnostic,
    ImportRef,
    PythonFileAnalysis,
)


def _normalize_path(path: str | os.PathLike[str]) -> str:
    return os.fspath(path)


def _relative_module_name(node: ast.ImportFrom) -> str:
    prefix = "." * node.level
    return f"{prefix}{node.module or ''}"


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
            imports.append(ImportRef(module=_relative_module_name(node), kind="from", lineno=node.lineno))
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


__all__ = ["directory_analysis", "file_analysis"]
