from __future__ import annotations

import ast
import contextlib
import keyword
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pyinc import Database
from pyinc.integrations import (
    ConfigAnalysis,
    DependencyCheckAnalysis,
    DependencyStatus,
    DependencySurface,
    ModuleSymbolTable,
    Parameter,
    PythonModuleAnalysis,
    PythonWorkspaceAnalysis,
    Reference,
    ReferenceQueryResult,
    RequirementsAnalysis,
    ResolvedImportRef,
    ResolvedSymbol,
    Signature,
    Symbol,
    WorkspaceSymbolIndex,
    find_references,
    module_analysis,
    module_symbol_table,
    resolve_symbol,
    workspace_analysis,
    workspace_config_analysis,
    workspace_dependency_check,
    workspace_requirements_analysis,
    workspace_symbol_index,
)

DiagnosticSeverity = Literal["error", "warning", "information", "hint"]

DEFAULT_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "dist",
    }
)


@dataclass(frozen=True)
class AnalysisDiagnostic:
    path: str
    code: str
    message: str
    severity: DiagnosticSeverity
    source: str
    lineno: int | None = None
    col_offset: int | None = None


@dataclass(frozen=True)
class FileAnalysisResult:
    path: str
    module: PythonModuleAnalysis | None
    symbols: ModuleSymbolTable | None
    dependency_check: DependencyCheckAnalysis
    diagnostics: tuple[AnalysisDiagnostic, ...]


@dataclass(frozen=True)
class WorkspaceAnalysisResult:
    root: str
    python: PythonWorkspaceAnalysis
    symbols: WorkspaceSymbolIndex
    dependency_check: DependencyCheckAnalysis
    files: tuple[FileAnalysisResult, ...]
    diagnostics: tuple[AnalysisDiagnostic, ...]


RenameStatus = Literal[
    "ok",
    "non_workspace_target",
    "invalid_identifier",
    "keyword_identifier",
    "same_name",
    "alias_rename_unsupported",
]


@dataclass(frozen=True)
class RenameEdit:
    path: str
    lineno: int
    col_offset: int
    end_col_offset: int
    new_text: str


@dataclass(frozen=True)
class RenameResult:
    target: ResolvedSymbol
    edits: tuple[RenameEdit, ...]
    status: RenameStatus


@dataclass(frozen=True)
class FileRenameEdit:
    """A text edit returned by ``import_edits_for_file_renames``.

    All position fields are 0-based (LSP-style).
    """

    path: str
    start_line: int
    start_character: int
    end_line: int
    end_character: int
    new_text: str


@dataclass(frozen=True)
class FileDeletionEdit:
    """A text edit returned by ``import_edits_for_file_deletions``.

    Represents the deletion of an ``import`` / ``from`` statement (or a
    single alias inside one) that references a Python file that is about
    to be removed from the workspace. The edit's range covers the source
    span to be removed; ``new_text`` is always the empty string.

    All position fields are 0-based (LSP-style).
    """

    path: str
    start_line: int
    start_character: int
    end_line: int
    end_character: int
    new_text: str = ""


DocumentHighlightKind = Literal["text", "read", "write"]


@dataclass(frozen=True)
class DocumentHighlight:
    lineno: int
    col_offset: int
    end_col_offset: int
    kind: DocumentHighlightKind


@dataclass(frozen=True)
class LinkedEditingRange:
    lineno: int
    col_offset: int
    end_col_offset: int


@dataclass(frozen=True)
class SignatureParameterInfo:
    label: str
    label_offset_start: int
    label_offset_end: int


@dataclass(frozen=True)
class SignatureHelp:
    label: str
    parameters: tuple[SignatureParameterInfo, ...]
    active_parameter: int | None


FoldingRangeKind = Literal["imports", "comment", "region"]


@dataclass(frozen=True)
class FoldingRange:
    start_line: int
    end_line: int
    kind: FoldingRangeKind


@dataclass(frozen=True)
class SelectionRange:
    start_line: int
    start_character: int
    end_line: int
    end_character: int


@dataclass(frozen=True)
class DocumentLink:
    start_line: int
    start_character: int
    end_line: int
    end_character: int
    target_path: str


@dataclass(frozen=True)
class CodeLens:
    start_line: int
    start_character: int
    end_line: int
    end_character: int
    title: str


@dataclass(frozen=True)
class TypeDefinitionLocation:
    path: str
    lineno: int
    col_offset: int
    end_col_offset: int


@dataclass(frozen=True)
class DeclarationLocation:
    """Location where the identifier under the cursor is *declared* in the
    current file.

    Distinct from :class:`TypeDefinitionLocation` / ``textDocument/definition``:
    ``definition`` follows ``import`` / ``from … import`` chains through to
    the imported target's defining file, while ``declaration`` stops at the
    binding statement in the current file. For workspace functions, classes,
    and module-level variables the two coincide; for ``import_alias``,
    ``from_import_alias``, and ``wildcard_import_stub`` symbols they differ.

    All offsets are 1-based for ``lineno`` (matching ``Symbol.lineno`` and the
    rest of the session API); ``col_offset`` / ``end_col_offset`` are 0-based
    column positions, matching the rest of the session dataclasses. The LSP
    layer subtracts 1 from ``lineno`` to produce the LSP 0-based shape.
    """

    path: str
    lineno: int
    col_offset: int
    end_col_offset: int


InlayHintKind = Literal["parameter", "type"]


@dataclass(frozen=True)
class InlayHint:
    line: int
    character: int
    label: str
    kind: InlayHintKind
    padding_left: bool
    padding_right: bool


SemanticTokenType = Literal[
    "namespace",
    "class",
    "function",
    "method",
    "parameter",
    "variable",
]

SemanticTokenModifier = Literal["declaration", "async"]


@dataclass(frozen=True)
class SemanticToken:
    line: int
    character: int
    length: int
    token_type: SemanticTokenType
    token_modifiers: tuple[SemanticTokenModifier, ...]


CallHierarchyItemKind = Literal["function", "method", "class"]


@dataclass(frozen=True)
class CallHierarchyItem:
    name: str
    kind: CallHierarchyItemKind
    path: str
    qualified_name: str
    detail: str | None
    range_start_line: int
    range_start_character: int
    range_end_line: int
    range_end_character: int
    selection_start_line: int
    selection_start_character: int
    selection_end_line: int
    selection_end_character: int


@dataclass(frozen=True)
class CallHierarchyCallSite:
    start_line: int
    start_character: int
    end_line: int
    end_character: int


@dataclass(frozen=True)
class CallHierarchyIncomingCall:
    caller: CallHierarchyItem
    call_sites: tuple[CallHierarchyCallSite, ...]


@dataclass(frozen=True)
class CallHierarchyOutgoingCall:
    callee: CallHierarchyItem
    call_sites: tuple[CallHierarchyCallSite, ...]


TypeHierarchyItemKind = Literal["class"]


@dataclass(frozen=True)
class TypeHierarchyItem:
    """A single class entry returned by the type-hierarchy endpoints.

    The shape mirrors :class:`CallHierarchyItem`: ``range`` covers the whole
    ``class`` block (including any decorator lines), ``selection_*`` spans
    the bare class name on the header line, and ``qualified_name`` follows
    the ``module_symbol_table`` convention (``Outer.Inner`` for nested
    classes). ``detail`` is the declaring module name when known.

    The ``kind`` field is fixed to ``"class"`` for now; type hierarchies
    only surface classes, mirroring the LSP spec.
    """

    name: str
    kind: TypeHierarchyItemKind
    path: str
    qualified_name: str
    detail: str | None
    range_start_line: int
    range_start_character: int
    range_end_line: int
    range_end_character: int
    selection_start_line: int
    selection_start_character: int
    selection_end_line: int
    selection_end_character: int


@dataclass(frozen=True)
class _DependencyInputs:
    config: ConfigAnalysis | None
    requirements: RequirementsAnalysis | None
    declared_dependencies: tuple[str, ...]


def _normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _resolve_import_from_target(
    *,
    importer_module: str,
    importer_path: str,
    level: int,
    module: str | None,
) -> str | None:
    if level == 0:
        return module
    package_parts = [part for part in importer_module.split(".") if part]
    if package_parts and Path(importer_path).name != "__init__.py":
        package_parts = package_parts[:-1]
    if level - 1 > len(package_parts):
        return None
    anchor = package_parts[: len(package_parts) - (level - 1)]
    base_parts = [part for part in (module or "").split(".") if part]
    return ".".join(anchor + base_parts)


def _relative_import_anchor(
    *, importer_module: str, importer_path: str, level: int
) -> str | None:
    """Return the dotted-package anchor a relative import resolves against.

    Mirrors :func:`_resolve_import_from_target`'s level math: starts from
    ``importer_module``, drops the trailing component when the importer is
    not a package (``__init__.py``), then walks up ``level - 1`` more
    components. Returns ``None`` when ``level`` overshoots the available
    package depth, and ``""`` for ``level == 0`` (no anchor — absolute
    import).
    """
    if level == 0:
        return ""
    package_parts = [part for part in importer_module.split(".") if part]
    if package_parts and Path(importer_path).name != "__init__.py":
        package_parts = package_parts[:-1]
    if level - 1 > len(package_parts):
        return None
    return ".".join(package_parts[: len(package_parts) - (level - 1)])


def _find_from_module_span(
    source_lines: list[str], node: ast.ImportFrom
) -> tuple[int, int, int] | None:
    """Locate the dotted-module span of an ``ast.ImportFrom`` in source.

    Returns ``(line_index, start_column, end_column)`` — 0-based — for the
    ``".".joinedmodule`` portion between ``from`` and ``import`` (including
    any leading dots). Returns ``None`` when the span can't be located
    unambiguously on the statement's header line.
    """
    line_idx = node.lineno - 1
    if not (0 <= line_idx < len(source_lines)):
        return None
    line = source_lines[line_idx]
    expected = ("." * node.level) + (node.module or "")
    if not expected:
        return None
    cursor = node.col_offset
    if line[cursor : cursor + 4] != "from":
        return None
    cursor += 4
    while cursor < len(line) and line[cursor] in " \t":
        cursor += 1
    if line[cursor : cursor + len(expected)] != expected:
        return None
    end = cursor + len(expected)
    if end < len(line) and (line[end].isalnum() or line[end] in "._"):
        return None
    return (line_idx, cursor, end)


def _statement_line_span(source: str, node: ast.stmt) -> tuple[int, int] | None:
    """Return ``(start_line, end_line)`` for an import statement to delete.

    Both values are 0-based LSP-style line indices. ``start_line`` is the
    statement's first line (``node.lineno - 1``); ``end_line`` is one past
    the statement's last source line — i.e. the line index where the next
    statement starts. Pairing the two as ``{start: line_start, end:
    end_line_start}`` produces an LSP ``TextEdit`` range that removes the
    statement *including* its trailing newline, so neighbouring lines are
    not pulled up onto the same physical line.

    For an EOF-anchored statement (no trailing newline), ``end_line`` is
    clamped to the total number of source lines and the column at the end
    of the last line is folded back into the line index by setting
    ``end_line == start_line + n``.
    """
    if node.end_lineno is None:
        return None
    start_line = node.lineno - 1
    end_line = node.end_lineno
    total_lines = source.count("\n") + 1
    if end_line > total_lines:
        end_line = total_lines
    return start_line, end_line


def _alias_list_deletion_edits(
    *,
    importer_path: str,
    source: str,
    aliases: list[ast.alias],
    dead_indices: list[int],
) -> list[FileDeletionEdit]:
    """Emit edits that remove specific aliases from an alias-list import.

    Used when only some aliases inside a single ``import a, b, c`` /
    ``from M import a, b, c`` statement are dead — the surviving aliases
    keep the statement intact. For each dead alias the edit covers the
    alias's name + ``as`` clause span plus an adjacent comma so the list
    stays well-formed afterwards.

    Aliases whose ``end_lineno`` / ``end_col_offset`` are missing are
    skipped (the surviving statement still references the deleted module
    but at least the file is not mis-edited).
    """
    del source  # AST positions are sufficient; the source is unused here.
    edits: list[FileDeletionEdit] = []
    dead = set(dead_indices)
    for i in dead_indices:
        alias = aliases[i]
        if (
            alias.lineno is None
            or alias.col_offset is None
            or alias.end_lineno is None
            or alias.end_col_offset is None
        ):
            continue
        alias_start_line = alias.lineno - 1
        alias_start_char = alias.col_offset
        alias_end_line = alias.end_lineno - 1
        alias_end_char = alias.end_col_offset

        # Decide which adjacent comma to absorb.
        prev_alive_idx: int | None = None
        for j in range(i - 1, -1, -1):
            if j not in dead:
                prev_alive_idx = j
                break
        next_alive_idx: int | None = None
        for j in range(i + 1, len(aliases)):
            if j not in dead:
                next_alive_idx = j
                break

        if next_alive_idx is not None:
            # Absorb the trailing comma + whitespace up to the next alive
            # alias's start, so the surviving alias slides into this slot.
            next_alias = aliases[next_alive_idx]
            if (
                next_alias.lineno is None
                or next_alias.col_offset is None
            ):
                continue
            end_line = next_alias.lineno - 1
            end_char = next_alias.col_offset
            edits.append(
                FileDeletionEdit(
                    path=importer_path,
                    start_line=alias_start_line,
                    start_character=alias_start_char,
                    end_line=end_line,
                    end_character=end_char,
                )
            )
        elif prev_alive_idx is not None:
            # No surviving alias after us — absorb the preceding comma so
            # the surviving alias before us doesn't end with a trailing `,`.
            prev_alias = aliases[prev_alive_idx]
            if (
                prev_alias.end_lineno is None
                or prev_alias.end_col_offset is None
            ):
                continue
            start_line = prev_alias.end_lineno - 1
            start_char = prev_alias.end_col_offset
            edits.append(
                FileDeletionEdit(
                    path=importer_path,
                    start_line=start_line,
                    start_character=start_char,
                    end_line=alias_end_line,
                    end_character=alias_end_char,
                )
            )
        else:
            # No surviving sibling at all — caller should have routed this
            # to the whole-statement removal path, but emit a span-only
            # edit defensively rather than misbehaving silently.
            edits.append(
                FileDeletionEdit(
                    path=importer_path,
                    start_line=alias_start_line,
                    start_character=alias_start_char,
                    end_line=alias_end_line,
                    end_character=alias_end_char,
                )
            )
    return edits


def _line_char_to_offset(source: str, line: int, character: int) -> int | None:
    pos = 0
    current_line = 0
    while current_line < line:
        nl = source.find("\n", pos)
        if nl == -1:
            return None
        pos = nl + 1
        current_line += 1
    line_end = source.find("\n", pos)
    if line_end == -1:
        line_end = len(source)
    return pos + min(character, line_end - pos)


def _identifier_at_source_position(
    source: str, line: int, character: int
) -> str | None:
    """Return the bare identifier covering ``(line, character)`` or ``None``.

    Coordinates are LSP-style 0-based. Matches the identifier-lookup the LSP
    layer applies for hover/definition: walk outward from the cursor while
    the characters are `[A-Za-z0-9_]`, and require the leading character to
    be `[A-Za-z_]`.
    """
    lines = source.splitlines()
    if not (0 <= line < len(lines)):
        return None
    text = lines[line]
    if not (0 <= character <= len(text)):
        return None
    start = character
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    end = character
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    if start == end:
        return None
    first = text[start]
    if not (first.isalpha() or first == "_"):
        return None
    return text[start:end]


def _identifier_immediately_before(source: str, paren_pos: int) -> str | None:
    """Return the identifier appearing immediately before `(` at `paren_pos`.

    Returns None when the preceding token is not a usable identifier — for
    example a closing bracket, a literal, a Python keyword, or the name of
    a `def` / `class` definition header (which is not a call site).
    """
    j = paren_pos - 1
    while j >= 0 and source[j] in " \t":
        j -= 1
    if j < 0 or not (source[j].isalnum() or source[j] == "_"):
        return None
    end = j + 1
    while j >= 0 and (source[j].isalnum() or source[j] == "_"):
        j -= 1
    start = j + 1
    name = source[start:end]
    if not name or name[0].isdigit():
        return None
    if keyword.iskeyword(name):
        return None
    k = start - 1
    while k >= 0 and source[k] in " \t":
        k -= 1
    if k >= 0 and (source[k].isalnum() or source[k] == "_"):
        prev_end = k + 1
        prev_start = prev_end
        while prev_start > 0 and (
            source[prev_start - 1].isalnum() or source[prev_start - 1] == "_"
        ):
            prev_start -= 1
        if source[prev_start:prev_end] in ("def", "class"):
            return None
    return name


def _find_call_at_position(
    source: str, line: int, character: int
) -> tuple[str, int] | None:
    """Locate the call-expression enclosing the cursor.

    Returns ``(function_name, active_parameter_index)`` or ``None``.

    The scanner runs forward over `source`, skipping comments and string
    literals, and tracks a stack of open brackets. The topmost open `(`
    whose preceding token is a usable identifier is the enclosing call;
    its accumulated comma count yields the active parameter index. Only
    bare-name calls are detected — attribute calls (``obj.method(``) and
    subscripted calls (``factory[T](``) are not recognised.
    """
    target = _line_char_to_offset(source, line, character)
    if target is None:
        return None

    stack: list[tuple[str, str | None, int]] = []
    n = len(source)
    i = 0
    while i < n and i < target:
        c = source[i]
        if c == "#":
            j = source.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c in ('"', "'"):
            triple = c * 3
            if source[i : i + 3] == triple:
                end = source.find(triple, i + 3)
                i = n if end == -1 else end + 3
                continue
            j = i + 1
            while j < n:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == c:
                    j += 1
                    break
                if ch == "\n":
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c in "([{":
            name = _identifier_immediately_before(source, i) if c == "(" else None
            stack.append((c, name, 0))
            i += 1
            continue
        if c in ")]}":
            opener = "(" if c == ")" else ("[" if c == "]" else "{")
            if stack and stack[-1][0] == opener:
                stack.pop()
            i += 1
            continue
        if c == "," and stack:
            opener_top, name_top, commas = stack[-1]
            stack[-1] = (opener_top, name_top, commas + 1)
        i += 1

    for opener, name, commas in reversed(stack):
        if opener == "(" and name is not None:
            return name, commas
    return None


def _build_signature_label(
    name: str, signature: Signature
) -> tuple[str, tuple[SignatureParameterInfo, ...]]:
    parts: list[str] = [f"def {name}("]
    info: list[SignatureParameterInfo] = []
    for index, parameter in enumerate(signature.parameters):
        if index > 0:
            parts.append(", ")
        text = (
            f"{parameter.name}: {parameter.annotation}"
            if parameter.annotation is not None
            else parameter.name
        )
        offset = sum(len(piece) for piece in parts)
        parts.append(text)
        info.append(
            SignatureParameterInfo(
                label=text,
                label_offset_start=offset,
                label_offset_end=offset + len(text),
            )
        )
    parts.append(")")
    if signature.return_annotation is not None:
        parts.append(f" -> {signature.return_annotation}")
    return "".join(parts), tuple(info)


def _collect_annotation_type_refs(
    annotation: str,
) -> tuple[tuple[str, ...], ...]:
    """Parse `annotation` and return a tuple of type-name references.

    Each entry is either ``("name", id)`` for a bare-name reference or
    ``("attribute", lhs_id, attr)`` for an ``lhs.attr`` reference where the
    LHS is itself a bare name. Attribute chains whose LHS is not a bare
    `Name` (e.g. ``pkg.sub.Foo``) are skipped — only the rightmost-bare-LHS
    shape is supported, mirroring the resolver's existing handling for
    references.

    A whole-string forward reference (``"Foo"``, ``"pkg.Foo | None"``) is
    unwrapped exactly once before walking. Malformed annotation text returns
    an empty tuple.
    """
    try:
        tree = ast.parse(annotation, mode="eval")
    except SyntaxError:
        return ()
    body = tree.body
    if isinstance(body, ast.Constant) and isinstance(body.value, str):
        try:
            body = ast.parse(body.value, mode="eval").body
        except SyntaxError:
            return ()

    refs: list[tuple[str, ...]] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            refs.append(("attribute", node.value.id, node.attr))
            return
        if isinstance(node, ast.Name):
            refs.append(("name", node.id))
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(body)
    return tuple(refs)


def _compute_folding_ranges(source: str) -> tuple[FoldingRange, ...]:
    """Walk the AST of `source` and emit `FoldingRange` entries.

    Folds:
    - `def`, `async def`, and `class` blocks (header line stays visible, body
      folds). Decorated definitions start at the first decorator line so the
      decorator block + def + body collapse together below that line.
    - Runs of consecutive top-level `import` / `from ... import` statements
      that span more than one source line in total. Mixed `import` and
      `from-import` lines without a blank line between them are grouped.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    ranges: list[FoldingRange] = []

    def walk_definitions(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                if node.decorator_list:
                    start = min(dec.lineno for dec in node.decorator_list)
                else:
                    start = node.lineno
                end = node.end_lineno or node.lineno
                if end > start:
                    ranges.append(
                        FoldingRange(
                            start_line=start,
                            end_line=end,
                            kind="region",
                        )
                    )
                walk_definitions(list(node.body))
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                walk_definitions(list(node.body))
                walk_definitions(list(getattr(node, "orelse", []) or []))
                walk_definitions(list(getattr(node, "finalbody", []) or []))
                for handler in getattr(node, "handlers", []) or []:
                    walk_definitions(list(handler.body))

    walk_definitions(list(tree.body))

    run_start: int | None = None
    run_end: int | None = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if run_start is None:
                run_start = node.lineno
            run_end = node.end_lineno or node.lineno
        else:
            if run_start is not None and run_end is not None and run_end > run_start:
                ranges.append(
                    FoldingRange(
                        start_line=run_start,
                        end_line=run_end,
                        kind="imports",
                    )
                )
            run_start = None
            run_end = None
    if run_start is not None and run_end is not None and run_end > run_start:
        ranges.append(
            FoldingRange(
                start_line=run_start,
                end_line=run_end,
                kind="imports",
            )
        )

    ranges.sort(key=lambda r: (r.start_line, r.end_line))
    return tuple(ranges)


def _compute_selection_chain(
    source: str, line: int, character: int
) -> tuple[SelectionRange, ...]:
    """Walk the AST of `source` and return a chain of nested ranges around the cursor.

    The chain is ordered innermost-first; each subsequent entry strictly contains its
    predecessor. Coordinates are 0-based (LSP-style) for both line and character.
    Returns `()` when the file fails to parse, the cursor is out of bounds, or no AST
    node contains the cursor.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    line_starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            line_starts.append(index + 1)
    line_count = len(line_starts)

    if line < 0 or line >= line_count:
        return ()
    line_start = line_starts[line]
    line_end = line_starts[line + 1] - 1 if line + 1 < line_count else len(source)
    if character < 0 or line_start + character > line_end + 1:
        return ()
    cursor = line_start + character

    candidates: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        start_lineno = getattr(node, "lineno", None)
        start_col = getattr(node, "col_offset", None)
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if (
            start_lineno is None
            or start_col is None
            or end_lineno is None
            or end_col is None
        ):
            continue
        if start_lineno < 1 or start_lineno > line_count:
            continue
        if end_lineno < 1 or end_lineno > line_count:
            continue
        start_offset = line_starts[start_lineno - 1] + start_col
        end_offset = line_starts[end_lineno - 1] + end_col
        if start_offset <= cursor <= end_offset and start_offset != end_offset:
            candidates.add((start_offset, end_offset))

    if not candidates:
        return ()

    sorted_candidates = sorted(candidates, key=lambda pair: (pair[1] - pair[0], pair[0]))

    chain: list[tuple[int, int]] = []
    for start_offset, end_offset in sorted_candidates:
        if not chain:
            chain.append((start_offset, end_offset))
            continue
        prev_start, prev_end = chain[-1]
        if (
            start_offset <= prev_start
            and end_offset >= prev_end
            and (start_offset < prev_start or end_offset > prev_end)
        ):
            chain.append((start_offset, end_offset))

    def offset_to_position(offset: int) -> tuple[int, int]:
        lo, hi = 0, line_count - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo, offset - line_starts[lo]

    selection_ranges: list[SelectionRange] = []
    for start_offset, end_offset in chain:
        sl, sc = offset_to_position(start_offset)
        el, ec = offset_to_position(end_offset)
        selection_ranges.append(
            SelectionRange(
                start_line=sl,
                start_character=sc,
                end_line=el,
                end_character=ec,
            )
        )
    return tuple(selection_ranges)


def _compute_document_links(
    source: str, resolved_imports: tuple[ResolvedImportRef, ...]
) -> tuple[DocumentLink, ...]:
    """Walk the AST of `source` and emit one `DocumentLink` per alias whose
    resolved import points at a workspace file.

    For `import M` / `import M as alias` / `import M.x` the link spans the
    `ast.alias` node (which covers any `as <alias>` suffix). For `from M
    import bar [, baz]` each alias is linked to its own resolved path —
    the same path goto-definition would jump to for the bound name. Aliases
    whose resolution is anything other than `"workspace"` (including
    stdlib / installed / missing / ambiguous) are skipped, mirroring the
    LSP's existing scope.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    import_targets: dict[tuple[int, str], str] = {}
    from_targets: dict[tuple[int, str], str] = {}
    for resolved in resolved_imports:
        if resolved.resolution != "workspace" or resolved.resolved_path is None:
            continue
        if resolved.kind == "import":
            import_targets[(resolved.lineno, resolved.module)] = resolved.resolved_path
        elif resolved.imported_name is not None:
            from_targets[(resolved.lineno, resolved.imported_name)] = (
                resolved.resolved_path
            )

    links: list[DocumentLink] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.end_lineno is None or alias.end_col_offset is None:
                    continue
                target = import_targets.get((node.lineno, alias.name))
                if target is None:
                    continue
                links.append(
                    DocumentLink(
                        start_line=alias.lineno - 1,
                        start_character=alias.col_offset,
                        end_line=alias.end_lineno - 1,
                        end_character=alias.end_col_offset,
                        target_path=target,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (
                    alias.name == "*"
                    or alias.end_lineno is None
                    or alias.end_col_offset is None
                ):
                    continue
                target = from_targets.get((node.lineno, alias.name))
                if target is None:
                    continue
                links.append(
                    DocumentLink(
                        start_line=alias.lineno - 1,
                        start_character=alias.col_offset,
                        end_line=alias.end_lineno - 1,
                        end_character=alias.end_col_offset,
                        target_path=target,
                    )
                )

    links.sort(key=lambda link: (link.start_line, link.start_character))
    return tuple(links)


_CallableNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _find_callable_node(
    tree: ast.Module, qualified_name: str
) -> _CallableNode | None:
    """Locate the FunctionDef/AsyncFunctionDef/ClassDef matching `qualified_name`.

    Matches `module_symbol_table`'s qualified-name convention: top-level
    `def f` / `class C` resolve to ``f`` / ``C``; methods inside a class body
    resolve to ``C.f``; nested classes inside a class body resolve to
    ``C.Inner``. Nested functions inside another function body are not in
    the symbol table and are therefore not matched here.
    """
    parts = qualified_name.split(".")
    if not parts or any(not part for part in parts):
        return None

    def walk(
        nodes: list[ast.stmt], remaining: list[str]
    ) -> _CallableNode | None:
        head = remaining[0]
        rest = remaining[1:]
        for node in nodes:
            if isinstance(node, ast.ClassDef) and node.name == head:
                if not rest:
                    return node
                found = walk(list(node.body), rest)
                if found is not None:
                    return found
            elif (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == head
            ):
                if not rest:
                    return node
                # Nested functions are not part of the symbol-table qualifier
                # scheme — stop descending.
        return None

    return walk(list(tree.body), parts)


def _enclosing_callable_qname(
    tree: ast.Module, known_qnames: frozenset[str], line: int
) -> str | None:
    """Innermost qualified name from `known_qnames` whose def/class span
    contains the 1-based source `line`.

    Qualifier follows the `module_symbol_table` convention: only `ClassDef`
    nesting contributes to the dotted path; nested function bodies do not
    extend the qualifier. Returns the deepest matching qname, or ``None`` if
    no enclosing def/class is in `known_qnames`.
    """
    best: tuple[int, str] | None = None

    def visit(node: ast.AST, class_qualifier: str) -> None:
        nonlocal best
        if isinstance(node, ast.ClassDef):
            qname = (
                f"{class_qualifier}.{node.name}" if class_qualifier else node.name
            )
            end_lineno = node.end_lineno or node.lineno
            if node.lineno <= line <= end_lineno and qname in known_qnames:
                span = end_lineno - node.lineno
                if best is None or span < best[0]:
                    best = (span, qname)
            for body_child in node.body:
                visit(body_child, qname)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = (
                f"{class_qualifier}.{node.name}" if class_qualifier else node.name
            )
            end_lineno = node.end_lineno or node.lineno
            if node.lineno <= line <= end_lineno and qname in known_qnames:
                span = end_lineno - node.lineno
                if best is None or span < best[0]:
                    best = (span, qname)
            # Nested defs/classes inside a function body are not in the
            # module symbol table; reset the class qualifier for any further
            # walk so a nested class can still be detected if it ever lands
            # in the table.
            for descendant in ast.iter_child_nodes(node):
                visit(descendant, "")
            return
        for descendant in ast.iter_child_nodes(node):
            visit(descendant, class_qualifier)

    visit(tree, "")
    return best[1] if best is not None else None


def _collect_outgoing_calls(
    body_node: _CallableNode,
) -> tuple[ast.Call, ...]:
    """Walk `body_node.body` for ``ast.Call`` nodes, skipping descent into
    any nested ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef`` /
    ``Lambda`` so each scope owns its own outgoing-call list.

    Comprehension scopes (`ListComp`, `SetComp`, `DictComp`, `GeneratorExp`)
    are walked through since they conceptually run inline.
    """
    calls: list[ast.Call] = []

    def walk(node: ast.AST) -> None:
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            return
        if isinstance(node, ast.Call):
            calls.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in body_node.body:
        walk(stmt)
    return tuple(calls)


def _call_func_range(call: ast.Call) -> tuple[int, int, int, int] | None:
    """Return the LSP-style 0-based range of `call.func`'s name span.

    For `Name(id=name)` it's the entire Name; for
    `Attribute(value=Name, attr=name)` it's just the rightmost-attribute
    span (matching `find_references`'s reporting convention). Returns
    ``None`` for any other call shape (subscripted, deep attribute chains,
    lambdas, etc.) so the caller can skip it.
    """
    func = call.func
    if isinstance(func, ast.Name):
        end_col = func.end_col_offset
        end_lineno = func.end_lineno
        if end_col is None or end_lineno is None:
            end_col = func.col_offset + len(func.id)
            end_lineno = func.lineno
        return (
            func.lineno - 1,
            func.col_offset,
            end_lineno - 1,
            end_col,
        )
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        end_col = func.end_col_offset
        end_lineno = func.end_lineno
        if end_col is None or end_lineno is None:
            return None
        attr_col = end_col - len(func.attr)
        if attr_col < 0:
            return None
        return (
            end_lineno - 1,
            attr_col,
            end_lineno - 1,
            end_col,
        )
    return None


def _unwrap_base_expression(node: ast.expr) -> tuple[str, ...] | None:
    """Map a ``ClassDef`` base expression to a resolver-ready tuple.

    Returns ``("name", id)`` for a bare ``Name``, or
    ``("attr", lhs_id, attr_name)`` for ``Name.attr``. ``Subscript``
    bases (``Generic[T]``, ``Base[T]``) are unwrapped to their ``value``
    once, so ``Base[T]`` resolves to ``Base``. ``Starred`` bases, deep
    attribute chains (``pkg.sub.Foo``), and call expressions are
    rejected by returning ``None`` — matching the LHS-bare-Name limit
    that ``find_references`` and ``call_hierarchy_outgoing_calls``
    apply.
    """
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return ("name", node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return ("attr", node.value.id, node.attr)
    return None


def _walk_class_definitions(
    tree: ast.Module,
) -> tuple[tuple[str, ast.ClassDef], ...]:
    """Yield every ``ClassDef`` in ``tree`` with its dotted qualifier.

    The qualifier follows ``module_symbol_table``'s scheme: only
    ``ClassDef`` nesting contributes to the dotted path — a class
    declared inside a function body is reported with its bare class
    name (no function qualifier), matching how the symbol table would
    have stored it had it been at module top level. Classes declared
    inside another class are reported as ``Outer.Inner``. Class bodies
    are walked recursively so arbitrary nesting depth is covered.
    """
    out: list[tuple[str, ast.ClassDef]] = []

    def walk(node: ast.AST, class_qualifier: str) -> None:
        if isinstance(node, ast.ClassDef):
            qname = (
                f"{class_qualifier}.{node.name}" if class_qualifier else node.name
            )
            out.append((qname, node))
            for body_child in node.body:
                walk(body_child, qname)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Classes declared inside a function body are not part of the
            # module symbol table's qualifier scheme; reset the class
            # qualifier so any nested class re-enters at "top level".
            for descendant in ast.iter_child_nodes(node):
                walk(descendant, "")
            return
        for descendant in ast.iter_child_nodes(node):
            walk(descendant, class_qualifier)

    walk(tree, "")
    return tuple(out)


def _inlay_hints_for_call(
    call: ast.Call,
    parameters: Sequence[Parameter],
) -> list[InlayHint]:
    """Pair each positional argument with the next positional parameter slot
    and emit one ``InlayHint`` with label ``"name:"`` per pair.

    Walks ``parameters`` left-to-right (which mirrors `_parameter_payloads_from_args`'s
    posonly-then-positional-then-vararg-then-kwonly-then-kwarg order). The
    encoding prefixes vararg parameter names with ``*`` and kwargs with
    ``**`` — both are skipped/stopped here:

    - ``**name`` cannot receive positional → silently skipped (kwonly args
      following a ``*`` are handled by the rule below).
    - ``*name`` absorbs all remaining positional args → iteration stops.

    Iteration also stops at the first ``ast.Starred`` argument in the call,
    since a `*spread` consumes an unknown number of slots and the pairing
    becomes ambiguous after that point. Hints are suppressed when the
    argument is itself a bare ``Name`` whose identifier matches the
    parameter name.
    """
    hints: list[InlayHint] = []
    param_index = 0
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            break
        while param_index < len(parameters):
            name = parameters[param_index].name
            if name.startswith("**"):
                param_index += 1
                continue
            if name.startswith("*"):
                return hints
            break
        if param_index >= len(parameters):
            break
        param_name = parameters[param_index].name
        param_index += 1
        if isinstance(arg, ast.Name) and arg.id == param_name:
            continue
        if arg.col_offset is None or arg.lineno is None:
            continue
        hints.append(
            InlayHint(
                line=arg.lineno - 1,
                character=arg.col_offset,
                label=f"{param_name}:",
                kind="parameter",
                padding_left=False,
                padding_right=True,
            )
        )
    return hints


_SYMBOL_KIND_TO_SEMANTIC_TOKEN_TYPE: dict[str, SemanticTokenType] = {
    "function": "function",
    "method": "method",
    "class": "class",
    "variable": "variable",
    "class_variable": "variable",
    "import_alias": "namespace",
}


def _locate_def_name_offsets_on_header(
    source_lines: Sequence[str], node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
) -> tuple[int, int, int] | None:
    """Return ``(line, col_offset, end_col_offset)`` of ``node.name`` on the
    definition's header line, scanning forward from ``node.col_offset``.

    The AST records the header line on ``node.lineno`` (in 3.8+ that's the
    ``def`` / ``class`` keyword line, even for decorated definitions), so the
    name lives on that same line. Returns ``None`` when the line is missing
    or the name cannot be located by a word-boundary search.
    """
    line_idx = node.lineno - 1
    if not (0 <= line_idx < len(source_lines)):
        return None
    line = source_lines[line_idx]
    pattern = re.compile(rf"\b{re.escape(node.name)}\b")
    match = pattern.search(line, node.col_offset)
    if match is None:
        return None
    return node.lineno, match.start(), match.end()


def _iter_function_args(args: ast.arguments) -> list[ast.arg]:
    """Return all ``ast.arg`` entries in posonly / positional / vararg /
    kwonly / kwarg slot order — the same order
    ``_parameter_payloads_from_args`` uses inside ``symbol_resolution``.
    """
    entries: list[ast.arg] = []
    entries.extend(args.posonlyargs)
    entries.extend(args.args)
    if args.vararg is not None:
        entries.append(args.vararg)
    entries.extend(args.kwonlyargs)
    if args.kwarg is not None:
        entries.append(args.kwarg)
    return entries


def _compute_semantic_tokens(
    source: str, symbol_table: ModuleSymbolTable
) -> tuple[SemanticToken, ...]:
    """Walk ``source``'s AST and emit semantic tokens for declarations
    (function / method / class headers and function parameters) and for bare
    ``ast.Name`` uses whose identifier matches a top-level entry in
    ``symbol_table``.

    Files that fail to parse return ``()``. Token coordinates are 0-based
    (LSP-style); the returned tuple is sorted by ``(line, character)``.

    Use-site classification covers only top-level bare-name lookups in the
    file's own symbol table; attribute access, function-local shadowing, and
    cross-module re-export following are intentionally out of scope (they
    match the existing limitations of ``find_references`` / ``inlayHint``).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    lines = source.splitlines()

    name_to_token_type: dict[str, SemanticTokenType] = {}
    for symbol in symbol_table.symbols:
        if "." in symbol.qualified_name:
            continue
        token_type = _SYMBOL_KIND_TO_SEMANTIC_TOKEN_TYPE.get(symbol.kind)
        if token_type is None:
            continue
        name_to_token_type.setdefault(symbol.qualified_name, token_type)

    tokens: list[SemanticToken] = []

    def emit(
        lineno: int,
        col_offset: int,
        length: int,
        token_type: SemanticTokenType,
        modifiers: tuple[SemanticTokenModifier, ...],
    ) -> None:
        if length <= 0 or lineno < 1:
            return
        tokens.append(
            SemanticToken(
                line=lineno - 1,
                character=col_offset,
                length=length,
                token_type=token_type,
                token_modifiers=modifiers,
            )
        )

    def walk(node: ast.AST, inside_class: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            located = _locate_def_name_offsets_on_header(lines, node)
            if located is not None:
                line_no, col, end_col = located
                modifiers: tuple[SemanticTokenModifier, ...] = ("declaration",)
                if isinstance(node, ast.AsyncFunctionDef):
                    modifiers = modifiers + ("async",)
                emit(
                    line_no,
                    col,
                    end_col - col,
                    "method" if inside_class else "function",
                    modifiers,
                )
            for arg in _iter_function_args(node.args):
                if arg.lineno is None or arg.col_offset is None:
                    continue
                emit(
                    arg.lineno,
                    arg.col_offset,
                    len(arg.arg),
                    "parameter",
                    ("declaration",),
                )
            for decorator in node.decorator_list:
                walk(decorator, inside_class=inside_class)
            for default_expr in node.args.defaults:
                walk(default_expr, inside_class=inside_class)
            for kw_default in node.args.kw_defaults:
                if kw_default is not None:
                    walk(kw_default, inside_class=inside_class)
            for arg in _iter_function_args(node.args):
                if arg.annotation is not None:
                    walk(arg.annotation, inside_class=inside_class)
            if node.returns is not None:
                walk(node.returns, inside_class=inside_class)
            for body_stmt in node.body:
                walk(body_stmt, inside_class=False)
            return
        if isinstance(node, ast.ClassDef):
            located = _locate_def_name_offsets_on_header(lines, node)
            if located is not None:
                line_no, col, end_col = located
                emit(line_no, col, end_col - col, "class", ("declaration",))
            for decorator in node.decorator_list:
                walk(decorator, inside_class=inside_class)
            for base in node.bases:
                walk(base, inside_class=inside_class)
            for keyword_arg in node.keywords:
                walk(keyword_arg.value, inside_class=inside_class)
            for class_body_stmt in node.body:
                walk(class_body_stmt, inside_class=True)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            token_type = name_to_token_type.get(node.id)
            if token_type is not None and node.lineno is not None:
                emit(node.lineno, node.col_offset, len(node.id), token_type, ())
            return
        for descendant in ast.iter_child_nodes(node):
            walk(descendant, inside_class=inside_class)

    walk(tree, inside_class=False)
    tokens.sort(key=lambda token: (token.line, token.character))
    return tuple(tokens)


def _collect_filesystem_snapshot(
    root: str, ignored_dir_names: frozenset[str]
) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignored_dir_names]
        for filename in filenames:
            file_path = Path(current_root, filename).resolve(strict=False)
            try:
                stat = file_path.stat()
            except FileNotFoundError:
                continue
            snapshot[str(file_path)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


class WorkspaceSession:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        mode: str = "strict",
        ignored_dir_names: tuple[str, ...] | None = None,
    ) -> None:
        root_path = Path(root).resolve(strict=False)
        if not root_path.exists() or not root_path.is_dir():
            raise ValueError(f"{root_path!s} is not an existing workspace directory.")

        self.root = str(root_path)
        self.db = Database(mode=mode)
        self._ignored_dir_names = frozenset(
            ignored_dir_names or DEFAULT_IGNORED_DIR_NAMES
        )
        self._tempdir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
            prefix="pyinc-tools-"
        )
        self.mirror_root = str(Path(self._tempdir.name, "workspace"))
        self._mirror_root_path = Path(self.mirror_root)
        self._mirror_root_path.mkdir(parents=True, exist_ok=True)
        self._overlays: dict[str, str] = {}
        self._scheduled_paths: set[str] = set()
        self._state_lock = threading.RLock()
        self._closed = False
        self._copy_workspace_into_mirror()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._tempdir.cleanup()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("WorkspaceSession is closed.")

    def __enter__(self) -> WorkspaceSession:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def set_overlay(self, path: str | os.PathLike[str], text: str) -> str:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            mirror_path.write_text(text, encoding="utf-8")
            self._overlays[real_path] = text
            self._scheduled_paths.add(real_path)
            return real_path

    def clear_overlay(self, path: str | os.PathLike[str]) -> str:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            self._overlays.pop(real_path, None)
            self._sync_path_from_disk(real_path)
            self._scheduled_paths.add(real_path)
            return real_path

    def refresh_paths(
        self,
        paths: Sequence[str | os.PathLike[str]],
    ) -> tuple[str, ...]:
        with self._state_lock:
            self._check_open()
            refreshed: list[str] = []
            seen: set[str] = set()
            for raw_path in paths:
                real_path = self._normalize_real_path(raw_path)
                if real_path in seen:
                    continue
                seen.add(real_path)
                if real_path not in self._overlays:
                    self._sync_path_from_disk(real_path)
                self._scheduled_paths.add(real_path)
                refreshed.append(real_path)
            return tuple(refreshed)

    def analyze_file(self, path: str | os.PathLike[str]) -> FileAnalysisResult:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            dependency_inputs = self._dependency_inputs()
            dependency_check = workspace_dependency_check(
                self.db,
                self.mirror_root,
                dependency_inputs.declared_dependencies,
            )
            result = self._build_file_result(
                real_path, dependency_inputs, dependency_check
            )
            self._scheduled_paths.discard(real_path)
            return result

    def analyze_workspace(self) -> WorkspaceAnalysisResult:
        with self._state_lock:
            self._check_open()
            dependency_inputs = self._dependency_inputs()
            dependency_check = workspace_dependency_check(
                self.db,
                self.mirror_root,
                dependency_inputs.declared_dependencies,
            )
            python_analysis = self._remap_workspace_analysis(
                workspace_analysis(self.db, self.mirror_root)
            )
            symbol_index = self._remap_workspace_symbol_index(
                workspace_symbol_index(self.db, self.mirror_root)
            )
            files = tuple(
                self._build_file_result(
                    module.path,
                    dependency_inputs,
                    dependency_check,
                    module=module,
                )
                for module in python_analysis.modules
            )
            diagnostics = self._dedupe_diagnostics(
                tuple(
                    diagnostic
                    for file_result in files
                    for diagnostic in file_result.diagnostics
                )
                + self._dependency_status_diagnostics(
                    dependency_inputs, dependency_check
                )
            )
            self._scheduled_paths.clear()
            return WorkspaceAnalysisResult(
                root=self.root,
                python=python_analysis,
                symbols=symbol_index,
                dependency_check=dependency_check,
                files=files,
                diagnostics=diagnostics,
            )

    def resolve_symbol_reference(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> ResolvedSymbol:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            resolved = resolve_symbol(
                self.db, self.mirror_root, str(mirror_path), qualified_name
            )
            return self._remap_resolved_symbol(resolved)

    def find_references(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
        *,
        include_declaration: bool = True,
    ) -> ReferenceQueryResult:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            result = find_references(
                self.db,
                self.mirror_root,
                str(mirror_path),
                qualified_name,
                include_declaration=include_declaration,
            )
            remapped_target = self._remap_resolved_symbol(result.target)
            remapped_refs = tuple(
                Reference(
                    path=self._remap_path(ref.path) or ref.path,
                    lineno=ref.lineno,
                    col_offset=ref.col_offset,
                    end_col_offset=ref.end_col_offset,
                    is_declaration=ref.is_declaration,
                )
                for ref in result.references
            )
            return ReferenceQueryResult(
                target=remapped_target, references=remapped_refs
            )

    def find_document_highlights(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> tuple[DocumentHighlight, ...]:
        bare_name = qualified_name.rsplit(".", 1)[-1]
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            result = find_references(
                self.db,
                self.mirror_root,
                str(mirror_path),
                qualified_name,
                include_declaration=True,
            )
            if result.target.resolution != "workspace":
                return ()
            highlights: list[DocumentHighlight] = []
            seen: set[tuple[int, int, int]] = set()
            for ref in result.references:
                ref_real_path = self._remap_path(ref.path) or ref.path
                if ref_real_path != real_path:
                    continue
                col, end_col = ref.col_offset, ref.end_col_offset
                if ref.is_declaration and col == 0 and end_col == 1:
                    located = self._locate_def_class_name_offsets(
                        ref_real_path, ref.lineno, bare_name
                    )
                    if located is None:
                        continue
                    col, end_col = located
                key = (ref.lineno, col, end_col)
                if key in seen:
                    continue
                seen.add(key)
                kind: DocumentHighlightKind = "write" if ref.is_declaration else "text"
                highlights.append(
                    DocumentHighlight(
                        lineno=ref.lineno,
                        col_offset=col,
                        end_col_offset=end_col,
                        kind=kind,
                    )
                )
            highlights.sort(key=lambda h: (h.lineno, h.col_offset))
            return tuple(highlights)

    def linked_editing_ranges_at(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> tuple[LinkedEditingRange, ...]:
        highlights = self.find_document_highlights(path, qualified_name)
        return tuple(
            LinkedEditingRange(
                lineno=highlight.lineno,
                col_offset=highlight.col_offset,
                end_col_offset=highlight.end_col_offset,
            )
            for highlight in highlights
        )

    def signature_help_at(
        self,
        path: str | os.PathLike[str],
        line: int,
        character: int,
    ) -> SignatureHelp | None:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return None
            located = _find_call_at_position(source, line, character)
            if located is None:
                return None
            function_name, active_index = located
            resolved = self._remap_resolved_symbol(
                resolve_symbol(
                    self.db, self.mirror_root, str(mirror_path), function_name
                )
            )
            if resolved.resolution != "workspace":
                return None
            callable_info = self._lookup_callable_signature(resolved)
            if callable_info is None:
                return None
            display_name, signature = callable_info
            label, parameters = _build_signature_label(display_name, signature)
            active_parameter = (
                active_index if 0 <= active_index < len(parameters) else None
            )
            return SignatureHelp(
                label=label,
                parameters=parameters,
                active_parameter=active_parameter,
            )

    def folding_ranges_for_file(
        self,
        path: str | os.PathLike[str],
    ) -> tuple[FoldingRange, ...]:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            return _compute_folding_ranges(source)

    def selection_ranges_at(
        self,
        path: str | os.PathLike[str],
        line: int,
        character: int,
    ) -> tuple[SelectionRange, ...]:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            return _compute_selection_chain(source, line, character)

    def document_links_for_file(
        self,
        path: str | os.PathLike[str],
    ) -> tuple[DocumentLink, ...]:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            module = self._remap_module_analysis(
                module_analysis(self.db, self.mirror_root, str(mirror_path))
            )
            return _compute_document_links(source, module.resolved_imports)

    def code_lenses_for_file(
        self,
        path: str | os.PathLike[str],
    ) -> tuple[CodeLens, ...]:
        """Return one reference-count `CodeLens` per top-level `def`/`class`.

        Each lens spans the definition's bare-name identifier range on its
        header line and carries a `title` of `"N reference"` /
        `"N references"`, counting workspace references reported by
        `find_references` with `include_declaration=False`. Methods, class
        variables, import aliases, and other non-top-level symbols emit no
        lens — references on those are not reliably resolvable through the
        symbol resolver. Files that fail to parse or have no symbols emit
        an empty tuple.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            table = self._remap_module_symbol_table(
                module_symbol_table(self.db, self.mirror_root, str(mirror_path))
            )
            lenses: list[CodeLens] = []
            for symbol in table.symbols:
                if symbol.kind not in ("function", "class"):
                    continue
                if "." in symbol.qualified_name:
                    continue
                located = self._locate_def_class_name_offsets(
                    real_path, symbol.lineno, symbol.qualified_name
                )
                if located is None:
                    continue
                start_col, end_col = located
                result = find_references(
                    self.db,
                    self.mirror_root,
                    str(mirror_path),
                    symbol.qualified_name,
                    include_declaration=False,
                )
                if result.target.resolution != "workspace":
                    continue
                count = len(result.references)
                title = f"{count} reference" if count == 1 else f"{count} references"
                line_zero = max(symbol.lineno - 1, 0)
                lenses.append(
                    CodeLens(
                        start_line=line_zero,
                        start_character=start_col,
                        end_line=line_zero,
                        end_character=end_col,
                        title=title,
                    )
                )
            lenses.sort(key=lambda lens: (lens.start_line, lens.start_character))
            return tuple(lenses)

    def inlay_hints_for_file(
        self,
        path: str | os.PathLike[str],
        start_line: int = 0,
        start_character: int = 0,
        end_line: int | None = None,
        end_character: int = 0,
    ) -> tuple[InlayHint, ...]:
        """Return parameter-name `InlayHint`s for call sites in ``path``.

        Walks the AST for ``ast.Call`` nodes whose call-function span starts
        inside the half-open LSP range ``[(start_line, start_character),
        (end_line, end_character))`` (omit ``end_line`` to scan the whole
        file). For each call whose callee resolves to a workspace function
        or class, positional arguments are matched against the callee's
        positional parameters from `Signature.parameters` and a single
        ``"name:"`` hint is emitted at each argument's start position with
        ``kind="parameter"`` and ``padding_right=True``.

        A hint is suppressed when the argument is a bare ``Name`` whose
        identifier already equals the parameter name (the standard
        no-redundant-hint convention used by other Python language
        servers). Iteration stops at the first ``*args`` parameter — once a
        positional parameter consumes a variable number of slots, slot
        alignment for subsequent arguments is ambiguous. The first
        ``ast.Starred`` argument similarly stops emission. ``**kwargs``-only
        parameters are silently skipped since they cannot receive a
        positional argument.

        Targets resolved as stdlib / installed / ambiguous / missing, calls
        whose callee shape is not a bare ``Name`` or ``Name.attr`` (e.g.
        ``factory[T](...)``, ``pkg.subpkg.foo(...)``, ``self.method(...)``,
        ``lambda x: x(...)``), and files that fail to parse return ``()``.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                return ()

            calls: list[ast.Call] = []

            def walk(node: ast.AST) -> None:
                if isinstance(node, ast.Call):
                    calls.append(node)
                for child in ast.iter_child_nodes(node):
                    walk(child)

            walk(tree)

            range_end_line = end_line
            hints: list[InlayHint] = []
            for call in calls:
                func_range = _call_func_range(call)
                if func_range is None:
                    continue
                func_start_line, func_start_col, _, _ = func_range
                if func_start_line < start_line or (
                    func_start_line == start_line and func_start_col < start_character
                ):
                    continue
                if range_end_line is not None and (
                    func_start_line > range_end_line
                    or (
                        func_start_line == range_end_line
                        and func_start_col >= end_character
                    )
                ):
                    continue
                target = self._resolve_call_target(mirror_path, call.func)
                if target is None or target.resolution != "workspace":
                    continue
                callable_info = self._lookup_callable_signature(target)
                if callable_info is None:
                    continue
                _display, signature = callable_info
                hints.extend(
                    _inlay_hints_for_call(call, signature.parameters)
                )
            hints.sort(key=lambda hint: (hint.line, hint.character))
            return tuple(hints)

    def semantic_tokens_for_file(
        self,
        path: str | os.PathLike[str],
    ) -> tuple[SemanticToken, ...]:
        """Return semantic-token classifications for ``path``.

        Walks the document's AST once and emits one ``SemanticToken`` per:

        - ``def`` / ``async def`` header — token type ``"function"`` (or
          ``"method"`` when nested inside a ``ClassDef`` body), modifier
          ``"declaration"`` (plus ``"async"`` for ``async def``).
        - ``class`` header — token type ``"class"``, modifier
          ``"declaration"``.
        - Each function parameter (posonly / positional / vararg / kwonly /
          kwarg) — token type ``"parameter"``, modifier ``"declaration"``.
        - Each bare ``ast.Name`` use (Load context) whose identifier matches
          a top-level entry in the file's ``ModuleSymbolTable``. The token
          type follows the matched symbol's kind: ``function``,
          ``method`` (already qualified entries are skipped),
          ``class``, ``variable`` / ``class_variable`` → ``"variable"``, and
          ``import_alias`` → ``"namespace"``. ``from_import_alias`` and
          ``wildcard_import_stub`` entries are skipped (resolving them to
          their real kind would require cross-module hops; the editor's
          default highlighting handles them).

        Tokens are sorted by ``(line, character)`` with ``line`` /
        ``character`` 0-based (LSP-style). Files that fail to parse,
        non-``.py`` paths, and missing files raise ``FileNotFoundError``
        for the missing-file case and return ``()`` for the unparseable
        case.

        Function-local shadowing is not modeled (a local ``foo`` inside a
        function that shadows a top-level ``foo`` is still reported as the
        top-level kind), mirroring the documented ``find_references``
        limitation.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            table = self._remap_module_symbol_table(
                module_symbol_table(self.db, self.mirror_root, str(mirror_path))
            )
            return _compute_semantic_tokens(source, table)

    def semantic_tokens_range_for_file(
        self,
        path: str | os.PathLike[str],
        start_line: int = 0,
        start_character: int = 0,
        end_line: int | None = None,
        end_character: int = 0,
    ) -> tuple[SemanticToken, ...]:
        """Return semantic-token classifications for ``path`` filtered to the
        half-open LSP range ``[(start_line, start_character),
        (end_line, end_character))``.

        Computes the full document's tokens via the same walk as
        :meth:`semantic_tokens_for_file` and then filters by token start
        position. A token at ``(line, character)`` is included when its start
        position is ``>= (start_line, start_character)`` and (if ``end_line``
        is provided) strictly less than ``(end_line, end_character)``. Omit
        ``end_line`` to scan from the start position through end-of-file.

        Coordinate convention matches the LSP wire format (0-based
        ``line`` / ``character``). Missing files and non-``.py`` paths raise
        ``FileNotFoundError``; unparseable files return ``()``.
        """
        all_tokens = self.semantic_tokens_for_file(path)
        if not all_tokens:
            return all_tokens
        if start_line == 0 and start_character == 0 and end_line is None:
            return all_tokens
        filtered: list[SemanticToken] = []
        for token in all_tokens:
            if token.line < start_line or (
                token.line == start_line and token.character < start_character
            ):
                continue
            if end_line is not None and (
                token.line > end_line
                or (token.line == end_line and token.character >= end_character)
            ):
                continue
            filtered.append(token)
        return tuple(filtered)

    def type_definitions_at(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> tuple[TypeDefinitionLocation, ...]:
        """Resolve the type-definition locations of the symbol named `qualified_name`.

        Resolves `qualified_name` against `path`'s imports to find the symbol's
        declaration, reads the declared annotation (variable / class-variable
        annotation, or function / method return annotation), parses it as a
        Python expression, and resolves the contained type names against the
        declaration's defining module. Returns one
        `TypeDefinitionLocation(path, lineno, col_offset, end_col_offset)` per
        workspace-resolved type, deduplicated by `(path, lineno)`.

        Classes are themselves the type — clicking on a class name returns its
        own definition location. Import aliases, `from_import` aliases,
        wildcard-import stubs, parameters, and non-workspace targets return an
        empty tuple. Whole-string forward references (`x: "Foo"`,
        `def f() -> "Foo"`) are unwrapped and re-parsed once; partial string
        annotations (`x: "Foo" | None`) and stdlib / installed / ambiguous type
        names are skipped.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            resolved = self._remap_resolved_symbol(
                resolve_symbol(
                    self.db, self.mirror_root, str(mirror_path), qualified_name
                )
            )
            if resolved.resolution != "workspace":
                return ()
            if resolved.defining_path is None or resolved.defining_lineno is None:
                return ()
            defining_mirror = self._mirror_path_for_real(resolved.defining_path)
            if not defining_mirror.exists() or defining_mirror.suffix != ".py":
                return ()
            defining_table = module_symbol_table(
                self.db, self.mirror_root, str(defining_mirror)
            )
            matched: Symbol | None = None
            for symbol in defining_table.symbols:
                if (
                    symbol.lineno == resolved.defining_lineno
                    and "." not in symbol.qualified_name
                ):
                    matched = symbol
                    break
            if matched is None:
                return ()
            if matched.kind == "class":
                return (
                    TypeDefinitionLocation(
                        path=resolved.defining_path,
                        lineno=resolved.defining_lineno,
                        col_offset=0,
                        end_col_offset=1,
                    ),
                )
            if matched.kind in ("function", "method"):
                annotation = (
                    matched.signature.return_annotation
                    if matched.signature is not None
                    else None
                )
            elif matched.kind in ("variable", "class_variable"):
                annotation = matched.annotation
            else:
                return ()
            if annotation is None:
                return ()
            type_refs = _collect_annotation_type_refs(annotation)
            locations: list[TypeDefinitionLocation] = []
            seen: set[tuple[str, int]] = set()
            for ref in type_refs:
                type_resolved = self._resolve_annotation_type_ref(
                    defining_mirror, ref
                )
                if type_resolved is None:
                    continue
                if (
                    type_resolved.resolution != "workspace"
                    or type_resolved.defining_path is None
                    or type_resolved.defining_lineno is None
                ):
                    continue
                key = (type_resolved.defining_path, type_resolved.defining_lineno)
                if key in seen:
                    continue
                seen.add(key)
                locations.append(
                    TypeDefinitionLocation(
                        path=type_resolved.defining_path,
                        lineno=type_resolved.defining_lineno,
                        col_offset=0,
                        end_col_offset=1,
                    )
                )
            return tuple(locations)

    def declaration_location_at(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> DeclarationLocation | None:
        """Return the in-file declaration location of ``qualified_name``.

        Looks up ``qualified_name`` in ``path``'s
        :class:`ModuleSymbolTable` (an exact ``qualified_name`` match wins
        over a bare-name match against the last dotted component) and
        returns a :class:`DeclarationLocation` pointing at the binding
        statement in ``path``. The location's ``col_offset`` /
        ``end_col_offset`` span the bare-name identifier on the binding's
        header line; if the identifier cannot be located on that line, the
        offsets fall back to ``(0, 1)``.

        Distinct from ``definition``: for an ``import_alias`` /
        ``from_import_alias`` / ``wildcard_import_stub`` the declaration is
        the ``import`` statement in the current file, while ``definition``
        follows the import chain through to the resolved target's file.
        For workspace functions, classes, and module-level variables the
        two coincide.

        Returns ``None`` when ``qualified_name`` is not found in ``path``'s
        symbol table, when the file has not been mirrored, or when the
        path is not a Python file.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            table = module_symbol_table(
                self.db, self.mirror_root, str(mirror_path)
            )
            matched: Symbol | None = None
            for symbol in table.symbols:
                if symbol.qualified_name == qualified_name:
                    matched = symbol
                    break
            if matched is None:
                bare = qualified_name.rsplit(".", 1)[-1]
                for symbol in table.symbols:
                    if symbol.qualified_name.rsplit(".", 1)[-1] == bare:
                        matched = symbol
                        break
            if matched is None:
                return None
            col_offset, end_col_offset = self._locate_symbol_name_offsets(
                real_path, matched
            )
            return DeclarationLocation(
                path=real_path,
                lineno=matched.lineno,
                col_offset=col_offset,
                end_col_offset=end_col_offset,
            )

    def prepare_call_hierarchy(
        self,
        path: str | os.PathLike[str],
        line: int,
        character: int,
    ) -> tuple[CallHierarchyItem, ...]:
        """Return the call-hierarchy item(s) for the identifier at the cursor.

        Resolves the identifier under ``(line, character)`` (LSP-style 0-based
        coordinates) through ``resolve_symbol``. If the resolved target is a
        workspace function, method, or class, a single
        :class:`CallHierarchyItem` describing that target is returned;
        otherwise the result is empty. Variables, import aliases,
        ``from_import`` aliases, wildcard-import stubs, and stdlib /
        installed / ambiguous / missing targets all return ``()``.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            identifier = _identifier_at_source_position(source, line, character)
            if identifier is None:
                return ()
            resolved = self._remap_resolved_symbol(
                resolve_symbol(
                    self.db, self.mirror_root, str(mirror_path), identifier
                )
            )
            if resolved.resolution != "workspace":
                return ()
            if resolved.defining_path is None or resolved.defining_lineno is None:
                return ()
            defining_mirror = self._mirror_path_for_real(resolved.defining_path)
            if not defining_mirror.exists() or defining_mirror.suffix != ".py":
                return ()
            defining_table = module_symbol_table(
                self.db, self.mirror_root, str(defining_mirror)
            )
            matched: Symbol | None = None
            for symbol in defining_table.symbols:
                if (
                    symbol.lineno == resolved.defining_lineno
                    and symbol.qualified_name == resolved.qualified_name
                    and symbol.kind in ("function", "method", "class")
                ):
                    matched = symbol
                    break
            if matched is None:
                return ()
            item = self._build_call_hierarchy_item(
                resolved.defining_path, matched.qualified_name, defining_table.module
            )
            if item is None:
                return ()
            return (item,)

    def call_hierarchy_incoming_calls(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> tuple[CallHierarchyIncomingCall, ...]:
        """Return callers of the symbol named ``qualified_name`` (declared in ``path``).

        ``find_references(include_declaration=False)`` produces every workspace
        reference; each reference is attributed to its innermost enclosing
        ``def`` / ``async def`` / ``class`` in the same file whose qualified
        name appears in that file's symbol table. References inside nested
        function bodies bubble up to their enclosing top-level function or
        class method (mirroring ``module_symbol_table``'s qualifier scheme);
        references at module top level are dropped because there is no caller
        item to attribute them to. Stdlib / installed / ambiguous / missing
        targets, and references that don't sit inside any known def/class in
        the workspace, return ``()``.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            result = find_references(
                self.db,
                self.mirror_root,
                str(mirror_path),
                qualified_name,
                include_declaration=False,
            )
            if result.target.resolution != "workspace":
                return ()

            grouped: dict[
                tuple[str, str], list[CallHierarchyCallSite]
            ] = {}
            order: list[tuple[str, str]] = []
            tree_cache: dict[str, ast.Module | None] = {}
            table_cache: dict[str, ModuleSymbolTable] = {}

            for ref in result.references:
                ref_real_path = self._remap_path(ref.path) or ref.path
                ref_mirror_path = self._mirror_path_for_real(ref_real_path)
                if not ref_mirror_path.exists() or ref_mirror_path.suffix != ".py":
                    continue
                if ref_real_path not in tree_cache:
                    source = self.source_text(ref_real_path)
                    if source is None:
                        tree_cache[ref_real_path] = None
                    else:
                        try:
                            tree_cache[ref_real_path] = ast.parse(source)
                        except SyntaxError:
                            tree_cache[ref_real_path] = None
                tree = tree_cache[ref_real_path]
                if tree is None:
                    continue
                if ref_real_path not in table_cache:
                    table_cache[ref_real_path] = self._remap_module_symbol_table(
                        module_symbol_table(
                            self.db, self.mirror_root, str(ref_mirror_path)
                        )
                    )
                table = table_cache[ref_real_path]
                known_qnames = frozenset(
                    symbol.qualified_name
                    for symbol in table.symbols
                    if symbol.kind in ("function", "method", "class")
                )
                caller_qname = _enclosing_callable_qname(
                    tree, known_qnames, ref.lineno
                )
                if caller_qname is None:
                    continue
                key = (ref_real_path, caller_qname)
                if key not in grouped:
                    grouped[key] = []
                    order.append(key)
                grouped[key].append(
                    CallHierarchyCallSite(
                        start_line=max(ref.lineno - 1, 0),
                        start_character=ref.col_offset,
                        end_line=max(ref.lineno - 1, 0),
                        end_character=ref.end_col_offset,
                    )
                )

            incoming: list[CallHierarchyIncomingCall] = []
            for caller_path, caller_qname in order:
                caller_item = self._build_call_hierarchy_item(
                    caller_path, caller_qname, module_name=None
                )
                if caller_item is None:
                    continue
                sites = sorted(
                    grouped[(caller_path, caller_qname)],
                    key=lambda site: (site.start_line, site.start_character),
                )
                incoming.append(
                    CallHierarchyIncomingCall(
                        caller=caller_item, call_sites=tuple(sites)
                    )
                )
            incoming.sort(key=lambda call: (call.caller.path, call.caller.qualified_name))
            return tuple(incoming)

    def call_hierarchy_outgoing_calls(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> tuple[CallHierarchyOutgoingCall, ...]:
        """Return callees called from the body of ``qualified_name`` (declared in ``path``).

        Parses the declaring file's AST once, locates the ``FunctionDef`` /
        ``AsyncFunctionDef`` / ``ClassDef`` matching ``qualified_name``, and
        walks its body for ``ast.Call`` nodes — without descending into
        nested ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef`` /
        ``Lambda`` scopes, each of which owns its own outgoing-call list.
        Calls whose ``func`` is a bare ``Name`` are resolved against the
        declaring module's imports; calls whose ``func`` is
        ``Name.attr`` are resolved by first resolving the LHS name to a
        workspace module and then resolving ``attr`` inside that module
        (mirroring ``find_references``'s LHS-bare-Name handling).
        Subscripted calls (``factory[T](``), deep attribute chains
        (``pkg.subpkg.foo()``), and lambda calls produce no callee. Targets
        that don't resolve to a workspace function/method/class are skipped.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                return ()
            body_node = _find_callable_node(tree, qualified_name)
            if body_node is None:
                return ()

            grouped: dict[
                tuple[str, str], list[CallHierarchyCallSite]
            ] = {}
            order: list[tuple[str, str]] = []
            for call in _collect_outgoing_calls(body_node):
                func_range = _call_func_range(call)
                if func_range is None:
                    continue
                target_resolved = self._resolve_call_target(
                    mirror_path, call.func
                )
                if target_resolved is None:
                    continue
                if (
                    target_resolved.resolution != "workspace"
                    or target_resolved.defining_path is None
                    or target_resolved.defining_lineno is None
                ):
                    continue
                defining_mirror = self._mirror_path_for_real(
                    target_resolved.defining_path
                )
                if (
                    not defining_mirror.exists()
                    or defining_mirror.suffix != ".py"
                ):
                    continue
                defining_table = module_symbol_table(
                    self.db, self.mirror_root, str(defining_mirror)
                )
                matched: Symbol | None = None
                for symbol in defining_table.symbols:
                    if (
                        symbol.lineno == target_resolved.defining_lineno
                        and symbol.qualified_name == target_resolved.qualified_name
                        and symbol.kind in ("function", "method", "class")
                    ):
                        matched = symbol
                        break
                if matched is None:
                    continue
                key = (target_resolved.defining_path, matched.qualified_name)
                if key not in grouped:
                    grouped[key] = []
                    order.append(key)
                sl, sc, el, ec = func_range
                grouped[key].append(
                    CallHierarchyCallSite(
                        start_line=sl,
                        start_character=sc,
                        end_line=el,
                        end_character=ec,
                    )
                )

            outgoing: list[CallHierarchyOutgoingCall] = []
            for callee_path, callee_qname in order:
                callee_item = self._build_call_hierarchy_item(
                    callee_path, callee_qname, module_name=None
                )
                if callee_item is None:
                    continue
                sites = sorted(
                    grouped[(callee_path, callee_qname)],
                    key=lambda site: (site.start_line, site.start_character),
                )
                outgoing.append(
                    CallHierarchyOutgoingCall(
                        callee=callee_item, call_sites=tuple(sites)
                    )
                )
            outgoing.sort(key=lambda call: (call.callee.path, call.callee.qualified_name))
            return tuple(outgoing)

    def prepare_type_hierarchy(
        self,
        path: str | os.PathLike[str],
        line: int,
        character: int,
    ) -> tuple[TypeHierarchyItem, ...]:
        """Return the type-hierarchy item for the identifier at the cursor.

        Resolves the identifier under ``(line, character)`` (LSP-style 0-based
        coordinates) through :func:`resolve_symbol`. If the resolved target is
        a workspace class (including a class re-exported through an
        ``import`` / ``from … import …`` chain), a single
        :class:`TypeHierarchyItem` describing the declaring ``ClassDef`` is
        returned; otherwise the result is empty. Functions, methods,
        variables, import aliases, ``from_import`` aliases, wildcard-import
        stubs, and stdlib / installed / ambiguous / missing targets all
        return ``()``.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            identifier = _identifier_at_source_position(source, line, character)
            if identifier is None:
                return ()
            resolved = self._remap_resolved_symbol(
                resolve_symbol(
                    self.db, self.mirror_root, str(mirror_path), identifier
                )
            )
            if resolved.resolution != "workspace":
                return ()
            if resolved.defining_path is None or resolved.defining_lineno is None:
                return ()
            defining_mirror = self._mirror_path_for_real(resolved.defining_path)
            if not defining_mirror.exists() or defining_mirror.suffix != ".py":
                return ()
            defining_table = module_symbol_table(
                self.db, self.mirror_root, str(defining_mirror)
            )
            matched: Symbol | None = None
            for symbol in defining_table.symbols:
                if (
                    symbol.lineno == resolved.defining_lineno
                    and symbol.qualified_name == resolved.qualified_name
                    and symbol.kind == "class"
                ):
                    matched = symbol
                    break
            if matched is None:
                return ()
            item = self._build_type_hierarchy_item(
                resolved.defining_path, matched.qualified_name, defining_table.module
            )
            if item is None:
                return ()
            return (item,)

    def type_hierarchy_supertypes(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> tuple[TypeHierarchyItem, ...]:
        """Return the immediate base classes of ``qualified_name`` (declared in ``path``).

        Parses the declaring file's AST once, locates the ``ClassDef``
        matching ``qualified_name`` (using the same dotted-name walker as
        ``call_hierarchy_outgoing_calls``), and resolves each entry in its
        ``bases`` list. ``Subscript`` bases (``Generic[T]``, ``Base[T]``) are
        unwrapped to their ``value`` before resolution, so generic base
        classes are still navigated. Bare ``Name(id=X)`` bases resolve
        ``X`` against the declaring module's imports; ``Name.attr`` bases
        resolve the LHS to a workspace module and then ``attr`` inside it
        (mirroring ``call_hierarchy_outgoing_calls``'s LHS-bare-Name rule).
        ``Starred`` bases and deeper attribute chains (``pkg.sub.Foo``)
        produce no entry. Only workspace ``class`` targets contribute an
        item; stdlib / installed / ambiguous / missing bases are dropped.
        Duplicates (same ``(path, qualified_name)``) are collapsed, and the
        result is sorted by ``(path, qualified_name)``.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                return ()
            class_node = _find_callable_node(tree, qualified_name)
            if class_node is None or not isinstance(class_node, ast.ClassDef):
                return ()

            seen: set[tuple[str, str]] = set()
            items: list[TypeHierarchyItem] = []
            for base in class_node.bases:
                target = _unwrap_base_expression(base)
                if target is None:
                    continue
                resolved = self._resolve_class_target(mirror_path, target)
                if resolved is None:
                    continue
                if (
                    resolved.resolution != "workspace"
                    or resolved.defining_path is None
                    or resolved.defining_lineno is None
                ):
                    continue
                key = (resolved.defining_path, resolved.qualified_name)
                if key in seen:
                    continue
                seen.add(key)
                defining_mirror = self._mirror_path_for_real(resolved.defining_path)
                if (
                    not defining_mirror.exists()
                    or defining_mirror.suffix != ".py"
                ):
                    continue
                defining_table = module_symbol_table(
                    self.db, self.mirror_root, str(defining_mirror)
                )
                base_symbol: Symbol | None = None
                for symbol in defining_table.symbols:
                    if (
                        symbol.lineno == resolved.defining_lineno
                        and symbol.qualified_name == resolved.qualified_name
                        and symbol.kind == "class"
                    ):
                        base_symbol = symbol
                        break
                if base_symbol is None:
                    continue
                item = self._build_type_hierarchy_item(
                    resolved.defining_path,
                    base_symbol.qualified_name,
                    defining_table.module,
                )
                if item is not None:
                    items.append(item)
            items.sort(key=lambda hi: (hi.path, hi.qualified_name))
            return tuple(items)

    def type_hierarchy_subtypes(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> tuple[TypeHierarchyItem, ...]:
        """Return the immediate workspace subtypes of ``qualified_name``.

        Walks the workspace once via :func:`workspace_analysis` and visits
        every ``ClassDef`` in every Python file, recursing into class
        bodies so nested classes are eligible subtypes (qualified-name
        nesting follows ``module_symbol_table``: ``Outer.Inner`` for a
        class nested inside another class). For each candidate's
        ``bases`` list, each base expression is unwrapped (``Subscript``
        bases drop their subscript) and resolved through the candidate
        file's imports; a candidate is a subtype iff at least one of its
        resolved bases points at ``(path, qualified_name)``. Resolution
        of bases follows the same rules as :meth:`type_hierarchy_supertypes`:
        bare ``Name`` resolves in the candidate's module, ``Name.attr``
        resolves the LHS to a workspace module then ``attr`` inside it,
        deeper attribute chains are skipped. Duplicates by
        ``(path, qualified_name)`` are collapsed, and the result is
        sorted by ``(path, qualified_name)``.

        Only direct subtypes are returned; LSP clients drill down by
        calling ``typeHierarchy/subtypes`` recursively on each result.
        Returns ``()`` when the target itself is not a workspace class.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            source = self.source_text(real_path)
            if source is None:
                return ()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                return ()
            target_node = _find_callable_node(tree, qualified_name)
            if target_node is None or not isinstance(target_node, ast.ClassDef):
                return ()

            workspace = self._remap_workspace_analysis(
                workspace_analysis(self.db, self.mirror_root)
            )
            target_key = (real_path, qualified_name)
            seen: set[tuple[str, str]] = set()
            items: list[TypeHierarchyItem] = []

            for module in workspace.modules:
                candidate_real_path = module.path
                candidate_mirror = self._mirror_path_for_real(candidate_real_path)
                if (
                    not candidate_mirror.exists()
                    or candidate_mirror.suffix != ".py"
                ):
                    continue
                candidate_source = self.source_text(candidate_real_path)
                if candidate_source is None:
                    continue
                try:
                    candidate_tree = ast.parse(candidate_source)
                except SyntaxError:
                    continue
                candidate_table: ModuleSymbolTable | None = None
                for class_qname, class_node in _walk_class_definitions(candidate_tree):
                    candidate_key = (candidate_real_path, class_qname)
                    if candidate_key == target_key or candidate_key in seen:
                        continue
                    is_subtype = False
                    for base in class_node.bases:
                        base_target = _unwrap_base_expression(base)
                        if base_target is None:
                            continue
                        base_resolved = self._resolve_class_target(
                            candidate_mirror, base_target
                        )
                        if base_resolved is None:
                            continue
                        if (
                            base_resolved.resolution == "workspace"
                            and base_resolved.defining_path == real_path
                            and base_resolved.qualified_name == qualified_name
                        ):
                            is_subtype = True
                            break
                    if not is_subtype:
                        continue
                    if candidate_table is None:
                        candidate_table = self._remap_module_symbol_table(
                            module_symbol_table(
                                self.db,
                                self.mirror_root,
                                str(candidate_mirror),
                            )
                        )
                    item = self._build_type_hierarchy_item(
                        candidate_real_path,
                        class_qname,
                        candidate_table.module,
                    )
                    if item is None:
                        continue
                    seen.add(candidate_key)
                    items.append(item)

            items.sort(key=lambda hi: (hi.path, hi.qualified_name))
            return tuple(items)

    def _resolve_class_target(
        self,
        caller_mirror_path: Path,
        target: tuple[str, ...],
    ) -> ResolvedSymbol | None:
        """Resolve a ``("name", X)`` or ``("attr", L, A)`` ref to a class.

        ``("name", X)`` looks ``X`` up in ``caller_mirror_path``'s module
        imports. ``("attr", L, A)`` resolves ``L`` to a workspace module
        and then ``A`` inside that module. The resolved symbol is mapped
        back from the mirror to the real workspace before being returned.
        Mirrors :meth:`_resolve_call_target`'s shape — kept separate so
        the two resolvers can diverge without coupling.
        """
        if target[0] == "name":
            return self._remap_resolved_symbol(
                resolve_symbol(
                    self.db,
                    self.mirror_root,
                    str(caller_mirror_path),
                    target[1],
                )
            )
        # ("attr", lhs_name, attr_name)
        lhs_resolved = self._remap_resolved_symbol(
            resolve_symbol(
                self.db, self.mirror_root, str(caller_mirror_path), target[1]
            )
        )
        if (
            lhs_resolved.resolution != "workspace"
            or lhs_resolved.defining_path is None
        ):
            return None
        lhs_mirror = self._mirror_path_for_real(lhs_resolved.defining_path)
        if not lhs_mirror.exists() or lhs_mirror.suffix != ".py":
            return None
        return self._remap_resolved_symbol(
            resolve_symbol(self.db, self.mirror_root, str(lhs_mirror), target[2])
        )

    def _build_type_hierarchy_item(
        self,
        real_path: str,
        qualified_name: str,
        module_name: str | None,
    ) -> TypeHierarchyItem | None:
        source = self.source_text(real_path)
        if source is None:
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        node = _find_callable_node(tree, qualified_name)
        if node is None or not isinstance(node, ast.ClassDef):
            return None
        if node.decorator_list:
            range_start_line = (
                min(dec.lineno for dec in node.decorator_list) - 1
            )
            range_start_col = min(
                dec.col_offset for dec in node.decorator_list
            )
        else:
            range_start_line = node.lineno - 1
            range_start_col = node.col_offset
        range_end_line = (node.end_lineno or node.lineno) - 1
        range_end_col = node.end_col_offset or 0

        bare_name = qualified_name.rsplit(".", 1)[-1]
        located = self._locate_def_class_name_offsets(
            real_path, node.lineno, bare_name
        )
        if located is None:
            return None
        selection_start_col, selection_end_col = located
        selection_line = node.lineno - 1

        return TypeHierarchyItem(
            name=bare_name,
            kind="class",
            path=real_path,
            qualified_name=qualified_name,
            detail=module_name,
            range_start_line=range_start_line,
            range_start_character=range_start_col,
            range_end_line=range_end_line,
            range_end_character=range_end_col,
            selection_start_line=selection_line,
            selection_start_character=selection_start_col,
            selection_end_line=selection_line,
            selection_end_character=selection_end_col,
        )

    def _resolve_call_target(
        self,
        caller_mirror_path: Path,
        func: ast.expr,
    ) -> ResolvedSymbol | None:
        if isinstance(func, ast.Name):
            return self._remap_resolved_symbol(
                resolve_symbol(
                    self.db,
                    self.mirror_root,
                    str(caller_mirror_path),
                    func.id,
                )
            )
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            lhs_resolved = self._remap_resolved_symbol(
                resolve_symbol(
                    self.db,
                    self.mirror_root,
                    str(caller_mirror_path),
                    func.value.id,
                )
            )
            if (
                lhs_resolved.resolution != "workspace"
                or lhs_resolved.defining_path is None
            ):
                return None
            lhs_mirror = self._mirror_path_for_real(lhs_resolved.defining_path)
            if not lhs_mirror.exists() or lhs_mirror.suffix != ".py":
                return None
            return self._remap_resolved_symbol(
                resolve_symbol(
                    self.db, self.mirror_root, str(lhs_mirror), func.attr
                )
            )
        return None

    def _build_call_hierarchy_item(
        self,
        real_path: str,
        qualified_name: str,
        module_name: str | None,
    ) -> CallHierarchyItem | None:
        source = self.source_text(real_path)
        if source is None:
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        node = _find_callable_node(tree, qualified_name)
        if node is None:
            return None
        if isinstance(node, ast.ClassDef):
            kind: CallHierarchyItemKind = "class"
        else:
            kind = "method" if "." in qualified_name else "function"
        if node.decorator_list:
            range_start_line = (
                min(dec.lineno for dec in node.decorator_list) - 1
            )
            range_start_col = min(
                dec.col_offset for dec in node.decorator_list
            )
        else:
            range_start_line = node.lineno - 1
            range_start_col = node.col_offset
        range_end_line = (node.end_lineno or node.lineno) - 1
        range_end_col = node.end_col_offset or 0

        bare_name = qualified_name.rsplit(".", 1)[-1]
        located = self._locate_def_class_name_offsets(
            real_path, node.lineno, bare_name
        )
        if located is None:
            return None
        selection_start_col, selection_end_col = located
        selection_line = node.lineno - 1

        if module_name is None:
            mirror_path = self._mirror_path_for_real(real_path)
            if mirror_path.exists() and mirror_path.suffix == ".py":
                table = self._remap_module_symbol_table(
                    module_symbol_table(
                        self.db, self.mirror_root, str(mirror_path)
                    )
                )
                module_name = table.module

        # Ensure the LSP invariant `selectionRange ⊆ range` even for
        # decorated definitions: the selection line is the header line,
        # which is always after the first decorator line and before
        # `end_lineno`, so the range covers it.
        return CallHierarchyItem(
            name=bare_name,
            kind=kind,
            path=real_path,
            qualified_name=qualified_name,
            detail=module_name,
            range_start_line=range_start_line,
            range_start_character=range_start_col,
            range_end_line=range_end_line,
            range_end_character=range_end_col,
            selection_start_line=selection_line,
            selection_start_character=selection_start_col,
            selection_end_line=selection_line,
            selection_end_character=selection_end_col,
        )

    def _resolve_annotation_type_ref(
        self,
        defining_mirror: Path,
        ref: tuple[str, ...],
    ) -> ResolvedSymbol | None:
        if ref[0] == "name":
            return self._remap_resolved_symbol(
                resolve_symbol(
                    self.db,
                    self.mirror_root,
                    str(defining_mirror),
                    ref[1],
                )
            )
        # ("attribute", lhs_name, attr)
        lhs_resolved = self._remap_resolved_symbol(
            resolve_symbol(
                self.db, self.mirror_root, str(defining_mirror), ref[1]
            )
        )
        if (
            lhs_resolved.resolution != "workspace"
            or lhs_resolved.defining_path is None
        ):
            return None
        lhs_mirror = self._mirror_path_for_real(lhs_resolved.defining_path)
        if not lhs_mirror.exists() or lhs_mirror.suffix != ".py":
            return None
        return self._remap_resolved_symbol(
            resolve_symbol(
                self.db, self.mirror_root, str(lhs_mirror), ref[2]
            )
        )

    def _lookup_callable_signature(
        self, target: ResolvedSymbol
    ) -> tuple[str, Signature] | None:
        if target.defining_path is None or target.defining_lineno is None:
            return None
        defining_mirror = self._mirror_path_for_real(target.defining_path)
        if not defining_mirror.exists() or defining_mirror.suffix != ".py":
            return None
        table = module_symbol_table(
            self.db, self.mirror_root, str(defining_mirror)
        )
        matched = None
        for symbol in table.symbols:
            if symbol.lineno == target.defining_lineno and "." not in symbol.qualified_name:
                matched = symbol
                break
        if matched is None:
            return None
        if matched.kind == "function" and matched.signature is not None:
            return (matched.qualified_name, matched.signature)
        if matched.kind == "class":
            init_qualified = f"{matched.qualified_name}.__init__"
            for inner in table.symbols:
                if (
                    inner.qualified_name == init_qualified
                    and inner.signature is not None
                ):
                    init_params = inner.signature.parameters
                    if init_params and init_params[0].name in ("self", "cls"):
                        init_params = init_params[1:]
                    return (
                        matched.qualified_name,
                        Signature(
                            parameters=init_params,
                            return_annotation=None,
                        ),
                    )
            return (
                matched.qualified_name,
                Signature(parameters=(), return_annotation=None),
            )
        return None

    def rename_symbol(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
        new_name: str,
    ) -> RenameResult:
        bare_old = qualified_name.rsplit(".", 1)[-1]
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            target = self._remap_resolved_symbol(
                resolve_symbol(
                    self.db, self.mirror_root, str(mirror_path), qualified_name
                )
            )
            if not new_name.isidentifier():
                return RenameResult(
                    target=target, edits=(), status="invalid_identifier"
                )
            if keyword.iskeyword(new_name):
                return RenameResult(
                    target=target, edits=(), status="keyword_identifier"
                )
            if new_name == bare_old:
                return RenameResult(target=target, edits=(), status="same_name")
            if target.resolution != "workspace":
                return RenameResult(
                    target=target, edits=(), status="non_workspace_target"
                )
            defining_bare_name = self._defining_bare_name(target)
            if defining_bare_name is not None and defining_bare_name != bare_old:
                return RenameResult(
                    target=target, edits=(), status="alias_rename_unsupported"
                )
            references = find_references(
                self.db,
                self.mirror_root,
                str(mirror_path),
                qualified_name,
                include_declaration=True,
            )
            edits: list[RenameEdit] = []
            for ref in references.references:
                ref_real_path = self._remap_path(ref.path) or ref.path
                col, end_col = ref.col_offset, ref.end_col_offset
                if ref.is_declaration and col == 0 and end_col == 1:
                    located = self._locate_def_class_name_offsets(
                        ref_real_path, ref.lineno, bare_old
                    )
                    if located is None:
                        continue
                    col, end_col = located
                edits.append(
                    RenameEdit(
                        path=ref_real_path,
                        lineno=ref.lineno,
                        col_offset=col,
                        end_col_offset=end_col,
                        new_text=new_name,
                    )
                )
            if target.defining_module is not None:
                edits.extend(
                    self._collect_from_import_edits(
                        defining_module=target.defining_module,
                        bare_old=bare_old,
                        new_name=new_name,
                    )
                )
            seen: set[tuple[str, int, int, int]] = set()
            unique_edits: list[RenameEdit] = []
            for edit in edits:
                key = (edit.path, edit.lineno, edit.col_offset, edit.end_col_offset)
                if key in seen:
                    continue
                seen.add(key)
                unique_edits.append(edit)
            unique_edits.sort(
                key=lambda e: (e.path, e.lineno, e.col_offset)
            )
            return RenameResult(
                target=target, edits=tuple(unique_edits), status="ok"
            )

    def _defining_bare_name(self, target: ResolvedSymbol) -> str | None:
        """Return the bare name of `target` as bound in its defining module.

        Returns None if the symbol can't be located in the defining module's
        symbol table (e.g. resolution data is missing); callers should treat
        None as "best-effort proceed" rather than as a mismatch.
        """
        if target.defining_path is None or target.defining_lineno is None:
            return None
        defining_mirror = self._mirror_path_for_real(target.defining_path)
        if not defining_mirror.exists() or defining_mirror.suffix != ".py":
            return None
        table = module_symbol_table(
            self.db, self.mirror_root, str(defining_mirror)
        )
        for symbol in table.symbols:
            if symbol.lineno != target.defining_lineno:
                continue
            if "." in symbol.qualified_name:
                continue
            return symbol.qualified_name
        return None

    def _locate_symbol_name_offsets(
        self, real_path: str, symbol: Symbol
    ) -> tuple[int, int]:
        """Return ``(col_offset, end_col_offset)`` of ``symbol``'s bare name on
        its header line.

        Scans the file's line at ``symbol.lineno - 1`` for the first
        word-boundary match of the bare-name component of
        ``symbol.qualified_name``. Used to make declaration-style endpoints
        report the actual identifier span rather than a column-0 placeholder.

        Falls back to ``(0, 1)`` when the source is unreadable, the line is
        out of range, or the bare name does not match on that line.
        """
        bare_name = symbol.qualified_name.rsplit(".", 1)[-1]
        source = self.source_text(real_path)
        if source is None:
            return 0, 1
        lines = source.splitlines()
        line_idx = symbol.lineno - 1
        if not (0 <= line_idx < len(lines)):
            return 0, 1
        line = lines[line_idx]
        pattern = re.compile(rf"\b{re.escape(bare_name)}\b")
        match = pattern.search(line)
        if match is None:
            return 0, 1
        return match.start(), match.end()

    def _locate_def_class_name_offsets(
        self, real_path: str, lineno: int, bare_old: str
    ) -> tuple[int, int] | None:
        source = self.source_text(real_path)
        if source is None:
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if (
                isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
                and node.lineno == lineno
                and node.name == bare_old
            ):
                lines = source.splitlines()
                line_idx = lineno - 1
                if not (0 <= line_idx < len(lines)):
                    return None
                line = lines[line_idx]
                pattern = re.compile(rf"\b{re.escape(bare_old)}\b")
                match = pattern.search(line, node.col_offset)
                if match is not None:
                    return match.start(), match.end()
        return None

    def _collect_from_import_edits(
        self,
        *,
        defining_module: str,
        bare_old: str,
        new_name: str,
    ) -> list[RenameEdit]:
        workspace = self._remap_workspace_analysis(
            workspace_analysis(self.db, self.mirror_root)
        )
        edits: list[RenameEdit] = []
        for module in workspace.modules:
            real_path = module.path
            source = self.source_text(real_path)
            if source is None:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                absolute_module = _resolve_import_from_target(
                    importer_module=module.module,
                    importer_path=real_path,
                    level=node.level,
                    module=node.module,
                )
                if absolute_module != defining_module:
                    continue
                for alias in node.names:
                    if alias.name != bare_old:
                        continue
                    if alias.lineno is None or alias.col_offset is None:
                        continue
                    edits.append(
                        RenameEdit(
                            path=real_path,
                            lineno=alias.lineno,
                            col_offset=alias.col_offset,
                            end_col_offset=alias.col_offset + len(bare_old),
                            new_text=new_name,
                        )
                    )
        return edits

    def import_edits_for_file_renames(
        self,
        renames: Sequence[
            tuple[str | os.PathLike[str], str | os.PathLike[str]]
        ],
    ) -> tuple[FileRenameEdit, ...]:
        """Compute import edits that update references to renamed Python files.

        Each ``(old, new)`` pair describes a planned ``.py`` rename inside the
        workspace. The returned edits update every ``import`` and ``from``
        statement across the workspace that currently references one of the
        ``old`` paths' module names, rewriting it to the corresponding ``new``
        module name. Renames where either side is outside the workspace, is
        not a ``.py`` file, is ``__init__.py``, or where the resulting module
        name is unchanged are silently skipped.

        Returned spans are 0-based (LSP-style) and reference the files'
        *current* paths (the old paths for the files being renamed).

        Three rewrite shapes are produced:

        - ``import <old_module> [as alias]`` → replace the dotted-module span
          with ``<new_module>``; any ``as`` clause is preserved.
        - ``from <old_module> import ...`` → replace the dotted-module span
          (including any leading dots). When the importer is inside the same
          package anchor as both old and new modules, the existing ``level``
          is preserved and only the relative tail is rewritten; otherwise the
          statement is rewritten to absolute form (``level == 0``).
        - ``from <pkg> import <leaf> [as alias]`` where ``<pkg>.<leaf> ==
          old_module`` and ``old_module`` and ``new_module`` share the same
          parent package — rewrite ``<leaf>`` to the new leaf, preserving any
          ``as`` clause. Cross-directory submodule rewrites are intentionally
          out of scope: they would require either rewriting usage sites or
          inserting an ``as`` clause, neither of which is well-defined here.
        """
        with self._state_lock:
            self._check_open()
            old_to_new: dict[str, str] = {}
            for old_path, new_path in renames:
                pair = self._resolve_file_rename_pair(old_path, new_path)
                if pair is None:
                    continue
                old_module, new_module = pair
                if old_module == new_module:
                    continue
                old_to_new[old_module] = new_module
            if not old_to_new:
                return ()

            workspace = self._remap_workspace_analysis(
                workspace_analysis(self.db, self.mirror_root)
            )
            edits: list[FileRenameEdit] = []
            for module in workspace.modules:
                edits.extend(
                    self._import_edits_for_one_file(
                        importer_module=module.module,
                        importer_path=module.path,
                        old_to_new=old_to_new,
                    )
                )
            edits.sort(
                key=lambda e: (e.path, e.start_line, e.start_character)
            )
            return tuple(edits)

    def _resolve_file_rename_pair(
        self,
        old_path: str | os.PathLike[str],
        new_path: str | os.PathLike[str],
    ) -> tuple[str, str] | None:
        try:
            old_real = self._normalize_real_path(old_path)
            new_real = self._normalize_real_path(new_path)
        except ValueError:
            return None
        if Path(old_real).suffix != ".py" or Path(new_real).suffix != ".py":
            return None
        if Path(old_real).name == "__init__.py" or Path(new_real).name == "__init__.py":
            return None
        try:
            old_relative = Path(old_real).relative_to(Path(self.root))
            new_relative = Path(new_real).relative_to(Path(self.root))
        except ValueError:
            return None
        old_module = ".".join(old_relative.parts[:-1] + (old_relative.stem,))
        new_module = ".".join(new_relative.parts[:-1] + (new_relative.stem,))
        return old_module, new_module

    def _import_edits_for_one_file(
        self,
        *,
        importer_module: str,
        importer_path: str,
        old_to_new: dict[str, str],
    ) -> list[FileRenameEdit]:
        source = self.source_text(importer_path)
        if source is None:
            return []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        source_lines = source.splitlines()
        edits: list[FileRenameEdit] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    new_module = old_to_new.get(alias.name)
                    if new_module is None:
                        continue
                    if alias.lineno is None or alias.col_offset is None:
                        continue
                    edits.append(
                        FileRenameEdit(
                            path=importer_path,
                            start_line=alias.lineno - 1,
                            start_character=alias.col_offset,
                            end_line=alias.lineno - 1,
                            end_character=alias.col_offset + len(alias.name),
                            new_text=new_module,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from_target(
                    importer_module=importer_module,
                    importer_path=importer_path,
                    level=node.level,
                    module=node.module,
                )
                if resolved in old_to_new:
                    span = _find_from_module_span(source_lines, node)
                    if span is not None:
                        new_module = old_to_new[resolved]
                        replacement = self._rewrite_from_module(
                            importer_module=importer_module,
                            importer_path=importer_path,
                            level=node.level,
                            new_module=new_module,
                        )
                        line_idx, start_col, end_col = span
                        edits.append(
                            FileRenameEdit(
                                path=importer_path,
                                start_line=line_idx,
                                start_character=start_col,
                                end_line=line_idx,
                                end_character=end_col,
                                new_text=replacement,
                            )
                        )
                    # Don't also try the submodule-alias rewrite when the
                    # whole from-target matched: the from-module rewrite
                    # already moved the statement to the new path.
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    candidate = (
                        f"{resolved}.{alias.name}" if resolved else alias.name
                    )
                    new_module = old_to_new.get(candidate)
                    if new_module is None:
                        continue
                    old_parent = candidate.rsplit(".", 1)[0] if "." in candidate else ""
                    new_parent, _, new_leaf = new_module.rpartition(".")
                    if old_parent != new_parent:
                        continue
                    if alias.lineno is None or alias.col_offset is None:
                        continue
                    edits.append(
                        FileRenameEdit(
                            path=importer_path,
                            start_line=alias.lineno - 1,
                            start_character=alias.col_offset,
                            end_line=alias.lineno - 1,
                            end_character=alias.col_offset + len(alias.name),
                            new_text=new_leaf,
                        )
                    )
        return edits

    def _rewrite_from_module(
        self,
        *,
        importer_module: str,
        importer_path: str,
        level: int,
        new_module: str,
    ) -> str:
        """Produce a ``from`` clause replacement that resolves to ``new_module``.

        Preserves the existing relative ``level`` when the new module lives
        under the same package anchor; otherwise rewrites to an absolute
        ``from <new_module>`` form (``level == 0``).
        """
        anchor = _relative_import_anchor(
            importer_module=importer_module,
            importer_path=importer_path,
            level=level,
        )
        if level > 0 and anchor is not None:
            anchor_prefix = f"{anchor}." if anchor else ""
            if new_module == anchor:
                return "." * level
            if new_module.startswith(anchor_prefix):
                tail = new_module[len(anchor_prefix) :] if anchor else new_module
                return ("." * level) + tail
        return new_module

    def import_edits_for_file_deletions(
        self,
        deletions: Sequence[str | os.PathLike[str]],
    ) -> tuple[FileDeletionEdit, ...]:
        """Compute import edits that remove references to deleted Python files.

        Each entry in ``deletions`` is the path of a ``.py`` file that the
        editor is about to delete from the workspace. The returned edits
        remove every ``import`` / ``from`` statement (or single alias inside
        one) across the workspace that currently references one of those
        files' module names; the statements would become broken once the
        files are gone.

        Deletions are silently skipped when the path is outside the
        workspace, is not a ``.py`` file, or is ``__init__.py`` (package
        deletions are a separate feature). The returned edits all carry
        ``new_text == ""``; the spans are 0-based (LSP-style) and reference
        the importer file's *current* path.

        Three rewrite shapes are produced:

        - ``import <deleted_module> [as alias]`` — when this is the only
          alias in the statement, the whole statement is removed (including
          its trailing newline); otherwise only the dead alias plus its
          adjacent comma is removed, leaving the surviving aliases intact.
        - ``from <deleted_module> import ...`` — the whole statement is
          removed; every imported name's source module is gone.
        - ``from <pkg> import <leaf> [as alias]`` where
          ``<pkg>.<leaf> == deleted_module`` — when this is the only
          imported name, the whole statement is removed; otherwise only
          the dead leaf plus its adjacent comma is removed.
        """
        with self._state_lock:
            self._check_open()
            deleted_modules: set[str] = set()
            deleted_importer_paths: set[str] = set()
            for path in deletions:
                resolved = self._resolve_file_deletion(path)
                if resolved is None:
                    continue
                deleted_modules.add(resolved)
                with contextlib.suppress(ValueError):
                    deleted_importer_paths.add(self._normalize_real_path(path))
            if not deleted_modules:
                return ()

            workspace = self._remap_workspace_analysis(
                workspace_analysis(self.db, self.mirror_root)
            )
            edits: list[FileDeletionEdit] = []
            for analysis in workspace.modules:
                if analysis.path in deleted_importer_paths:
                    continue
                edits.extend(
                    self._import_deletion_edits_for_one_file(
                        importer_module=analysis.module,
                        importer_path=analysis.path,
                        deleted_modules=deleted_modules,
                    )
                )
            edits.sort(
                key=lambda e: (e.path, e.start_line, e.start_character)
            )
            return tuple(edits)

    def _resolve_file_deletion(
        self, path: str | os.PathLike[str]
    ) -> str | None:
        try:
            real = self._normalize_real_path(path)
        except ValueError:
            return None
        if Path(real).suffix != ".py":
            return None
        if Path(real).name == "__init__.py":
            return None
        try:
            relative = Path(real).relative_to(Path(self.root))
        except ValueError:
            return None
        return ".".join(relative.parts[:-1] + (relative.stem,))

    def _import_deletion_edits_for_one_file(
        self,
        *,
        importer_module: str,
        importer_path: str,
        deleted_modules: set[str],
    ) -> list[FileDeletionEdit]:
        source = self.source_text(importer_path)
        if source is None:
            return []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        edits: list[FileDeletionEdit] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edits.extend(
                    self._delete_edits_for_import(
                        importer_path=importer_path,
                        source=source,
                        node=node,
                        deleted_modules=deleted_modules,
                    )
                )
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from_target(
                    importer_module=importer_module,
                    importer_path=importer_path,
                    level=node.level,
                    module=node.module,
                )
                if resolved is not None and resolved in deleted_modules:
                    span = _statement_line_span(source, node)
                    if span is not None:
                        start_line, end_line = span
                        edits.append(
                            FileDeletionEdit(
                                path=importer_path,
                                start_line=start_line,
                                start_character=0,
                                end_line=end_line,
                                end_character=0,
                            )
                        )
                    continue
                edits.extend(
                    self._delete_edits_for_from_aliases(
                        importer_path=importer_path,
                        source=source,
                        node=node,
                        resolved_module=resolved,
                        deleted_modules=deleted_modules,
                    )
                )
        return edits

    def _delete_edits_for_import(
        self,
        *,
        importer_path: str,
        source: str,
        node: ast.Import,
        deleted_modules: set[str],
    ) -> list[FileDeletionEdit]:
        dead_indices = [
            i for i, alias in enumerate(node.names) if alias.name in deleted_modules
        ]
        if not dead_indices:
            return []
        if len(dead_indices) == len(node.names):
            span = _statement_line_span(source, node)
            if span is None:
                return []
            start_line, end_line = span
            return [
                FileDeletionEdit(
                    path=importer_path,
                    start_line=start_line,
                    start_character=0,
                    end_line=end_line,
                    end_character=0,
                )
            ]
        return _alias_list_deletion_edits(
            importer_path=importer_path,
            source=source,
            aliases=node.names,
            dead_indices=dead_indices,
        )

    def _delete_edits_for_from_aliases(
        self,
        *,
        importer_path: str,
        source: str,
        node: ast.ImportFrom,
        resolved_module: str | None,
        deleted_modules: set[str],
    ) -> list[FileDeletionEdit]:
        dead_indices: list[int] = []
        for i, alias in enumerate(node.names):
            if alias.name == "*":
                continue
            candidate = (
                f"{resolved_module}.{alias.name}" if resolved_module else alias.name
            )
            if candidate not in deleted_modules:
                continue
            dead_indices.append(i)
        if not dead_indices:
            return []
        if len(dead_indices) == len(node.names):
            span = _statement_line_span(source, node)
            if span is None:
                return []
            start_line, end_line = span
            return [
                FileDeletionEdit(
                    path=importer_path,
                    start_line=start_line,
                    start_character=0,
                    end_line=end_line,
                    end_character=0,
                )
            ]
        return _alias_list_deletion_edits(
            importer_path=importer_path,
            source=source,
            aliases=node.names,
            dead_indices=dead_indices,
        )

    def source_text(self, path: str | os.PathLike[str]) -> str | None:
        real_path = self._normalize_real_path(path)
        with self._state_lock:
            if self._closed:
                return None
            overlay = self._overlays.get(real_path)
        if overlay is not None:
            return overlay
        try:
            return Path(real_path).read_text(encoding="utf-8")
        except OSError:
            return None

    def _build_file_result(
        self,
        real_path: str,
        dependency_inputs: _DependencyInputs,
        dependency_check: DependencyCheckAnalysis,
        *,
        module: PythonModuleAnalysis | None = None,
    ) -> FileAnalysisResult:
        diagnostics = list(
            self._dependency_status_diagnostics(
                dependency_inputs,
                dependency_check,
                only_path=real_path,
            )
        )
        mirror_path = self._mirror_path_for_real(real_path)
        if not mirror_path.exists() or mirror_path.suffix != ".py":
            return FileAnalysisResult(
                path=real_path,
                module=None,
                symbols=None,
                dependency_check=dependency_check,
                diagnostics=self._dedupe_diagnostics(tuple(diagnostics)),
            )

        module_result = module or self._remap_module_analysis(
            module_analysis(self.db, self.mirror_root, str(mirror_path))
        )
        symbol_table = self._remap_module_symbol_table(
            module_symbol_table(self.db, self.mirror_root, str(mirror_path))
        )
        diagnostics.extend(
            self._module_diagnostics(
                real_path,
                str(mirror_path),
                module_result,
                dependency_check,
            )
        )
        return FileAnalysisResult(
            path=real_path,
            module=module_result,
            symbols=symbol_table,
            dependency_check=dependency_check,
            diagnostics=self._dedupe_diagnostics(tuple(diagnostics)),
        )

    def _module_diagnostics(
        self,
        real_path: str,
        mirror_path: str,
        module_result: PythonModuleAnalysis,
        dependency_check: DependencyCheckAnalysis,
    ) -> tuple[AnalysisDiagnostic, ...]:
        diagnostics: list[AnalysisDiagnostic] = []
        declared_names = {status.name for status in dependency_check.statuses}

        for diagnostic in module_result.diagnostics:
            diagnostics.append(
                AnalysisDiagnostic(
                    path=real_path,
                    code=diagnostic.code,
                    message=diagnostic.message,
                    severity="error",
                    source="pyinc.python_source",
                    lineno=diagnostic.lineno,
                    col_offset=diagnostic.col_offset,
                )
            )

        for resolved_import in module_result.resolved_imports:
            if resolved_import.resolution == "missing":
                diagnostics.append(
                    AnalysisDiagnostic(
                        path=real_path,
                        code="missing-import",
                        message=f"Import {resolved_import.module!r} could not be resolved.",
                        severity="error",
                        source="pyinc.python_source",
                        lineno=resolved_import.lineno,
                        col_offset=0,
                    )
                )
            elif resolved_import.resolution == "ambiguous":
                diagnostics.append(
                    AnalysisDiagnostic(
                        path=real_path,
                        code="ambiguous-import",
                        message=f"Import {resolved_import.module!r} resolved ambiguously.",
                        severity="warning",
                        source="pyinc.python_source",
                        lineno=resolved_import.lineno,
                        col_offset=0,
                    )
                )

            if (
                resolved_import.resolution == "installed"
                and resolved_import.distribution_name is not None
                and _normalize_dependency_name(resolved_import.distribution_name)
                not in declared_names
            ):
                diagnostics.append(
                    AnalysisDiagnostic(
                        path=real_path,
                        code="undeclared-import",
                        message=(
                            f"Import {resolved_import.module!r} comes from installed distribution "
                            f"{resolved_import.distribution_name!r}, but that dependency is not declared."
                        ),
                        severity="warning",
                        source="pyinc.dependency_check",
                        lineno=resolved_import.lineno,
                        col_offset=0,
                    )
                )

            if (
                resolved_import.resolution == "workspace"
                and resolved_import.imported_name is not None
                and resolved_import.imported_name != "*"
            ):
                symbol_result = self._remap_resolved_symbol(
                    resolve_symbol(
                        self.db,
                        self.mirror_root,
                        mirror_path,
                        resolved_import.imported_name,
                    )
                )
                if symbol_result.resolution == "missing":
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=real_path,
                            code="unresolved-symbol",
                            message=(
                                f"Imported name {resolved_import.imported_name!r} could not be resolved "
                                f"from {resolved_import.module!r}."
                            ),
                            severity="error",
                            source="pyinc.symbol_resolution",
                            lineno=resolved_import.lineno,
                            col_offset=0,
                        )
                    )
                elif symbol_result.resolution == "ambiguous":
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=real_path,
                            code="ambiguous-symbol",
                            message=(
                                f"Imported name {resolved_import.imported_name!r} is ambiguous "
                                f"when resolved from {resolved_import.module!r}."
                            ),
                            severity="warning",
                            source="pyinc.symbol_resolution",
                            lineno=resolved_import.lineno,
                            col_offset=0,
                        )
                    )

        return tuple(diagnostics)

    def _dependency_inputs(self) -> _DependencyInputs:
        config = workspace_config_analysis(self.db, self.mirror_root)
        requirements = workspace_requirements_analysis(self.db, self.mirror_root)

        declared: list[str] = []
        if config is not None:
            declared.extend(config.dependencies)
            for _, group_entries in config.optional_dependency_groups:
                declared.extend(group_entries)
        if requirements is not None:
            declared.extend(
                requirement.raw_line for requirement in requirements.requirements
            )

        return _DependencyInputs(
            config=self._remap_config_analysis(config),
            requirements=self._remap_requirements_analysis(requirements),
            declared_dependencies=tuple(dict.fromkeys(declared)),
        )

    def _dependency_status_diagnostics(
        self,
        dependency_inputs: _DependencyInputs,
        dependency_check: DependencyCheckAnalysis,
        *,
        only_path: str | None = None,
    ) -> tuple[AnalysisDiagnostic, ...]:
        diagnostics: list[AnalysisDiagnostic] = []
        requirements_by_name: dict[str, tuple[str, int]] = {}
        if dependency_inputs.requirements is not None:
            for requirement in dependency_inputs.requirements.requirements:
                requirements_by_name.setdefault(
                    _normalize_dependency_name(requirement.name),
                    (dependency_inputs.requirements.path, requirement.lineno),
                )

        config_path = (
            dependency_inputs.config.path
            if dependency_inputs.config is not None
            else None
        )

        for status in dependency_check.statuses:
            if status.status == "satisfied":
                continue

            if status.name in requirements_by_name:
                path, lineno = requirements_by_name[status.name]
            elif config_path is not None:
                path, lineno = config_path, 1
            else:
                continue

            if only_path is not None and path != only_path:
                continue

            message = self._dependency_status_message(status)
            diagnostics.append(
                AnalysisDiagnostic(
                    path=path,
                    code=f"dependency-{status.status}",
                    message=message,
                    severity="warning",
                    source="pyinc.dependency_check",
                    lineno=lineno,
                    col_offset=0,
                )
            )

        if dependency_check.diagnostics:
            target_path: str | None = None
            if dependency_inputs.requirements is not None:
                target_path = dependency_inputs.requirements.path
            elif dependency_inputs.config is not None:
                target_path = dependency_inputs.config.path
            if target_path is not None and (
                only_path is None or target_path == only_path
            ):
                for code, message in dependency_check.diagnostics:
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=target_path,
                            code=code,
                            message=message,
                            severity="warning",
                            source="pyinc.dependency_check",
                            lineno=1,
                            col_offset=0,
                        )
                    )

        return self._dedupe_diagnostics(tuple(diagnostics))

    def _dependency_status_message(self, status: DependencyStatus) -> str:
        if status.status == "missing":
            return f"Declared dependency {status.name!r} is not installed."
        if status.status == "version_mismatch":
            return (
                f"Declared dependency {status.name!r} with constraint {status.declared_spec!r} "
                f"does not match installed version {status.installed_version!r}: {status.detail}"
            )
        return (
            f"Declared dependency {status.name!r} could not be evaluated: "
            f"{status.detail or status.declared_spec!r}"
        )

    def _copy_workspace_into_mirror(self) -> None:
        for current_root, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [
                name for name in dirnames if name not in self._ignored_dir_names
            ]
            relative_dir = (
                Path(current_root).resolve(strict=False).relative_to(Path(self.root))
            )
            target_dir = self._mirror_root_path / relative_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            for filename in filenames:
                source_path = Path(current_root, filename)
                target_path = target_dir / filename
                shutil.copy2(source_path, target_path)

    def _normalize_real_path(self, path: str | os.PathLike[str]) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(self.root, candidate)
        normalized = candidate.resolve(strict=False)
        try:
            normalized.relative_to(Path(self.root))
        except ValueError as exc:
            raise ValueError(
                f"{normalized!s} is outside the workspace root {self.root!r}."
            ) from exc
        return str(normalized)

    def _mirror_path_for_real(self, real_path: str) -> Path:
        relative_path = Path(real_path).relative_to(Path(self.root))
        return self._mirror_root_path / relative_path

    def _sync_path_from_disk(self, real_path: str) -> None:
        source_path = Path(real_path)
        mirror_path = self._mirror_path_for_real(real_path)
        if source_path.exists():
            if source_path.is_dir():
                mirror_path.mkdir(parents=True, exist_ok=True)
                return
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, mirror_path)
            return

        if mirror_path.is_dir():
            shutil.rmtree(mirror_path)
        elif mirror_path.exists():
            mirror_path.unlink()
        self._prune_empty_parents(mirror_path.parent)

    def _prune_empty_parents(self, directory: Path) -> None:
        while directory != self._mirror_root_path:
            if not directory.exists():
                directory = directory.parent
                continue
            try:
                next(directory.iterdir())
                return
            except StopIteration:
                directory.rmdir()
                directory = directory.parent

    def _remap_workspace_analysis(
        self, analysis: PythonWorkspaceAnalysis
    ) -> PythonWorkspaceAnalysis:
        return PythonWorkspaceAnalysis(
            root=self.root,
            modules=tuple(
                self._remap_module_analysis(module) for module in analysis.modules
            ),
        )

    def _remap_module_analysis(
        self, analysis: PythonModuleAnalysis
    ) -> PythonModuleAnalysis:
        return PythonModuleAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            module=analysis.module,
            imports=analysis.imports,
            definitions=analysis.definitions,
            diagnostics=analysis.diagnostics,
            resolved_imports=tuple(
                ResolvedImportRef(
                    module=item.module,
                    kind=item.kind,
                    lineno=item.lineno,
                    imported_name=item.imported_name,
                    resolved_module=item.resolved_module,
                    resolved_path=self._remap_path(item.resolved_path),
                    resolution=item.resolution,
                    distribution_name=item.distribution_name,
                    distribution_version=item.distribution_version,
                )
                for item in analysis.resolved_imports
            ),
            dependencies=tuple(
                DependencySurface(
                    module=item.module,
                    path=self._remap_path(item.path) or item.path,
                    exports=item.exports,
                )
                for item in analysis.dependencies
            ),
        )

    def _remap_module_symbol_table(self, table: ModuleSymbolTable) -> ModuleSymbolTable:
        return ModuleSymbolTable(
            module=table.module,
            path=self._remap_path(table.path) or table.path,
            symbols=table.symbols,
            impurity_reasons=table.impurity_reasons,
        )

    def _remap_workspace_symbol_index(
        self, index: WorkspaceSymbolIndex
    ) -> WorkspaceSymbolIndex:
        return WorkspaceSymbolIndex(
            root=self.root,
            entries=index.entries,
        )

    def _remap_resolved_symbol(self, symbol: ResolvedSymbol) -> ResolvedSymbol:
        return ResolvedSymbol(
            original_module=symbol.original_module,
            qualified_name=symbol.qualified_name,
            resolution=symbol.resolution,
            defining_module=symbol.defining_module,
            defining_path=self._remap_path(symbol.defining_path),
            defining_lineno=symbol.defining_lineno,
            distribution_name=symbol.distribution_name,
            distribution_version=symbol.distribution_version,
            follow_depth=symbol.follow_depth,
            trail=symbol.trail,
        )

    def _remap_config_analysis(
        self, analysis: ConfigAnalysis | None
    ) -> ConfigAnalysis | None:
        if analysis is None:
            return None
        return ConfigAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            sections=analysis.sections,
            dependencies=analysis.dependencies,
            optional_dependency_groups=analysis.optional_dependency_groups,
            tool_configs=analysis.tool_configs,
            diagnostics=analysis.diagnostics,
        )

    def _remap_requirements_analysis(
        self, analysis: RequirementsAnalysis | None
    ) -> RequirementsAnalysis | None:
        if analysis is None:
            return None
        return RequirementsAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            requirements=analysis.requirements,
            file_references=analysis.file_references,
            index_directives=analysis.index_directives,
            diagnostics=analysis.diagnostics,
        )

    def _remap_path(self, path: str | None) -> str | None:
        if path is None:
            return None
        candidate = Path(path)
        try:
            relative_path = candidate.relative_to(self._mirror_root_path)
        except ValueError:
            return path
        return str(Path(self.root, relative_path))

    def _dedupe_diagnostics(
        self, diagnostics: tuple[AnalysisDiagnostic, ...]
    ) -> tuple[AnalysisDiagnostic, ...]:
        seen: set[tuple[str, str, str, str, str, int | None, int | None]] = set()
        ordered: list[AnalysisDiagnostic] = []
        for diagnostic in diagnostics:
            key = (
                diagnostic.path,
                diagnostic.code,
                diagnostic.message,
                diagnostic.severity,
                diagnostic.source,
                diagnostic.lineno,
                diagnostic.col_offset,
            )
            if key in seen:
                continue
            seen.add(key)
            ordered.append(diagnostic)
        return tuple(ordered)


class PollingWorkspaceWatcher:
    def __init__(
        self,
        session: WorkspaceSession,
        *,
        debounce_ms: int = 200,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session = session
        self._debounce_seconds = debounce_ms / 1000.0
        self._clock = clock or time.monotonic
        self._snapshot = _collect_filesystem_snapshot(
            self._session.root,
            self._session._ignored_dir_names,
        )
        self._pending: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._on_change: Callable[[tuple[str, ...]], None] | None = None
        self._on_error: Callable[[Exception], None] | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def poll(self) -> tuple[str, ...]:
        if self.is_running:
            raise RuntimeError(
                "PollingWorkspaceWatcher is running; stop() it before calling poll() directly."
            )
        return self._poll_once()

    def _poll_once(self) -> tuple[str, ...]:
        now = self._clock()
        current_snapshot = _collect_filesystem_snapshot(
            self._session.root,
            self._session._ignored_dir_names,
        )

        changed_paths = {
            path
            for path in set(self._snapshot) | set(current_snapshot)
            if self._snapshot.get(path) != current_snapshot.get(path)
        }
        for path in changed_paths:
            self._pending[path] = now

        ready = tuple(
            sorted(
                path
                for path, seen_at in self._pending.items()
                if now - seen_at >= self._debounce_seconds
            )
        )
        for path in ready:
            self._pending.pop(path, None)
        if ready:
            self._session.refresh_paths(list(ready))

        self._snapshot = current_snapshot
        return ready

    def start(
        self,
        on_change: Callable[[tuple[str, ...]], None],
        *,
        interval_s: float | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if self.is_running:
            raise RuntimeError("PollingWorkspaceWatcher is already running.")
        effective_interval = (
            interval_s
            if interval_s is not None
            else max(self._debounce_seconds / 2.0, 0.05)
        )
        self._on_change = on_change
        self._on_error = on_error
        self._stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(effective_interval,),
            name="pyinc-tools-watcher",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout)
        if thread.is_alive():
            print(
                "pyinc-tools watcher: thread did not stop within timeout",
                file=sys.stderr,
            )
        self._thread = None
        self._on_change = None
        self._on_error = None

    def __enter__(self) -> PollingWorkspaceWatcher:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.stop()

    def _run(self, interval_s: float) -> None:
        while not self._stop_event.is_set():
            try:
                ready = self._poll_once()
            except RuntimeError:
                # Session was closed out from under us; exit cleanly.
                return
            except Exception as exc:  # pragma: no cover - defensive
                self._handle_error(exc)
                ready = ()
            if ready:
                callback = self._on_change
                if callback is not None:
                    try:
                        callback(ready)
                    except Exception as exc:
                        self._handle_error(exc)
            if self._stop_event.wait(interval_s):
                return

    def _handle_error(self, exc: Exception) -> None:
        if self._on_error is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                self._on_error(exc)
            return
        print(
            f"pyinc-tools watcher: callback raised: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
