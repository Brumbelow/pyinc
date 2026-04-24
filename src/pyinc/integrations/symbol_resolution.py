from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pyinc.core import query
from pyinc.integrations.python_source import (
    module_binding_analysis_payload,
    module_wildcard_export_surface,
    resolved_imports_for_file,
    source_text,
    workspace_python_files,
)
from pyinc.runtime import Database
from pyinc.value import thaw

# ---------------------------------------------------------------------------
# Literal aliases
# ---------------------------------------------------------------------------

SymbolKind: TypeAlias = Literal[
    "function",
    "method",
    "class",
    "class_variable",
    "variable",
    "import_alias",
    "from_import_alias",
    "wildcard_import_stub",
]

SymbolResolutionKind: TypeAlias = Literal[
    "workspace",
    "stdlib",
    "installed",
    "external",
    "missing",
    "ambiguous",
]

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------

ParameterPayload: TypeAlias = tuple[str, str | None]
#                                    name, annotation_text

SignaturePayload: TypeAlias = tuple[tuple[ParameterPayload, ...], str | None]
#                                    parameters,                   return_annotation

SymbolPayload: TypeAlias = tuple[
    str,
    SymbolKind,
    int,
    str | None,
    SignaturePayload | None,
    str | None,
    str | None,
]
#   qualified_name, kind, lineno, annotation, signature, import_source_module, import_source_name

ModuleSymbolTablePayload: TypeAlias = tuple[
    str,
    str,
    tuple[SymbolPayload, ...],
    tuple[str, ...],
]
#   module, path, symbols, impurity_reasons

ResolvedSymbolPayload: TypeAlias = tuple[
    str,
    str,
    SymbolResolutionKind,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    int,
    tuple[str, ...],
]
#   original_module, qualified_name, resolution, defining_module,
#   defining_path, defining_lineno, distribution_name, distribution_version,
#   follow_depth, trail

WorkspaceSymbolEntryPayload: TypeAlias = tuple[
    str,
    str,
    SymbolKind,
    int,
    str | None,
]
#   module, qualified_name, kind, lineno, annotation

WorkspaceSymbolIndexPayload: TypeAlias = tuple[
    str,
    tuple[WorkspaceSymbolEntryPayload, ...],
]
#   root, entries

NameOccurrencePayload: TypeAlias = tuple[str, int, int, int]
#                                    bare_name, lineno, col_offset, end_col_offset

FileNameOccurrencesPayload: TypeAlias = tuple[str, tuple[NameOccurrencePayload, ...]]
#                                    path,  occurrences

WorkspaceNameOccurrencesPayload: TypeAlias = tuple[FileNameOccurrencesPayload, ...]

ReferenceEntryPayload: TypeAlias = tuple[str, int, int, int, bool]
#                                    path, lineno, col_offset, end_col_offset, is_declaration

ReferenceQueryResultPayload: TypeAlias = tuple[
    ResolvedSymbolPayload,
    tuple[ReferenceEntryPayload, ...],
]
#   target, references

# ---------------------------------------------------------------------------
# Result dataclasses (Layer 3 public API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Parameter:
    name: str
    annotation: str | None


@dataclass(frozen=True)
class Signature:
    parameters: tuple[Parameter, ...]
    return_annotation: str | None


@dataclass(frozen=True)
class Symbol:
    qualified_name: str
    kind: SymbolKind
    lineno: int
    annotation: str | None
    signature: Signature | None
    import_source_module: str | None
    import_source_name: str | None


@dataclass(frozen=True)
class ModuleSymbolTable:
    module: str
    path: str
    symbols: tuple[Symbol, ...]
    impurity_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedSymbol:
    original_module: str
    qualified_name: str
    resolution: SymbolResolutionKind
    defining_module: str | None
    defining_path: str | None
    defining_lineno: int | None
    distribution_name: str | None
    distribution_version: str | None
    follow_depth: int
    trail: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceSymbolEntry:
    module: str
    qualified_name: str
    kind: SymbolKind
    lineno: int
    annotation: str | None


@dataclass(frozen=True)
class WorkspaceSymbolIndex:
    root: str
    entries: tuple[WorkspaceSymbolEntry, ...]


@dataclass(frozen=True)
class Reference:
    path: str
    lineno: int
    col_offset: int
    end_col_offset: int
    is_declaration: bool


@dataclass(frozen=True)
class ReferenceQueryResult:
    target: ResolvedSymbol
    references: tuple[Reference, ...]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

MAX_FOLLOW_DEPTH = 8

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_path(path: str | os.PathLike[str]) -> str:
    return os.fspath(path)


def _try_parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _module_name_for_path(root: str, path: str) -> str:
    relative_path = Path(path).relative_to(Path(root))
    if relative_path.suffix != ".py":
        raise ValueError(f"{path!r} is not a Python source file under {root!r}.")
    if relative_path.name == "__init__.py":
        module_parts = relative_path.parts[:-1]
    else:
        module_parts = relative_path.parts[:-1] + (relative_path.stem,)
    return ".".join(module_parts)


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


def _annotation_text(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    return ast.unparse(node)


def _parameter_payloads_from_args(args: ast.arguments) -> tuple[ParameterPayload, ...]:
    params: list[ParameterPayload] = []
    for arg in args.posonlyargs:
        params.append((arg.arg, _annotation_text(arg.annotation)))
    for arg in args.args:
        params.append((arg.arg, _annotation_text(arg.annotation)))
    if args.vararg is not None:
        params.append(
            (f"*{args.vararg.arg}", _annotation_text(args.vararg.annotation))
        )
    for arg in args.kwonlyargs:
        params.append((arg.arg, _annotation_text(arg.annotation)))
    if args.kwarg is not None:
        params.append(
            (f"**{args.kwarg.arg}", _annotation_text(args.kwarg.annotation))
        )
    return tuple(params)


def _signature_payload(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> SignaturePayload:
    return (
        _parameter_payloads_from_args(node.args),
        _annotation_text(node.returns),
    )


_TOP_LEVEL_ALLOWED_NODE_TYPES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Assign,
    ast.AnnAssign,
    ast.Import,
    ast.ImportFrom,
    ast.Pass,
    ast.Expr,
)


def _class_body_walk(
    cls: ast.ClassDef, qualifier: str, out: list[SymbolPayload]
) -> None:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(
                (
                    f"{qualifier}.{node.name}",
                    "method",
                    node.lineno,
                    None,
                    _signature_payload(node),
                    None,
                    None,
                )
            )
            continue
        if isinstance(node, ast.ClassDef):
            nested_qualifier = f"{qualifier}.{node.name}"
            out.append(
                (
                    nested_qualifier,
                    "class",
                    node.lineno,
                    None,
                    None,
                    None,
                    None,
                )
            )
            _class_body_walk(node, nested_qualifier, out)
            continue
        if isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name):
                continue
            out.append(
                (
                    f"{qualifier}.{node.target.id}",
                    "class_variable",
                    node.lineno,
                    _annotation_text(node.annotation),
                    None,
                    None,
                    None,
                )
            )
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _target_bound_names(target):
                    if name == "__all__":
                        continue
                    out.append(
                        (
                            f"{qualifier}.{name}",
                            "class_variable",
                            node.lineno,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
            continue


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


def _type_checking_block_walk(
    body: list[ast.stmt], symbols: list[SymbolPayload]
) -> None:
    """Collect import symbols from the body of an ``if TYPE_CHECKING:`` block."""
    for stmt in body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = _bound_name_for_import(alias)
                symbols.append(
                    (bound, "import_alias", stmt.lineno, None, None, alias.name, None)
                )
        elif isinstance(stmt, ast.ImportFrom):
            module_label = ("." * stmt.level) + (stmt.module or "")
            for alias in stmt.names:
                if alias.name == "*":
                    symbols.append(
                        (
                            "*",
                            "wildcard_import_stub",
                            stmt.lineno,
                            None,
                            None,
                            module_label,
                            "*",
                        )
                    )
                    continue
                bound = _bound_name_for_from_import(alias)
                symbols.append(
                    (
                        bound,
                        "from_import_alias",
                        stmt.lineno,
                        None,
                        None,
                        module_label,
                        alias.name,
                    )
                )


def _module_symbol_walk(
    tree: ast.Module,
) -> tuple[tuple[SymbolPayload, ...], tuple[str, ...]]:
    symbols: list[SymbolPayload] = []
    impurity_reasons: list[str] = []
    seen_reasons: set[str] = set()

    def _record_reason(reason: str) -> None:
        if reason not in seen_reasons:
            seen_reasons.add(reason)
            impurity_reasons.append(reason)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                (
                    node.name,
                    "function",
                    node.lineno,
                    None,
                    _signature_payload(node),
                    None,
                    None,
                )
            )
            continue
        if isinstance(node, ast.ClassDef):
            symbols.append(
                (
                    node.name,
                    "class",
                    node.lineno,
                    None,
                    None,
                    None,
                    None,
                )
            )
            _class_body_walk(node, node.name, symbols)
            continue
        if isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name):
                continue
            name = node.target.id
            if name == "__all__":
                continue
            symbols.append(
                (
                    name,
                    "variable",
                    node.lineno,
                    _annotation_text(node.annotation),
                    None,
                    None,
                    None,
                )
            )
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _target_bound_names(target):
                    if name == "__all__":
                        continue
                    symbols.append(
                        (
                            name,
                            "variable",
                            node.lineno,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = _bound_name_for_import(alias)
                symbols.append(
                    (
                        bound,
                        "import_alias",
                        node.lineno,
                        None,
                        None,
                        alias.name,
                        None,
                    )
                )
            continue
        if isinstance(node, ast.ImportFrom):
            module_label = ("." * node.level) + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    symbols.append(
                        (
                            "*",
                            "wildcard_import_stub",
                            node.lineno,
                            None,
                            None,
                            module_label,
                            "*",
                        )
                    )
                    continue
                bound = _bound_name_for_from_import(alias)
                symbols.append(
                    (
                        bound,
                        "from_import_alias",
                        node.lineno,
                        None,
                        None,
                        module_label,
                        alias.name,
                    )
                )
            continue
        if isinstance(node, (ast.Pass, ast.Expr)):
            continue
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            _type_checking_block_walk(node.body, symbols)
            continue
        _record_reason("conditional top-level binding")

    return tuple(symbols), tuple(impurity_reasons)


# ---------------------------------------------------------------------------
# Layer 1 query: per-file symbol table
# ---------------------------------------------------------------------------


@query
def module_symbol_table_payload(
    db: Database, path: str
) -> ModuleSymbolTablePayload:
    source = source_text(db, path)
    tree = _try_parse(source)
    if tree is None:
        return ("", path, tuple(), ("syntax error",))
    symbols, impurity = _module_symbol_walk(tree)
    return ("", path, symbols, impurity)


# ---------------------------------------------------------------------------
# Layer 2 query: workspace-rooted table + impurity propagation
# ---------------------------------------------------------------------------


@query
def module_symbol_table_for_module(
    db: Database, root: str, path: str
) -> ModuleSymbolTablePayload:
    workspace_files = workspace_python_files(db, root)
    if path not in workspace_files:
        return ("", path, tuple(), ("not in workspace",))
    module_name = _module_name_for_path(root, path)
    _, _, symbols, impurity = module_symbol_table_payload(db, path)

    binding = module_binding_analysis_payload(db, path)
    merged_impurity = tuple(dict.fromkeys((*impurity, *binding[2])))

    for reason in merged_impurity:
        db.report_untracked_read(f"symbol_table({module_name}): {reason}")
    return (module_name, path, symbols, merged_impurity)


# ---------------------------------------------------------------------------
# Layer 2 query: cross-module resolution
# ---------------------------------------------------------------------------


_TERMINAL_SYMBOL_KINDS: frozenset[SymbolKind] = frozenset(
    {"function", "method", "class", "class_variable", "variable"}
)


def _find_symbol(
    table: ModuleSymbolTablePayload, qualified_name: str
) -> SymbolPayload | None:
    for symbol in table[2]:
        if symbol[0] == qualified_name:
            return symbol
    return None


def _find_wildcard_stubs(
    table: ModuleSymbolTablePayload,
) -> tuple[SymbolPayload, ...]:
    return tuple(symbol for symbol in table[2] if symbol[1] == "wildcard_import_stub")


def _terminal(
    original_module: str,
    qualified_name: str,
    resolution: SymbolResolutionKind,
    defining_module: str | None,
    defining_path: str | None,
    defining_lineno: int | None,
    dist_name: str | None,
    dist_ver: str | None,
    depth: int,
    trail: tuple[str, ...],
) -> ResolvedSymbolPayload:
    return (
        original_module,
        qualified_name,
        resolution,
        defining_module,
        defining_path,
        defining_lineno,
        dist_name,
        dist_ver,
        depth,
        trail,
    )


def _match_import(
    db: Database,
    root: str,
    current_path: str,
    source_module: str,
    source_name: str | None,
) -> tuple[SymbolResolutionKind, str | None, str | None, str | None] | None:
    resolved = resolved_imports_for_file(db, root, current_path)
    for (
        request_module,
        kind,
        _lineno,
        imported_name,
        _resolved_module,
        resolved_path,
        resolution,
        dist_name,
        dist_ver,
    ) in resolved:
        if request_module != source_module:
            continue
        if kind == "import":
            if source_name is None:
                return (resolution, resolved_path, dist_name, dist_ver)
            continue
        if imported_name != source_name:
            continue
        return (resolution, resolved_path, dist_name, dist_ver)
    return None


def _is_module_target(root: str, target_path: str, source_name: str) -> bool:
    target_module = _module_name_for_path(root, target_path)
    if target_module == source_name:
        return True
    return target_module.endswith("." + source_name)


def _resolve_via_wildcards(
    db: Database,
    root: str,
    current_path: str,
    qualified_name: str,
    table: ModuleSymbolTablePayload,
) -> tuple[str, str] | SymbolResolutionKind | None:
    """Try to resolve `qualified_name` through `from X import *` stubs.

    Returns:
        * ``(target_path, target_qname)`` — continue hopping to target file.
        * ``"ambiguous"`` — multi-provider match or dynamic-__all__ provider blocks lookup.
        * ``None`` — no wildcard stubs or no matching provider export.
    """
    stubs = _find_wildcard_stubs(table)
    if not stubs:
        return None

    resolved_imports = resolved_imports_for_file(db, root, current_path)
    matches: list[tuple[str, str]] = []
    any_dynamic_provider = False

    for stub in stubs:
        stub_module = stub[5]
        if stub_module is None:
            continue
        for (
            request_module,
            _kind,
            _lineno,
            imported_name,
            _resolved_module,
            resolved_path,
            resolution,
            _dist_name,
            _dist_ver,
        ) in resolved_imports:
            if request_module != stub_module or imported_name != "*":
                continue
            if resolution != "workspace" or resolved_path is None:
                continue
            _, _, provider_exports = module_wildcard_export_surface(
                db, root, resolved_path
            )
            _, _, _, provider_impurity = module_symbol_table_for_module(
                db, root, resolved_path
            )
            if "dynamic __all__" in provider_impurity:
                any_dynamic_provider = True
            if qualified_name in provider_exports:
                matches.append((resolved_path, qualified_name))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        db.report_untracked_read(
            f"wildcard lookup {qualified_name} resolves to multiple providers"
        )
        return "ambiguous"
    if any_dynamic_provider:
        db.report_untracked_read(
            f"wildcard lookup {qualified_name} blocked by dynamic __all__"
        )
        return "ambiguous"
    return None


@query
def resolve_symbol_payload(
    db: Database, root: str, path: str, qualified_name: str
) -> ResolvedSymbolPayload:
    workspace_files = workspace_python_files(db, root)
    if path not in workspace_files:
        return _terminal("", qualified_name, "missing", None, None, None, None, None, 0, tuple())

    original_module = _module_name_for_path(root, path)
    current_path = path
    current_qname = qualified_name
    visited: set[tuple[str, str]] = set()
    trail: list[str] = []
    depth = 0

    while True:
        current_module = _module_name_for_path(root, current_path)
        key = (current_module, current_qname)
        if key in visited:
            db.report_untracked_read(
                f"symbol resolution cycle at {current_module}:{current_qname}"
            )
            return _terminal(
                original_module, qualified_name, "ambiguous",
                None, None, None, None, None, depth, tuple(trail),
            )
        visited.add(key)
        trail.append(f"{current_module}:{current_qname}")

        if depth >= MAX_FOLLOW_DEPTH:
            return _terminal(
                original_module, qualified_name, "ambiguous",
                None, None, None, None, None, depth, tuple(trail),
            )

        table = module_symbol_table_for_module(db, root, current_path)
        symbol = _find_symbol(table, current_qname)

        if symbol is None:
            wildcard_outcome = _resolve_via_wildcards(
                db, root, current_path, current_qname, table
            )
            if wildcard_outcome is None:
                return _terminal(
                    original_module, qualified_name, "missing",
                    None, None, None, None, None, depth, tuple(trail),
                )
            if isinstance(wildcard_outcome, str):
                return _terminal(
                    original_module, qualified_name, wildcard_outcome,
                    None, None, None, None, None, depth, tuple(trail),
                )
            current_path, current_qname = wildcard_outcome
            depth += 1
            continue

        kind = symbol[1]
        lineno = symbol[2]

        if kind in _TERMINAL_SYMBOL_KINDS:
            return _terminal(
                original_module, qualified_name, "workspace",
                current_module, current_path, lineno, None, None, depth, tuple(trail),
            )

        if kind in ("import_alias", "from_import_alias"):
            source_module = symbol[5]
            source_name = symbol[6]
            if source_module is None:
                return _terminal(
                    original_module, qualified_name, "missing",
                    None, None, None, None, None, depth, tuple(trail),
                )
            match = _match_import(db, root, current_path, source_module, source_name)
            if match is None:
                return _terminal(
                    original_module, qualified_name, "missing",
                    None, None, None, None, None, depth, tuple(trail),
                )
            resolution, target_path, dist_name, dist_ver = match

            if resolution == "stdlib":
                return _terminal(
                    original_module, qualified_name, "stdlib",
                    None, None, None, None, None, depth, tuple(trail),
                )
            if resolution == "installed":
                return _terminal(
                    original_module, qualified_name, "installed",
                    None, None, None, dist_name, dist_ver, depth, tuple(trail),
                )
            if resolution in ("external", "ambiguous", "missing"):
                return _terminal(
                    original_module, qualified_name, resolution,
                    None, None, None, None, None, depth, tuple(trail),
                )
            # resolution == "workspace"
            if target_path is None:
                return _terminal(
                    original_module, qualified_name, "missing",
                    None, None, None, None, None, depth, tuple(trail),
                )

            if kind == "import_alias":
                target_module = _module_name_for_path(root, target_path)
                return _terminal(
                    original_module, qualified_name, "workspace",
                    target_module, target_path, None, None, None, depth, tuple(trail),
                )

            assert source_name is not None
            if _is_module_target(root, target_path, source_name):
                target_module = _module_name_for_path(root, target_path)
                return _terminal(
                    original_module, qualified_name, "workspace",
                    target_module, target_path, None, None, None, depth, tuple(trail),
                )
            current_path = target_path
            current_qname = source_name
            depth += 1
            continue

        return _terminal(
            original_module, qualified_name, "missing",
            None, None, None, None, None, depth, tuple(trail),
        )


# ---------------------------------------------------------------------------
# Layer 2 query: workspace-wide symbol index
# ---------------------------------------------------------------------------


@query
def workspace_symbol_index_payload(
    db: Database, root: str
) -> WorkspaceSymbolIndexPayload:
    files = workspace_python_files(db, root)
    entries: list[WorkspaceSymbolEntryPayload] = []
    for path in files:
        module, _, symbols, _ = module_symbol_table_for_module(db, root, path)
        if not module and not symbols:
            continue
        for qname, kind, lineno, annotation, _signature, _src_mod, _src_name in symbols:
            if kind == "wildcard_import_stub":
                continue
            entries.append((module, qname, kind, lineno, annotation))
    entries.sort(key=lambda item: (item[0], item[1]))
    return (root, tuple(entries))


# ---------------------------------------------------------------------------
# Layer 1 query: per-file name occurrences (full-AST walk)
# ---------------------------------------------------------------------------


def _collect_name_occurrences(tree: ast.Module) -> tuple[NameOccurrencePayload, ...]:
    occurrences: list[NameOccurrencePayload] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            end_col = node.end_col_offset
            if end_col is None:
                end_col = node.col_offset + len(node.id)
            occurrences.append((node.id, node.lineno, node.col_offset, end_col))
            continue
        if isinstance(node, ast.Attribute):
            end_col = node.end_col_offset
            end_lineno = node.end_lineno
            if end_col is None or end_lineno is None:
                continue
            attr_col = end_col - len(node.attr)
            if attr_col < 0:
                continue
            occurrences.append((node.attr, end_lineno, attr_col, end_col))
    occurrences.sort(key=lambda item: (item[1], item[2]))
    return tuple(occurrences)


@query
def name_occurrences_for_file(
    db: Database, path: str
) -> tuple[NameOccurrencePayload, ...]:
    source = source_text(db, path)
    tree = _try_parse(source)
    if tree is None:
        return tuple()
    return _collect_name_occurrences(tree)


# ---------------------------------------------------------------------------
# Layer 2 query: workspace-wide name occurrence index
# ---------------------------------------------------------------------------


@query
def workspace_name_occurrence_index(
    db: Database, root: str
) -> WorkspaceNameOccurrencesPayload:
    files = workspace_python_files(db, root)
    entries: list[FileNameOccurrencesPayload] = []
    for path in files:
        occurrences = name_occurrences_for_file(db, path)
        entries.append((path, tuple(occurrences)))
    return tuple(entries)


# ---------------------------------------------------------------------------
# Layer 2 query: workspace-wide reference index for a target symbol
# ---------------------------------------------------------------------------


@query
def find_references_payload(
    db: Database, root: str, path: str, qualified_name: str
) -> ReferenceQueryResultPayload:
    target_payload = resolve_symbol_payload(db, root, path, qualified_name)
    (
        _orig_module,
        _qname,
        resolution,
        defining_module,
        defining_path,
        defining_lineno,
        _dist_name,
        _dist_ver,
        _follow_depth,
        _trail,
    ) = target_payload

    if resolution != "workspace" or defining_path is None:
        return (target_payload, tuple())

    bare_target = qualified_name.rsplit(".", 1)[-1]
    references: list[ReferenceEntryPayload] = []
    declaration_seen = False

    occurrences_index = workspace_name_occurrence_index(db, root)
    for file_path, occurrences in occurrences_index:
        for bare_name, lineno, col_offset, end_col_offset in occurrences:
            if bare_name != bare_target:
                continue
            verify = resolve_symbol_payload(db, root, file_path, bare_name)
            v_resolution = verify[2]
            v_def_module = verify[3]
            v_def_path = verify[4]
            v_def_lineno = verify[5]
            if v_resolution != "workspace":
                continue
            if v_def_module != defining_module:
                continue
            if v_def_path != defining_path:
                continue
            if v_def_lineno != defining_lineno:
                continue
            is_declaration = (
                file_path == defining_path and lineno == defining_lineno
            )
            if is_declaration:
                declaration_seen = True
            references.append(
                (file_path, lineno, col_offset, end_col_offset, is_declaration)
            )

    if not declaration_seen and defining_lineno is not None:
        references.append((defining_path, defining_lineno, 0, 1, True))

    references.sort(key=lambda item: (item[0], item[1], item[2]))
    return (target_payload, tuple(references))


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------


def _decode_parameter(payload: ParameterPayload) -> Parameter:
    name, annotation = payload
    return Parameter(name=name, annotation=annotation)


def _decode_signature(payload: SignaturePayload | None) -> Signature | None:
    if payload is None:
        return None
    parameters, return_annotation = payload
    return Signature(
        parameters=tuple(_decode_parameter(item) for item in parameters),
        return_annotation=return_annotation,
    )


def _decode_symbol(payload: SymbolPayload) -> Symbol:
    (
        qualified_name,
        kind,
        lineno,
        annotation,
        signature,
        import_source_module,
        import_source_name,
    ) = payload
    return Symbol(
        qualified_name=qualified_name,
        kind=kind,
        lineno=lineno,
        annotation=annotation,
        signature=_decode_signature(signature),
        import_source_module=import_source_module,
        import_source_name=import_source_name,
    )


def _decode_module_symbol_table(payload: ModuleSymbolTablePayload) -> ModuleSymbolTable:
    module, path, symbols, impurity_reasons = payload
    return ModuleSymbolTable(
        module=module,
        path=path,
        symbols=tuple(_decode_symbol(item) for item in symbols),
        impurity_reasons=impurity_reasons,
    )


def _decode_resolved_symbol(payload: ResolvedSymbolPayload) -> ResolvedSymbol:
    (
        original_module,
        qualified_name,
        resolution,
        defining_module,
        defining_path,
        defining_lineno,
        distribution_name,
        distribution_version,
        follow_depth,
        trail,
    ) = payload
    return ResolvedSymbol(
        original_module=original_module,
        qualified_name=qualified_name,
        resolution=resolution,
        defining_module=defining_module,
        defining_path=defining_path,
        defining_lineno=defining_lineno,
        distribution_name=distribution_name,
        distribution_version=distribution_version,
        follow_depth=follow_depth,
        trail=trail,
    )


def _decode_workspace_symbol_entry(
    payload: WorkspaceSymbolEntryPayload,
) -> WorkspaceSymbolEntry:
    module, qualified_name, kind, lineno, annotation = payload
    return WorkspaceSymbolEntry(
        module=module,
        qualified_name=qualified_name,
        kind=kind,
        lineno=lineno,
        annotation=annotation,
    )


def _decode_workspace_symbol_index(
    payload: WorkspaceSymbolIndexPayload,
) -> WorkspaceSymbolIndex:
    root, entries = payload
    return WorkspaceSymbolIndex(
        root=root,
        entries=tuple(_decode_workspace_symbol_entry(item) for item in entries),
    )


def _decode_reference(payload: ReferenceEntryPayload) -> Reference:
    path, lineno, col_offset, end_col_offset, is_declaration = payload
    return Reference(
        path=path,
        lineno=lineno,
        col_offset=col_offset,
        end_col_offset=end_col_offset,
        is_declaration=is_declaration,
    )


# ---------------------------------------------------------------------------
# Layer 3 entrypoints
# ---------------------------------------------------------------------------


def module_symbol_table(
    db: Database,
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
) -> ModuleSymbolTable:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    payload = cast(
        ModuleSymbolTablePayload,
        thaw(db.get(module_symbol_table_for_module, normalized_root, normalized_path)),
    )
    return _decode_module_symbol_table(payload)


def resolve_symbol(
    db: Database,
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    qualified_name: str,
) -> ResolvedSymbol:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    payload = cast(
        ResolvedSymbolPayload,
        thaw(
            db.get(
                resolve_symbol_payload,
                normalized_root,
                normalized_path,
                qualified_name,
            )
        ),
    )
    return _decode_resolved_symbol(payload)


def workspace_symbol_index(
    db: Database, root: str | os.PathLike[str]
) -> WorkspaceSymbolIndex:
    normalized_root = _normalize_path(root)
    payload = cast(
        WorkspaceSymbolIndexPayload,
        thaw(db.get(workspace_symbol_index_payload, normalized_root)),
    )
    return _decode_workspace_symbol_index(payload)


def find_references(
    db: Database,
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    qualified_name: str,
    *,
    include_declaration: bool = True,
) -> ReferenceQueryResult:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    payload = cast(
        ReferenceQueryResultPayload,
        thaw(
            db.get(
                find_references_payload,
                normalized_root,
                normalized_path,
                qualified_name,
            )
        ),
    )
    target_payload, references_payload = payload
    target = _decode_resolved_symbol(target_payload)
    decoded = tuple(_decode_reference(item) for item in references_payload)
    if not include_declaration:
        decoded = tuple(item for item in decoded if not item.is_declaration)
    return ReferenceQueryResult(target=target, references=decoded)


__all__ = [
    "ModuleSymbolTable",
    "Parameter",
    "Reference",
    "ReferenceQueryResult",
    "ResolvedSymbol",
    "Signature",
    "Symbol",
    "WorkspaceSymbolEntry",
    "WorkspaceSymbolIndex",
    "find_references",
    "module_symbol_table",
    "resolve_symbol",
    "workspace_symbol_index",
]
