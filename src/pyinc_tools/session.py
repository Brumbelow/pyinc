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
    PythonModuleAnalysis,
    PythonWorkspaceAnalysis,
    Reference,
    ReferenceQueryResult,
    RequirementsAnalysis,
    ResolvedImportRef,
    ResolvedSymbol,
    Signature,
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


DocumentHighlightKind = Literal["text", "read", "write"]


@dataclass(frozen=True)
class DocumentHighlight:
    lineno: int
    col_offset: int
    end_col_offset: int
    kind: DocumentHighlightKind


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
