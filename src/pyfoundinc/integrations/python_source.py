from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pyfoundinc.core import query
from pyfoundinc.resources import DirectoryResource, FileResource
from pyfoundinc.runtime import Database
from pyfoundinc.value import thaw

ImportKind: TypeAlias = Literal["import", "from"]
DefinitionKind: TypeAlias = Literal["function", "class"]

ImportPayload: TypeAlias = tuple[str, ImportKind, int]
DefinitionPayload: TypeAlias = tuple[str, DefinitionKind, int]
DiagnosticPayload: TypeAlias = tuple[str, str, int | None, int | None]
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


_FILES = FileResource()
_DIRECTORIES = DirectoryResource()


def _normalize_path(path: str | os.PathLike[str]) -> str:
    return os.fspath(path)


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


def _relative_module_name(node: ast.ImportFrom) -> str:
    prefix = "." * node.level
    return f"{prefix}{node.module or ''}"


@query(cutoff=_source_cutoff_token)
def source_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def imports_for_file(db: Database, path: str) -> tuple[ImportPayload, ...]:
    tree = _try_parse(source_text(db, path))
    if tree is None:
        return tuple()

    imports: list[ImportPayload] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend((alias.name, "import", node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((_relative_module_name(node), "from", node.lineno))
    return tuple(imports)


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
def file_analysis_payload(db: Database, path: str) -> FileAnalysisPayload:
    return (
        path,
        imports_for_file(db, path),
        definitions_for_file(db, path),
        syntax_diagnostics_for_file(db, path),
    )


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


def _decode_file_analysis(payload: FileAnalysisPayload) -> PythonFileAnalysis:
    path, imports, definitions, diagnostics = payload
    return PythonFileAnalysis(
        path=path,
        imports=tuple(_decode_import(item) for item in imports),
        definitions=tuple(_decode_definition(item) for item in definitions),
        diagnostics=tuple(_decode_diagnostic(item) for item in diagnostics),
    )


def file_analysis(db: Database, path: str | os.PathLike[str]) -> PythonFileAnalysis:
    normalized_path = _normalize_path(path)
    payload = cast(FileAnalysisPayload, thaw(db.get(file_analysis_payload, normalized_path)))
    return _decode_file_analysis(payload)


def directory_analysis(db: Database, root: str | os.PathLike[str]) -> tuple[PythonFileAnalysis, ...]:
    normalized_root = _normalize_path(root)
    payload = cast(DirectoryAnalysisPayload, thaw(db.get(directory_analysis_payload, normalized_root)))
    return tuple(_decode_file_analysis(item) for item in payload)


__all__ = [
    "DefinitionRef",
    "Diagnostic",
    "ImportRef",
    "PythonFileAnalysis",
    "definitions_for_file",
    "directory_analysis",
    "directory_analysis_payload",
    "file_analysis",
    "file_analysis_payload",
    "imports_for_file",
    "source_text",
    "syntax_diagnostics_for_file",
]
