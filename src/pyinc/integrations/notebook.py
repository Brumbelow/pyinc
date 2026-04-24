from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource, _file_read_snapshot
from pyinc.runtime import Database
from pyinc.value import thaw

CellType: TypeAlias = Literal["code", "markdown", "raw", "unknown"]
ImportKind: TypeAlias = Literal["import", "from"]
DefinitionKind: TypeAlias = Literal["function", "class"]

NotebookImportPayload: TypeAlias = tuple[str, ImportKind, int]
NotebookDefinitionPayload: TypeAlias = tuple[str, DefinitionKind, int]
NotebookCellPayload: TypeAlias = tuple[
    int,
    CellType,
    str,
    str | None,
    tuple[NotebookImportPayload, ...],
    tuple[NotebookDefinitionPayload, ...],
]
NotebookDiagnosticPayload: TypeAlias = tuple[str, str, int | None]
NotebookAnalysisPayload: TypeAlias = tuple[
    str,
    str | None,
    str | None,
    tuple[NotebookCellPayload, ...],
    tuple[NotebookDiagnosticPayload, ...],
]


@dataclass(frozen=True)
class NotebookImport:
    module: str
    kind: ImportKind
    lineno: int


@dataclass(frozen=True)
class NotebookDefinition:
    name: str
    kind: DefinitionKind
    lineno: int


@dataclass(frozen=True)
class NotebookCell:
    index: int
    cell_type: CellType
    source: str
    heading: str | None
    imports: tuple[NotebookImport, ...]
    definitions: tuple[NotebookDefinition, ...]


@dataclass(frozen=True)
class NotebookDiagnostic:
    code: str
    message: str
    cell_index: int | None


@dataclass(frozen=True)
class NotebookAnalysis:
    path: str
    kernel_name: str | None
    language: str | None
    cells: tuple[NotebookCell, ...]
    diagnostics: tuple[NotebookDiagnostic, ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NotebookFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"notebookfile[{path}]"

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


_FILES = _NotebookFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_source(raw: Any) -> str:
    if isinstance(raw, list):
        return "".join(item for item in raw if isinstance(item, str))
    if isinstance(raw, str):
        return raw
    return ""


def _classify_cell_type(value: Any) -> CellType:
    if value == "code":
        return "code"
    if value == "markdown":
        return "markdown"
    if value == "raw":
        return "raw"
    return "unknown"


def _first_markdown_heading(source: str) -> str | None:
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
    return None


def _extract_code_imports_and_defs(
    source: str,
) -> tuple[
    tuple[NotebookImportPayload, ...],
    tuple[NotebookDefinitionPayload, ...],
    str | None,
]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return (), (), exc.msg
    imports: list[NotebookImportPayload] = []
    defs: list[NotebookDefinitionPayload] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, "import", node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            imports.append((module, "from", node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append((node.name, "function", node.lineno))
        elif isinstance(node, ast.ClassDef):
            defs.append((node.name, "class", node.lineno))
    return tuple(imports), tuple(defs), None


def _try_parse_notebook(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _notebook_cutoff_token(text: str) -> tuple[str, ...]:
    """Project the notebook text to the parts that affect analysis.

    Outputs and per-execution metadata are stripped so that running cells
    (which only changes ``outputs`` and ``execution_count``) backdates and
    leaves downstream consumers untouched. JSON-decode failures fall back to
    the raw text so a malformed notebook still has a stable cutoff.
    """
    parsed = _try_parse_notebook(text)
    if parsed is None:
        return ("raw", text)
    cells_raw = parsed.get("cells", [])
    if not isinstance(cells_raw, list):
        return ("raw", text)
    metadata = parsed.get("metadata", {})
    kernel_name: str | None = None
    language: str | None = None
    if isinstance(metadata, dict):
        kernelspec = metadata.get("kernelspec")
        if isinstance(kernelspec, dict):
            ks_name = kernelspec.get("name")
            if isinstance(ks_name, str):
                kernel_name = ks_name
            ks_lang = kernelspec.get("language")
            if isinstance(ks_lang, str):
                language = ks_lang
        language_info = metadata.get("language_info")
        if language is None and isinstance(language_info, dict):
            li_name = language_info.get("name")
            if isinstance(li_name, str):
                language = li_name
    parts: list[str] = ["nb", repr(kernel_name), repr(language)]
    for raw_cell in cells_raw:
        if not isinstance(raw_cell, dict):
            parts.append("invalid-cell")
            continue
        cell_type = str(raw_cell.get("cell_type", ""))
        source = _coerce_source(raw_cell.get("source"))
        parts.append(cell_type)
        parts.append(source)
    return tuple(parts)


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query(cutoff=_notebook_cutoff_token)
def notebook_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def notebook_cells_payload(db: Database, path: str) -> tuple[NotebookCellPayload, ...]:
    text = notebook_text(db, path)
    parsed = _try_parse_notebook(text)
    if parsed is None:
        return ()
    cells_raw = parsed.get("cells", [])
    if not isinstance(cells_raw, list):
        return ()
    cells: list[NotebookCellPayload] = []
    for index, raw_cell in enumerate(cells_raw):
        if not isinstance(raw_cell, dict):
            continue
        cell_type = _classify_cell_type(raw_cell.get("cell_type"))
        source = _coerce_source(raw_cell.get("source"))
        heading: str | None = None
        imports: tuple[NotebookImportPayload, ...] = ()
        definitions: tuple[NotebookDefinitionPayload, ...] = ()
        if cell_type == "markdown":
            heading = _first_markdown_heading(source)
        elif cell_type == "code":
            imports, definitions, _ = _extract_code_imports_and_defs(source)
        cells.append((index, cell_type, source, heading, imports, definitions))
    return tuple(cells)


@query
def notebook_diagnostics_payload(db: Database, path: str) -> tuple[NotebookDiagnosticPayload, ...]:
    text = notebook_text(db, path)
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return (("notebook-decode-error", str(exc), None),)
    if not isinstance(parsed, dict):
        return (("notebook-shape-error", "top-level value is not an object", None),)
    cells_raw = parsed.get("cells")
    if cells_raw is None:
        return (("notebook-shape-error", "missing 'cells' field", None),)
    if not isinstance(cells_raw, list):
        return (("notebook-shape-error", "'cells' is not a list", None),)
    diagnostics: list[NotebookDiagnosticPayload] = []
    for index, raw_cell in enumerate(cells_raw):
        if not isinstance(raw_cell, dict):
            diagnostics.append(("notebook-shape-error", "cell is not an object", index))
            continue
        cell_type = _classify_cell_type(raw_cell.get("cell_type"))
        if cell_type == "code":
            source = _coerce_source(raw_cell.get("source"))
            _, _, err = _extract_code_imports_and_defs(source)
            if err is not None:
                diagnostics.append(("syntax-error", err, index))
    return tuple(diagnostics)


@query
def notebook_metadata_payload(db: Database, path: str) -> tuple[str | None, str | None]:
    text = notebook_text(db, path)
    parsed = _try_parse_notebook(text)
    if parsed is None:
        return (None, None)
    metadata = parsed.get("metadata", {})
    if not isinstance(metadata, dict):
        return (None, None)
    kernel_name: str | None = None
    language: str | None = None
    kernelspec = metadata.get("kernelspec")
    if isinstance(kernelspec, dict):
        ks_name = kernelspec.get("name")
        if isinstance(ks_name, str):
            kernel_name = ks_name
        ks_lang = kernelspec.get("language")
        if isinstance(ks_lang, str):
            language = ks_lang
    if language is None:
        language_info = metadata.get("language_info")
        if isinstance(language_info, dict):
            li_name = language_info.get("name")
            if isinstance(li_name, str):
                language = li_name
    return (kernel_name, language)


@query
def notebook_analysis_payload(db: Database, path: str) -> NotebookAnalysisPayload:
    cells = notebook_cells_payload(db, path)
    diagnostics = notebook_diagnostics_payload(db, path)
    kernel_name, language = notebook_metadata_payload(db, path)
    return (path, kernel_name, language, cells, diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_cell(payload: NotebookCellPayload) -> NotebookCell:
    index, cell_type, source, heading, imports, definitions = payload
    return NotebookCell(
        index=index,
        cell_type=cell_type,
        source=source,
        heading=heading,
        imports=tuple(
            NotebookImport(module=m, kind=k, lineno=ln) for m, k, ln in imports
        ),
        definitions=tuple(
            NotebookDefinition(name=n, kind=k, lineno=ln) for n, k, ln in definitions
        ),
    )


def _decode_diagnostic(payload: NotebookDiagnosticPayload) -> NotebookDiagnostic:
    code, message, cell_index = payload
    return NotebookDiagnostic(code=code, message=message, cell_index=cell_index)


def notebook_analysis(db: Database, path: str | os.PathLike[str]) -> NotebookAnalysis:
    normalized = os.fspath(path)
    payload = cast(
        NotebookAnalysisPayload, thaw(db.get(notebook_analysis_payload, normalized))
    )
    path_str, kernel_name, language, cells, diagnostics = payload
    return NotebookAnalysis(
        path=path_str,
        kernel_name=kernel_name,
        language=language,
        cells=tuple(_decode_cell(c) for c in cells),
        diagnostics=tuple(_decode_diagnostic(d) for d in diagnostics),
    )


def workspace_notebook_analysis(
    db: Database,
    root: str | os.PathLike[str],
) -> tuple[NotebookAnalysis, ...]:
    normalized_root = os.fspath(root)
    try:
        entries = _DIRECTORIES.read(db, normalized_root)
    except NotADirectoryError:
        return ()
    base = Path(normalized_root)
    notebook_paths = tuple(
        str(base / name) for name in entries if name.endswith(".ipynb")
    )
    return tuple(notebook_analysis(db, p) for p in notebook_paths)


__all__ = [
    "NotebookAnalysis",
    "NotebookCell",
    "NotebookDefinition",
    "NotebookDiagnostic",
    "NotebookImport",
    "notebook_analysis",
    "workspace_notebook_analysis",
]
