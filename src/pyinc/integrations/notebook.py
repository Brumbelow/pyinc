from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import thaw

from ._resources import file_probe, file_read_snapshot, file_text
from .source_geometry import DocumentMap, SourcePosition, SourceRange, identifier_range

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
    range: SourceRange


@dataclass(frozen=True)
class NotebookDefinition:
    name: str
    kind: DefinitionKind
    range: SourceRange


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
    range: SourceRange | None


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
        return cast(str, db.read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"notebookfile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        return file_probe(path)

    def load(self, db: Database, path: str) -> str:
        text = file_text(path, self.encoding)
        return text if text is not None else ""

    def probe_and_load(self, db: Database, path: str) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = file_read_snapshot(path, self.encoding)
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


# ---------------------------------------------------------------------------
# IPython syntax
# ---------------------------------------------------------------------------

# A cell magic owns the rest of its cell. These hand that body back to the
# Python compiler; every other cell magic hands it to something else entirely,
# so the body holds no Python to import from or define.
_PYTHON_BODY_CELL_MAGICS = frozenset(
    {"capture", "debug", "prun", "python", "python2", "python3", "time", "timeit"}
)

# Patterns stay uncompiled: a compiled ``re.Pattern`` is not a snapshot-safe
# capture, and these helpers are reached from cached queries.
_LINE_BREAK = r"\r\n|\r|\n"
_CELL_MAGIC = r"%%([A-Za-z_]\w*)"
# ``%`` is modulo and ``!`` is half of ``!=``, so these only read as notebook
# syntax where IPython itself reads them: at the start of a logical line.
_MAGIC_LINE = r"[ \t]*%{1,2}[A-Za-z_]"
_SHELL_LINE = r"[ \t]*!{1,2}(?!=)"
_HELP_PREFIX_LINE = r"[ \t]*\?{1,2}[ \t]*(?:[A-Za-z_%*]|$)"
_HELP_SUFFIX_LINE = r"[ \t]*[A-Za-z_][\w.]*(?:\(.*\))?\?{1,2}[ \t]*$"
_CAPTURE_LINE = (
    r"[ \t]*[A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*[ \t]*=[ \t]*(?:%{1,2}[A-Za-z_]|!(?!=))"
)
_IPYTHON_LINES = (
    _MAGIC_LINE,
    _SHELL_LINE,
    _HELP_PREFIX_LINE,
    _HELP_SUFFIX_LINE,
    _CAPTURE_LINE,
)


def _split_lines(source: str) -> tuple[tuple[str, str], ...]:
    """Split into ``(content, terminator)`` pairs the way ``DocumentMap`` does."""
    parts: list[tuple[str, str]] = []
    position = 0
    for match in re.finditer(_LINE_BREAK, source):
        parts.append((source[position : match.start()], match.group()))
        position = match.end()
    parts.append((source[position:], ""))
    return tuple(parts)


def _logical_line_starts(contents: tuple[str, ...]) -> tuple[bool, ...]:
    """Mark the physical lines that open a logical line.

    IPython only reads a magic where a statement could start, so this is what
    keeps a string literal or a bracketed continuation that happens to hold a
    magic-looking line from being rewritten.
    """
    starts: list[bool] = []
    quote = ""
    depth = 0
    continued = False
    for content in contents:
        starts.append(not quote and depth == 0 and not continued)
        commented = False
        index = 0
        while index < len(content):
            character = content[index]
            if quote:
                if character == "\\":
                    index += 2
                elif content.startswith(quote, index):
                    index += len(quote)
                    quote = ""
                else:
                    index += 1
                continue
            if character == "#":
                commented = True
                break
            if character in "\"'":
                quote = character * 3 if content.startswith(character * 3, index) else character
                index += len(quote)
                continue
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth = max(depth - 1, 0)
            index += 1
        if len(quote) == 1:
            # Only a triple-quoted string carries across a line break on its own.
            quote = ""
        continued = not quote and not commented and content.endswith("\\")
    return tuple(starts)


def _placeholder_line(content: str) -> str:
    """Return an equal-width Python statement standing in for ``content``."""
    stripped = content.lstrip(" \t")
    indent = content[: len(content) - len(stripped)]
    return f"{indent}0{' ' * (len(stripped) - 1)}"


def _neutralize_notebook_syntax(source: str) -> tuple[str, bool]:
    """Replace IPython-only lines with equal-width Python placeholders.

    Real notebooks open with lines such as ``%matplotlib inline`` that ``ast``
    rejects, which would otherwise cost the whole cell its imports and
    definitions. Every rewritten line keeps its exact width and terminator, so
    each position in the result still names the same position in the notebook
    and the ranges decoded from it stay truthful. Returns the rewritten source
    and whether anything was rewritten.
    """
    lines = _split_lines(source)
    contents = tuple(content for content, _ in lines)
    cell_magic = re.match(_CELL_MAGIC, contents[0])
    if cell_magic is not None and cell_magic.group(1) not in _PYTHON_BODY_CELL_MAGICS:
        # The magic claims the rest of the cell, and that body is not Python.
        return "".join(" " * len(content) + eol for content, eol in lines), True
    rewritten: list[str] = []
    changed = False
    for (content, eol), starts_line in zip(lines, _logical_line_starts(contents), strict=True):
        if starts_line and any(re.match(pattern, content) for pattern in _IPYTHON_LINES):
            rewritten.append(_placeholder_line(content) + eol)
            changed = True
        else:
            rewritten.append(content + eol)
    return "".join(rewritten), changed


def _parse_cell_source(
    source: str,
) -> tuple[ast.Module | None, str, tuple[str, SyntaxError] | None]:
    """Parse a code cell, neutralizing notebook syntax when plain Python fails.

    Returns the module, the source it was parsed from — geometrically identical
    to ``source`` — and a ``(code, error)`` pair when it never parsed. A cell
    that holds notebook syntax reports a different code than a cell whose
    Python is simply wrong.
    """
    try:
        return ast.parse(source), source, None
    except SyntaxError as python_error:
        neutralized, rewritten = _neutralize_notebook_syntax(source)
        if not rewritten:
            return None, source, ("syntax-error", python_error)
        try:
            return ast.parse(neutralized), neutralized, None
        except SyntaxError as exc:
            return None, neutralized, ("notebook-non-python-cell", exc)


_CELL_PARSE_CODES = ("syntax-error", "notebook-non-python-cell")


def _cell_parse_diagnostic(parse_error: tuple[str, SyntaxError] | None) -> tuple[str, str] | None:
    if parse_error is None:
        return None
    code, error = parse_error
    if code == "syntax-error":
        return code, error.msg
    return code, f"cell is not Python after neutralizing notebook syntax: {error.msg}"


def _extract_code_imports_and_defs(
    source: str,
) -> tuple[
    tuple[NotebookImportPayload, ...],
    tuple[NotebookDefinitionPayload, ...],
    tuple[str, str] | None,
]:
    tree, _parsed_source, parse_error = _parse_cell_source(source)
    imports: list[NotebookImportPayload] = []
    defs: list[NotebookDefinitionPayload] = []
    for node in tree.body if tree is not None else ():
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
    return tuple(imports), tuple(defs), _cell_parse_diagnostic(parse_error)


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
            _, _, diagnostic = _extract_code_imports_and_defs(source)
            if diagnostic is not None:
                code, message = diagnostic
                diagnostics.append((code, message, index))
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


def _payload_range(lineno: int) -> SourceRange:
    position = SourcePosition(max(lineno - 1, 0), 0)
    return SourceRange(position, position)


def _definition_name_range(
    document: DocumentMap,
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> SourceRange:
    return identifier_range(document.source, node, node.name)


def _cell_ranges(
    source: str,
) -> tuple[dict[int, SourceRange], dict[tuple[int, str], SourceRange]]:
    tree, parsed_source, _parse_error = _parse_cell_source(source)
    if tree is None:
        return {}, {}
    # Neutralization preserves every line and column, so ranges taken from the
    # parsed source still name notebook positions.
    document = DocumentMap(parsed_source)
    imports: dict[int, SourceRange] = {}
    definitions: dict[tuple[int, str], SourceRange] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.setdefault(node.lineno, document.ast_range(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[(node.lineno, node.name)] = _definition_name_range(document, node)
    return imports, definitions


def _decode_cell(payload: NotebookCellPayload) -> NotebookCell:
    index, cell_type, source, heading, imports, definitions = payload
    import_ranges, definition_ranges = _cell_ranges(source)
    return NotebookCell(
        index=index,
        cell_type=cell_type,
        source=source,
        heading=heading,
        imports=tuple(
            NotebookImport(
                module=module,
                kind=kind,
                range=import_ranges.get(lineno, _payload_range(lineno)),
            )
            for module, kind, lineno in imports
        ),
        definitions=tuple(
            NotebookDefinition(
                name=name,
                kind=kind,
                range=definition_ranges.get((lineno, name), _payload_range(lineno)),
            )
            for name, kind, lineno in definitions
        ),
    )


def _syntax_error_range(source: str) -> SourceRange | None:
    _tree, _parsed_source, parse_error = _parse_cell_source(source)
    if parse_error is None:
        return None
    _code, exc = parse_error
    if exc.lineno is None:
        return None
    start = SourcePosition(max(exc.lineno - 1, 0), max((exc.offset or 1) - 1, 0))
    end = SourcePosition(
        max((exc.end_lineno or exc.lineno) - 1, 0),
        max((exc.end_offset or exc.offset or 1) - 1, 0),
    )
    if end <= start:
        end = SourcePosition(start.line, start.character + 1)
    return SourceRange(start, end)


def _decode_diagnostic(
    payload: NotebookDiagnosticPayload,
    cells: tuple[NotebookCell, ...],
    notebook_text_value: str,
) -> NotebookDiagnostic:
    code, message, cell_index = payload
    source_range: SourceRange | None = None
    if code in _CELL_PARSE_CODES and cell_index is not None:
        cell = next((item for item in cells if item.index == cell_index), None)
        if cell is not None:
            source_range = _syntax_error_range(cell.source)
    elif code == "notebook-decode-error":
        try:
            json.loads(notebook_text_value)
        except json.JSONDecodeError as exc:
            start = SourcePosition(exc.lineno - 1, exc.colno - 1)
            source_range = SourceRange(start, SourcePosition(start.line, start.character + 1))
    return NotebookDiagnostic(
        code=code,
        message=message,
        cell_index=cell_index,
        range=source_range,
    )


def notebook_analysis(db: Database, path: str | os.PathLike[str]) -> NotebookAnalysis:
    normalized = os.fspath(path)
    payload = cast(NotebookAnalysisPayload, thaw(db.get(notebook_analysis_payload, normalized)))
    path_str, kernel_name, language, cells, diagnostics = payload
    decoded_cells = tuple(_decode_cell(c) for c in cells)
    text = notebook_text(db, normalized)
    return NotebookAnalysis(
        path=path_str,
        kernel_name=kernel_name,
        language=language,
        cells=decoded_cells,
        diagnostics=tuple(_decode_diagnostic(d, decoded_cells, text) for d in diagnostics),
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
    notebook_paths = tuple(str(base / name) for name in entries if name.endswith(".ipynb"))
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
