from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from pyinc.integrations import SourcePosition, SourceRange

from ._document import _source_line_count
from ._models import FileDeletionEdit


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


def _relative_import_anchor(*, importer_module: str, importer_path: str, level: int) -> str | None:
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


def _import_node_for_line(
    nodes: Sequence[ast.Import | ast.ImportFrom], lineno: int | None
) -> ast.Import | ast.ImportFrom | None:
    """Return the import statement whose line span covers ``lineno``.

    Diagnostics anchor at different points: ``unused-import`` sits on the
    individual alias line, which in a parenthesised multi-line import is
    *not* the statement's first line, whereas ``missing-import`` /
    ``unresolved-symbol`` sit on the statement line. A span-aware lookup
    (``node.lineno <= lineno <= node.end_lineno``) matches all three; import
    statements never overlap, so at most one node matches.
    """
    if lineno is None:
        return None
    for node in nodes:
        end = node.end_lineno if node.end_lineno is not None else node.lineno
        if node.lineno <= lineno <= end:
            return node
    return None


def _static_module_all_names(tree: ast.Module) -> frozenset[str]:
    """Names in the module's *static* ``__all__``, or empty when it has none.

    Mirrors the integration's ``static_all_names`` notion: only a literal
    ``__all__`` list / tuple / set of string constants at module scope
    counts. A dynamically built or mutated ``__all__`` cannot be inspected
    statically and yields the empty set (no suppression). Used to leave
    intentional public re-exports (``from m import foo`` with ``foo`` in this
    module's own ``__all__``) unflagged.
    """
    names: set[str] = set()
    has_static = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        literal: set[str] = set()
        if value is None or not isinstance(value, (ast.List, ast.Set, ast.Tuple)):
            # A non-literal `__all__` is dynamic — can't confirm membership.
            return frozenset()
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return frozenset()
            literal.add(item.value)
        names = literal
        has_static = True
    return frozenset(names) if has_static else frozenset()


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
    total_lines = _source_line_count(source)
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
    stays well-formed afterwards. Adjacent dead aliases are coalesced into one
    edit so callers never receive overlapping ranges.

    Runs containing aliases whose source positions are missing are skipped
    (the surviving statement still references the deleted module but at least
    the file is not mis-edited).
    """
    del source  # AST positions are sufficient; the source is unused here.
    edits: list[FileDeletionEdit] = []
    dead = set(dead_indices)
    for first_index in sorted(dead):
        if first_index > 0 and first_index - 1 in dead:
            continue
        last_index = first_index
        while last_index + 1 < len(aliases) and last_index + 1 in dead:
            last_index += 1
        run = aliases[first_index : last_index + 1]
        if any(
            alias.lineno is None
            or alias.col_offset is None
            or alias.end_lineno is None
            or alias.end_col_offset is None
            for alias in run
        ):
            continue
        first_alias = run[0]
        last_alias = run[-1]
        assert first_alias.lineno is not None
        assert first_alias.col_offset is not None
        assert last_alias.end_lineno is not None
        assert last_alias.end_col_offset is not None
        alias_start_line = first_alias.lineno - 1
        alias_start_char = first_alias.col_offset
        alias_end_line = last_alias.end_lineno - 1
        alias_end_char = last_alias.end_col_offset

        # Decide which adjacent comma to absorb.
        prev_alive_idx: int | None = None
        for j in range(first_index - 1, -1, -1):
            if j not in dead:
                prev_alive_idx = j
                break
        next_alive_idx: int | None = None
        for j in range(last_index + 1, len(aliases)):
            if j not in dead:
                next_alive_idx = j
                break

        if next_alive_idx is not None:
            # Absorb the trailing comma + whitespace up to the next alive
            # alias's start, so the surviving alias slides into this slot.
            next_alias = aliases[next_alive_idx]
            if next_alias.lineno is None or next_alias.col_offset is None:
                continue
            end_line = next_alias.lineno - 1
            end_char = next_alias.col_offset
            edits.append(
                FileDeletionEdit(
                    path=importer_path,
                    range=SourceRange(
                        SourcePosition(alias_start_line, alias_start_char),
                        SourcePosition(end_line, end_char),
                    ),
                )
            )
        elif prev_alive_idx is not None:
            # No surviving alias after us — absorb the preceding comma so
            # the surviving alias before us doesn't end with a trailing `,`.
            prev_alias = aliases[prev_alive_idx]
            if prev_alias.end_lineno is None or prev_alias.end_col_offset is None:
                continue
            start_line = prev_alias.end_lineno - 1
            start_char = prev_alias.end_col_offset
            edits.append(
                FileDeletionEdit(
                    path=importer_path,
                    range=SourceRange(
                        SourcePosition(start_line, start_char),
                        SourcePosition(alias_end_line, alias_end_char),
                    ),
                )
            )
        else:
            # No surviving sibling at all — caller should have routed this
            # to the whole-statement removal path, but emit a span-only
            # edit defensively rather than misbehaving silently.
            edits.append(
                FileDeletionEdit(
                    path=importer_path,
                    range=SourceRange(
                        SourcePosition(alias_start_line, alias_start_char),
                        SourcePosition(alias_end_line, alias_end_char),
                    ),
                )
            )
    return edits


__all__ = [
    "_alias_list_deletion_edits",
    "_find_from_module_span",
    "_import_node_for_line",
    "_relative_import_anchor",
    "_resolve_import_from_target",
    "_statement_line_span",
    "_static_module_all_names",
]
