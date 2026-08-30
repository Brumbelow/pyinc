from __future__ import annotations

import ast
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypeAlias

from pyinc.core import query
from pyinc.integrations.python_source import (
    module_binding_analysis_payload,
    module_wildcard_export_surface,
    resolved_imports_for_file,
    source_text,
    workspace_python_files,
)
from pyinc.integrations.scope_resolution import ScopeTree, SymbolId, scope_tree, symbol_at
from pyinc.integrations.source_geometry import DocumentMap, SourcePosition, SourceRange
from pyinc.runtime import Database

from ._decoding import _reject_in_query, decoded, once_per_request

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

_ResolvedSymbolPayload: TypeAlias = tuple[
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

ClassMemberKind: TypeAlias = Literal["method", "class_variable", "instance_variable"]

ClassMemberPayload: TypeAlias = tuple[
    str,
    ClassMemberKind,
    int,
    str | None,
    SignaturePayload | None,
]
#   name, kind, lineno, annotation_text, signature

EncodedBasePayload: TypeAlias = tuple[str, ...]
#   ("name", id) | ("attr", lhs_id, attr) | ("text", raw)

OwnClassModelPayload: TypeAlias = tuple[
    str,
    tuple[EncodedBasePayload, ...],
    tuple[ClassMemberPayload, ...],
]
#   qualified_name, bases, members

ResolvedClassMemberPayload: TypeAlias = tuple[
    str,
    ClassMemberKind,
    int,
    str | None,
    SignaturePayload | None,
    str | None,
    str | None,
]
#   name, kind, lineno, annotation_text, signature, defining_path, defining_class

ResolvedClassModelPayload: TypeAlias = tuple[
    str,
    str,
    tuple[ResolvedClassMemberPayload, ...],
    tuple[str, ...],
    tuple[str, ...],
]
#   path, qualified_name, members, unresolved_bases, truncated_bases

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
    range: SourceRange
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
class _ResolvedSymbol:
    original_module: str
    qualified_name: str
    resolution: SymbolResolutionKind
    defining_module: str | None
    defining_path: str | None
    range: SourceRange | None
    distribution_name: str | None
    distribution_version: str | None
    follow_depth: int
    trail: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceSymbolEntry:
    module: str
    qualified_name: str
    kind: SymbolKind
    range: SourceRange
    annotation: str | None


@dataclass(frozen=True)
class WorkspaceSymbolIndex:
    root: str
    entries: tuple[WorkspaceSymbolEntry, ...]


@dataclass(frozen=True)
class Reference:
    path: str
    range: SourceRange
    is_declaration: bool


@dataclass(frozen=True)
class ReferenceQueryResult:
    target: SymbolId
    references: tuple[Reference, ...]


@dataclass(frozen=True)
class ClassMember:
    name: str
    kind: ClassMemberKind
    range: SourceRange
    annotation: str | None
    signature: Signature | None
    defining_path: str | None
    defining_class: str | None


@dataclass(frozen=True)
class ClassModel:
    path: str
    qualified_name: str
    members: tuple[ClassMember, ...]
    unresolved_bases: tuple[str, ...]
    truncated_bases: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

MAX_FOLLOW_DEPTH = 8

# Depth bound for base-class following in `resolved_class_model_payload`.
# Mirrors `MAX_FOLLOW_DEPTH`'s trail/cap idiom: the starting class sits at depth
# 0, so classes at depths 0..MAX_BASE_DEPTH-1 contribute members and the class
# reached at depth MAX_BASE_DEPTH (and beyond) is not walked. A base stopped by
# the cap is named in `ClassModel.truncated_bases`, so the loss is observable.
MAX_BASE_DEPTH = 8

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
        params.append((f"*{args.vararg.arg}", _annotation_text(args.vararg.annotation)))
    for arg in args.kwonlyargs:
        params.append((arg.arg, _annotation_text(arg.annotation)))
    if args.kwarg is not None:
        params.append((f"**{args.kwarg.arg}", _annotation_text(args.kwarg.annotation)))
    return tuple(params)


def _signature_payload(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> SignaturePayload:
    return (
        _parameter_payloads_from_args(node.args),
        _annotation_text(node.returns),
    )


_AST_TYPE_ALIAS = getattr(ast, "TypeAlias", None)


def _is_type_alias(node: ast.AST) -> bool:
    return isinstance(_AST_TYPE_ALIAS, type) and isinstance(node, _AST_TYPE_ALIAS)


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


def _class_body_walk(cls: ast.ClassDef, qualifier: str, out: list[SymbolPayload]) -> None:
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
        if _is_type_alias(node):
            alias_name = getattr(node, "name", None)
            if isinstance(alias_name, ast.Name):
                out.append(
                    (
                        f"{qualifier}.{alias_name.id}",
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


def _type_checking_block_walk(body: list[ast.stmt], symbols: list[SymbolPayload]) -> None:
    """Collect import symbols from a block — used for TYPE_CHECKING and try/except ImportError."""
    for stmt in body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = _bound_name_for_import(alias)
                symbols.append((bound, "import_alias", stmt.lineno, None, None, alias.name, None))
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
        if _is_type_alias(node):
            alias_name = getattr(node, "name", None)
            if isinstance(alias_name, ast.Name):
                symbols.append(
                    (
                        alias_name.id,
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
        if isinstance(node, ast.Try) and _has_import_error_handler(node.handlers):
            _type_checking_block_walk(node.body, symbols)
            continue
        _record_reason("conditional top-level binding")

    return tuple(symbols), tuple(impurity_reasons)


# ---------------------------------------------------------------------------
# Layer 1 query: per-file symbol table
# ---------------------------------------------------------------------------


@query
def module_symbol_table_payload(db: Database, path: str) -> ModuleSymbolTablePayload:
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
def module_symbol_table_for_module(db: Database, root: str, path: str) -> ModuleSymbolTablePayload:
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


def _find_symbol(table: ModuleSymbolTablePayload, qualified_name: str) -> SymbolPayload | None:
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
) -> _ResolvedSymbolPayload:
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
            _, _, provider_exports = module_wildcard_export_surface(db, root, resolved_path)
            _, _, _, provider_impurity = module_symbol_table_for_module(db, root, resolved_path)
            if "dynamic __all__" in provider_impurity:
                any_dynamic_provider = True
            if qualified_name in provider_exports:
                matches.append((resolved_path, qualified_name))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        db.report_untracked_read(f"wildcard lookup {qualified_name} resolves to multiple providers")
        return "ambiguous"
    if any_dynamic_provider:
        db.report_untracked_read(f"wildcard lookup {qualified_name} blocked by dynamic __all__")
        return "ambiguous"
    return None


@query
def _resolve_symbol_payload(
    db: Database, root: str, path: str, qualified_name: str
) -> _ResolvedSymbolPayload:
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
            db.report_untracked_read(f"symbol resolution cycle at {current_module}:{current_qname}")
            return _terminal(
                original_module,
                qualified_name,
                "ambiguous",
                None,
                None,
                None,
                None,
                None,
                depth,
                tuple(trail),
            )
        visited.add(key)
        trail.append(f"{current_module}:{current_qname}")

        if depth >= MAX_FOLLOW_DEPTH:
            return _terminal(
                original_module,
                qualified_name,
                "ambiguous",
                None,
                None,
                None,
                None,
                None,
                depth,
                tuple(trail),
            )

        table = module_symbol_table_for_module(db, root, current_path)
        symbol = _find_symbol(table, current_qname)

        if symbol is None:
            wildcard_outcome = _resolve_via_wildcards(db, root, current_path, current_qname, table)
            if wildcard_outcome is None:
                return _terminal(
                    original_module,
                    qualified_name,
                    "missing",
                    None,
                    None,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )
            if isinstance(wildcard_outcome, str):
                return _terminal(
                    original_module,
                    qualified_name,
                    wildcard_outcome,
                    None,
                    None,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )
            current_path, current_qname = wildcard_outcome
            depth += 1
            continue

        kind = symbol[1]
        lineno = symbol[2]

        if kind in _TERMINAL_SYMBOL_KINDS:
            return _terminal(
                original_module,
                qualified_name,
                "workspace",
                current_module,
                current_path,
                lineno,
                None,
                None,
                depth,
                tuple(trail),
            )

        if kind in ("import_alias", "from_import_alias"):
            source_module = symbol[5]
            source_name = symbol[6]
            if source_module is None:
                return _terminal(
                    original_module,
                    qualified_name,
                    "missing",
                    None,
                    None,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )
            match = _match_import(db, root, current_path, source_module, source_name)
            if match is None:
                return _terminal(
                    original_module,
                    qualified_name,
                    "missing",
                    None,
                    None,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )
            resolution, target_path, dist_name, dist_ver = match

            if resolution == "stdlib":
                return _terminal(
                    original_module,
                    qualified_name,
                    "stdlib",
                    None,
                    None,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )
            if resolution == "installed":
                return _terminal(
                    original_module,
                    qualified_name,
                    "installed",
                    None,
                    None,
                    None,
                    dist_name,
                    dist_ver,
                    depth,
                    tuple(trail),
                )
            if resolution in ("external", "ambiguous", "missing"):
                return _terminal(
                    original_module,
                    qualified_name,
                    resolution,
                    None,
                    None,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )
            # resolution == "workspace"
            if target_path is None:
                return _terminal(
                    original_module,
                    qualified_name,
                    "missing",
                    None,
                    None,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )

            if kind == "import_alias":
                target_module = _module_name_for_path(root, target_path)
                return _terminal(
                    original_module,
                    qualified_name,
                    "workspace",
                    target_module,
                    target_path,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )

            assert source_name is not None
            if _is_module_target(root, target_path, source_name):
                target_module = _module_name_for_path(root, target_path)
                return _terminal(
                    original_module,
                    qualified_name,
                    "workspace",
                    target_module,
                    target_path,
                    None,
                    None,
                    None,
                    depth,
                    tuple(trail),
                )
            current_path = target_path
            current_qname = source_name
            depth += 1
            continue

        return _terminal(
            original_module,
            qualified_name,
            "missing",
            None,
            None,
            None,
            None,
            None,
            depth,
            tuple(trail),
        )


# ---------------------------------------------------------------------------
# Layer 2 query: workspace-wide symbol index
# ---------------------------------------------------------------------------


@query
def workspace_symbol_index_payload(db: Database, root: str) -> WorkspaceSymbolIndexPayload:
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


def _payload_range(lineno: int, width: int = 0) -> SourceRange:
    start = SourcePosition(max(lineno - 1, 0), 0)
    return SourceRange(start, SourcePosition(start.line, width))


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
        range=_payload_range(lineno),
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


def _decode_resolved_symbol(payload: _ResolvedSymbolPayload) -> _ResolvedSymbol:
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
    return _ResolvedSymbol(
        original_module=original_module,
        qualified_name=qualified_name,
        resolution=resolution,
        defining_module=defining_module,
        defining_path=defining_path,
        range=None,
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
        range=_payload_range(lineno),
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


def _source_ranges_for_path(db: Database, path: str) -> dict[tuple[str, int], SourceRange]:
    return once_per_request(
        db, "source_ranges_for_path", (path,), lambda: _ranges_for_path(db, path)
    )


def _ranges_for_path(db: Database, path: str) -> dict[tuple[str, int], SourceRange]:
    lexical = scope_tree(db, path)
    source = source_text(db, path)
    return decoded(
        db,
        "source_ranges_for_path",
        (lexical, source),
        lambda: _build_source_ranges(lexical, source),
    )


def _build_source_ranges(
    lexical: ScopeTree, source: str
) -> dict[tuple[str, int], SourceRange]:
    """Exact source spans for a file's bindings, keyed by name and line.

    The result is memoized and handed to every caller, so it is read-only by
    contract: the three call sites only look names up in it.
    """
    ranges = {
        (binding.name, binding.range.start.line): binding.range
        for binding in lexical.bindings
    }
    tree = _try_parse(source)
    if tree is None:
        return ranges
    document = DocumentMap(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name == "*":
                ranges[("*", node.lineno - 1)] = document.ast_range(alias)
    return ranges


# ---------------------------------------------------------------------------
# Layer 3 entrypoints
# ---------------------------------------------------------------------------

# Every payload below is declared as nested tuples of primitives, and `freeze`
# leaves such a value as plain tuples, so what `db.get` hands back in any mode is
# already the payload. Thawing it again only walks and copies the whole tree --
# on a workspace-sized request that copy dominated the cost of decoding.


def module_symbol_table(
    db: Database,
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
) -> ModuleSymbolTable:
    _reject_in_query(db, "module_symbol_table")
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    return once_per_request(
        db,
        "module_symbol_table",
        (normalized_root, normalized_path),
        lambda: _module_symbol_table(db, normalized_root, normalized_path),
    )


def _module_symbol_table(
    db: Database, normalized_root: str, normalized_path: str
) -> ModuleSymbolTable:
    payload = db.get(module_symbol_table_for_module, normalized_root, normalized_path)
    ranges_by_name_and_line = _source_ranges_for_path(db, normalized_path)
    return decoded(
        db,
        "module_symbol_table",
        (payload, ranges_by_name_and_line),
        lambda: _placed_module_symbol_table(payload, ranges_by_name_and_line),
    )


def _placed_module_symbol_table(
    payload: ModuleSymbolTablePayload,
    ranges_by_name_and_line: dict[tuple[str, int], SourceRange],
) -> ModuleSymbolTable:
    table = _decode_module_symbol_table(payload)
    symbols = tuple(
        replace(
            symbol,
            range=ranges_by_name_and_line.get(
                (
                    symbol.qualified_name.rsplit(".", 1)[-1],
                    symbol.range.start.line,
                ),
                symbol.range,
            ),
        )
        for symbol in table.symbols
    )
    return replace(table, symbols=symbols)


def resolve_qualified_name(
    db: Database,
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    qualified_name: str,
) -> _ResolvedSymbol:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    return once_per_request(
        db,
        "resolve_qualified_name",
        (normalized_root, normalized_path, qualified_name),
        lambda: _resolve_qualified_name(db, normalized_root, normalized_path, qualified_name),
    )


def _resolve_qualified_name(
    db: Database, normalized_root: str, normalized_path: str, qualified_name: str
) -> _ResolvedSymbol:
    payload = db.get(
        _resolve_symbol_payload,
        normalized_root,
        normalized_path,
        qualified_name,
    )
    decoded = _decode_resolved_symbol(payload)
    defining_lineno = payload[5]
    if decoded.defining_path is None or defining_lineno is None:
        return decoded
    name = decoded.qualified_name.rsplit(".", 1)[-1]
    lexical = scope_tree(db, decoded.defining_path)
    source_range = next(
        (
            binding.range
            for binding in lexical.bindings
            if binding.name == name and binding.range.start.line == defining_lineno - 1
        ),
        None,
    )
    return replace(decoded, range=source_range)


def workspace_symbol_index(db: Database, root: str | os.PathLike[str]) -> WorkspaceSymbolIndex:
    _reject_in_query(db, "workspace_symbol_index")
    normalized_root = _normalize_path(root)
    payload = db.get(workspace_symbol_index_payload, normalized_root)
    decoded = _decode_workspace_symbol_index(payload)
    module_paths = {
        _module_name_for_path(normalized_root, path): path
        for path in workspace_python_files(db, normalized_root)
    }
    ranges_by_path: dict[str, dict[tuple[str, int], SourceRange]] = {}
    entries: list[WorkspaceSymbolEntry] = []
    for entry in decoded.entries:
        path = module_paths.get(entry.module)
        source_range = entry.range
        if path is not None:
            ranges = ranges_by_path.get(path)
            if ranges is None:
                ranges = _source_ranges_for_path(db, path)
                ranges_by_path[path] = ranges
            source_range = ranges.get(
                (
                    entry.qualified_name.rsplit(".", 1)[-1],
                    entry.range.start.line,
                ),
                source_range,
            )
        entries.append(replace(entry, range=source_range))
    return replace(decoded, entries=tuple(entries))


def find_references(
    db: Database,
    root: str | os.PathLike[str],
    symbol_id: SymbolId,
    *,
    include_declaration: bool = True,
) -> ReferenceQueryResult:
    _reject_in_query(db, "find_references")
    if not isinstance(symbol_id, SymbolId):
        raise TypeError("find_references() requires a resolved SymbolId")
    return _find_references_for_symbol(
        db,
        os.fspath(root),
        symbol_id,
        include_declaration=include_declaration,
    )


def _find_references_for_symbol(
    db: Database,
    root: str,
    target: SymbolId,
    *,
    include_declaration: bool,
) -> ReferenceQueryResult:
    """Find references by resolved lexical identity.

    Local bindings are compared directly. Module attributes and direct
    ``from`` imports are normalized through the conservative workspace
    resolver before comparison, so an unrelated shadow with the same spelling
    is never included.
    """

    files = db.get(workspace_python_files, root)
    references: list[Reference] = []
    seen_locations: set[tuple[str, SourceRange]] = set()
    for path in files:
        lexical = scope_tree(db, path)
        for occurrence in lexical.occurrences:
            if occurrence.name != target.name:
                continue
            candidate = symbol_at(db, root, path, occurrence.range.start)
            if candidate != target:
                continue
            if occurrence.is_declaration and occurrence.symbol_id != target:
                # An import binding names the target but is not itself a
                # declaration or usage of the target symbol.
                continue
            is_declaration = (
                occurrence.is_declaration
                and occurrence.symbol_id == target
                and occurrence.range == target.declaration
            )
            if is_declaration and not include_declaration:
                continue
            location = (path, occurrence.range)
            if location in seen_locations:
                continue
            seen_locations.add(location)
            references.append(
                Reference(
                    path=path,
                    range=occurrence.range,
                    is_declaration=is_declaration,
                )
            )
    references.sort(key=lambda item: (item.path, item.range.start))
    return ReferenceQueryResult(target=target, references=tuple(references))


# ---------------------------------------------------------------------------
# Class model (own members) — declaration-only, caret-free
# ---------------------------------------------------------------------------


def _first_param_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The name of a callable's first positional parameter, or ``None``.

    Positional-only parameters take precedence, then regular positionals; a
    callable that only takes ``*args`` / keyword parameters has no first
    positional and returns ``None``.
    """
    args = node.args
    if args.posonlyargs:
        return args.posonlyargs[0].arg
    if args.args:
        return args.args[0].arg
    return None


def _encode_base(node: ast.expr) -> EncodedBasePayload:
    """Encode a ``ClassDef`` base expression for later (Stage 3) resolution.

    ``("name", id)`` for a bare ``Name``, ``("attr", lhs_id, attr)`` for
    ``Name.attr``. A single ``Subscript`` layer is unwrapped so ``Base[T]``
    resolves through its ``value`` (``Base``). ``Starred`` bases, deep
    attribute chains, and call expressions fall back to
    ``("text", ast.unparse(node))`` carrying the raw source of the whole
    original base expression.
    """
    inner: ast.expr = node
    if isinstance(inner, ast.Subscript):
        inner = inner.value
    if isinstance(inner, ast.Name):
        return ("name", inner.id)
    if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
        return ("attr", inner.value.id, inner.attr)
    return ("text", ast.unparse(node))


def _base_text(encoded: EncodedBasePayload) -> str:
    """Flatten an :data:`EncodedBasePayload` to its textual base name."""
    if encoded[0] == "attr":
        return f"{encoded[1]}.{encoded[2]}"
    return encoded[1]


def _self_attribute_names(target: ast.expr) -> tuple[str, ...]:
    """Attribute names bound by assigning to a ``self.NAME`` target.

    Handles tuple / list / starred unpacking (``self.a, *self.rest = ...``)
    recursively. Only ``Attribute`` targets whose value is the bare ``Name``
    ``self`` contribute; anything else yields nothing.
    """
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        return (target.attr,)
    if isinstance(target, ast.Starred):
        return _self_attribute_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_self_attribute_names(elt))
        return tuple(names)
    return tuple()


def _collect_instance_attributes(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, int, str | None]]:
    """Every ``self.NAME`` assignment in *method*'s body as ``(name, lineno,
    annotation_text)``.

    Descent stops at nested ``FunctionDef`` / ``AsyncFunctionDef`` /
    ``ClassDef`` / ``Lambda`` scopes — a ``self.x`` inside a closure belongs to
    that closure's binding of ``self``, not the enclosing method's. ``AugAssign``
    (``self.x += 1``) is excluded because it presumes a pre-existing attribute
    rather than establishing one.
    """
    collected: list[tuple[str, int, str | None]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _self_attribute_names(target):
                    collected.append((name, node.lineno, None))
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                collected.append((target.attr, node.lineno, _annotation_text(node.annotation)))
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
            ):
                continue
            visit(child)

    for stmt in method.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        visit(stmt)
    return collected


def _class_member_walk(cls: ast.ClassDef) -> tuple[ClassMemberPayload, ...]:
    """Own members of a single ``ClassDef`` in deterministic, deduped order.

    Priority (first binding of a name wins): annotated class-body variables,
    assigned class-body variables, methods, then instance attributes collected
    from every direct method whose first parameter is literally ``self``
    (lowest lineno kept when an attribute is bound in more than one place).
    Nested ``ClassDef`` members belong to that class's own model and are not
    reported here.
    """
    members: list[ClassMemberPayload] = []
    seen: set[str] = set()

    def add(
        name: str,
        kind: ClassMemberKind,
        lineno: int,
        annotation: str | None,
        signature: SignaturePayload | None,
    ) -> None:
        if name in seen:
            return
        seen.add(name)
        members.append((name, kind, lineno, annotation, signature))

    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            add(
                node.target.id,
                "class_variable",
                node.lineno,
                _annotation_text(node.annotation),
                None,
            )
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _target_bound_names(target):
                    if name == "__all__":
                        continue
                    add(name, "class_variable", node.lineno, None, None)
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, "method", node.lineno, None, _signature_payload(node))

    instance: dict[str, tuple[int, str | None]] = {}
    for node in cls.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _first_param_name(node) == "self"
        ):
            for name, lineno, annotation in _collect_instance_attributes(node):
                existing = instance.get(name)
                if existing is None or lineno < existing[0]:
                    instance[name] = (lineno, annotation)
    for name in sorted(instance, key=lambda item: (instance[item][0], item)):
        lineno, annotation = instance[name]
        add(name, "instance_variable", lineno, annotation, None)

    return tuple(members)


def _walk_class_defs(tree: ast.Module) -> tuple[tuple[str, ast.ClassDef], ...]:
    """Every ``ClassDef`` in *tree* paired with its dotted qualifier.

    The qualifier follows ``module_symbol_table``'s scheme: only ``ClassDef``
    nesting extends the dotted path (``Outer.Inner``); a class declared inside
    a function body re-enters at its bare name.
    """
    out: list[tuple[str, ast.ClassDef]] = []

    def walk(node: ast.AST, qualifier: str) -> None:
        if isinstance(node, ast.ClassDef):
            qname = f"{qualifier}.{node.name}" if qualifier else node.name
            out.append((qname, node))
            for body_child in node.body:
                walk(body_child, qname)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for descendant in ast.iter_child_nodes(node):
                walk(descendant, "")
            return
        for descendant in ast.iter_child_nodes(node):
            walk(descendant, qualifier)

    walk(tree, "")
    return tuple(out)


@query
def class_models_for_file(db: Database, path: str) -> tuple[OwnClassModelPayload, ...]:
    source = source_text(db, path)
    tree = _try_parse(source)
    if tree is None:
        return tuple()
    out: list[OwnClassModelPayload] = []
    for qname, cls in _walk_class_defs(tree):
        bases = tuple(_encode_base(base) for base in cls.bases)
        members = _class_member_walk(cls)
        out.append((qname, bases, members))
    return tuple(out)


def _class_site_from_resolved(
    db: Database, root: str, resolved: _ResolvedSymbolPayload
) -> tuple[str, str] | None:
    """The ``(defining_path, class_qname)`` a resolved symbol lands on when it
    points at a workspace ``class`` — else ``None``.

    The resolver returns the *original* requested name, which may be an import
    alias; ``defining_lineno`` instead pins the class in its own module table,
    which both confirms the target is a class and recovers its qualified name in
    that file (nested classes included, e.g. ``Outer.Inner``)."""
    if resolved[2] != "workspace":
        return None
    defining_path = resolved[4]
    defining_lineno = resolved[5]
    if defining_path is None or defining_lineno is None:
        return None
    table = module_symbol_table_for_module(db, root, defining_path)
    for sym_qname, kind, lineno, *_rest in table[2]:
        if lineno == defining_lineno and kind == "class":
            return (defining_path, sym_qname)
    return None


def _resolve_base_to_class(
    db: Database, root: str, path: str, encoded: EncodedBasePayload
) -> tuple[str, str] | None:
    """Resolve one encoded base to the ``(path, class_qname)`` of a workspace
    class, in ``path``'s module context.

    ``("name", X)`` resolves ``X`` through the file's imports (same-file class
    qnames live in the module table, so bare local bases resolve too).
    ``("attr", L, A)`` first tries the whole dotted name as a same-module class
    (``Outer.Inner``), then falls back to resolving ``L`` to a workspace module
    and ``A`` inside it (the ``_resolve_attr_on_module`` idiom). ``("text", …)``
    bases, and anything not landing on a workspace class (stdlib / installed /
    missing / ambiguous / non-class), return ``None`` — the caller records them
    in ``unresolved_bases``.
    """
    tag = encoded[0]
    if tag == "name":
        return _class_site_from_resolved(
            db, root, _resolve_symbol_payload(db, root, path, encoded[1])
        )
    if tag == "attr":
        lhs, attr = encoded[1], encoded[2]
        direct = _resolve_symbol_payload(db, root, path, f"{lhs}.{attr}")
        site = _class_site_from_resolved(db, root, direct)
        if site is not None:
            return site
        lhs_resolved = _resolve_symbol_payload(db, root, path, lhs)
        lhs_path = lhs_resolved[4]
        # `defining_lineno is None` means the LHS is a module, not a symbol.
        if lhs_resolved[2] == "workspace" and lhs_resolved[5] is None and lhs_path:
            return _class_site_from_resolved(
                db, root, _resolve_symbol_payload(db, root, lhs_path, attr)
            )
    return None


@query
def resolved_class_model_payload(
    db: Database, root: str, path: str, qualified_name: str
) -> ResolvedClassModelPayload:
    workspace_files = workspace_python_files(db, root)
    if path not in workspace_files:
        return (path, qualified_name, tuple(), tuple(), tuple())

    # Flatten the inheritance graph: DEPTH-FIRST, LEFT-TO-RIGHT,
    # SHALLOWEST-DEFINITION-WINS by member name (derived shadows base), ties at
    # equal depth going to the earlier arrival. This is not C3 MRO. Cycles are
    # cut by a `(path, class_qname)` key, and the walk is bounded by
    # `MAX_BASE_DEPTH`. Base files are queried one at a time via
    # `class_models_for_file`, so an edit to one base invalidates per file.
    #
    # `reached` holds the SHALLOWEST depth each key was walked at, not mere
    # membership: a class first met near the cap has its own bases cut short, so
    # a strictly-shallower reach has to walk it again with the larger remaining
    # budget — otherwise which members survive depends on traversal order. That
    # depth strictly decreases per revisit, so a key is walked at most
    # `MAX_BASE_DEPTH` times and a wide diamond cannot revisit exponentially.
    #
    # `member_depth` makes the winning DEFINITION depth-canonical the same way:
    # a revisit can splice a previously-cut ancestor into the walk ahead of a
    # sibling subtree that overrides it, so arrival order alone would let a base
    # claim a name over a nearer override. Claims are therefore replaced when a
    # class reached strictly shallower defines the same name.
    members: dict[str, ResolvedClassMemberPayload] = {}
    member_depth: dict[str, int] = {}
    unresolved: list[str] = []
    seen_unresolved: set[str] = set()
    reached: dict[tuple[str, str], int] = {}
    # One resolution per base expression, shared across a key's revisits.
    base_sites: dict[tuple[str, EncodedBasePayload], tuple[str, str] | None] = {}
    # Bases that resolved to a workspace class the cap stopped us from walking,
    # in first-encounter order and paired with the site so a later shallower
    # reach can retract the report.
    cut: list[tuple[tuple[str, str], str]] = []
    seen_cut: set[tuple[tuple[str, str], str]] = set()

    def visit(cur_path: str, cur_qname: str, depth: int) -> None:
        key = (cur_path, cur_qname)
        best = reached.get(key)
        if best is not None and best <= depth:
            return
        reached[key] = depth
        own: tuple[tuple[EncodedBasePayload, ...], tuple[ClassMemberPayload, ...]] | None
        own = None
        for model_qname, bases, own_members in class_models_for_file(db, cur_path):
            if model_qname == cur_qname:
                own = (bases, own_members)
                break
        if own is None:
            return
        cur_bases, cur_members = own
        for name, kind, lineno, annotation, signature in cur_members:
            claimed = member_depth.get(name)
            if claimed is not None and claimed <= depth:
                continue
            member_depth[name] = depth
            members[name] = (
                name,
                kind,
                lineno,
                annotation,
                signature,
                cur_path,
                cur_qname,
            )
        for base in cur_bases:
            site_key = (cur_path, base)
            if site_key not in base_sites:
                base_sites[site_key] = _resolve_base_to_class(db, root, cur_path, base)
            site = base_sites[site_key]
            if site is None:
                text = _base_text(base)
                if text not in seen_unresolved:
                    seen_unresolved.add(text)
                    unresolved.append(text)
                continue
            if depth + 1 >= MAX_BASE_DEPTH:
                entry = (site, _base_text(base))
                if entry not in seen_cut:
                    seen_cut.add(entry)
                    cut.append(entry)
                continue
            visit(site[0], site[1], depth + 1)

    visit(path, qualified_name, 0)
    # A site the cap stopped is only truly lost if no other reach walked it.
    truncated: list[str] = []
    seen_truncated: set[str] = set()
    for site, text in cut:
        if site in reached or text in seen_truncated:
            continue
        seen_truncated.add(text)
        truncated.append(text)
    return (
        path,
        qualified_name,
        tuple(members.values()),
        tuple(unresolved),
        tuple(truncated),
    )


def _decode_class_member(payload: ResolvedClassMemberPayload) -> ClassMember:
    (
        name,
        kind,
        lineno,
        annotation,
        signature,
        defining_path,
        defining_class,
    ) = payload
    return ClassMember(
        name=name,
        kind=kind,
        range=_payload_range(lineno),
        annotation=annotation,
        signature=_decode_signature(signature),
        defining_path=defining_path,
        defining_class=defining_class,
    )


def _decode_class_model(payload: ResolvedClassModelPayload) -> ClassModel:
    path, qualified_name, members, unresolved_bases, truncated_bases = payload
    return ClassModel(
        path=path,
        qualified_name=qualified_name,
        members=tuple(_decode_class_member(item) for item in members),
        unresolved_bases=unresolved_bases,
        truncated_bases=truncated_bases,
    )


def class_model(
    db: Database,
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    qualified_name: str,
) -> ClassModel:
    _reject_in_query(db, "class_model")
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    payload = db.get(
        resolved_class_model_payload,
        normalized_root,
        normalized_path,
        qualified_name,
    )
    decoded = _decode_class_model(payload)
    ranges_by_path: dict[str, dict[tuple[str, int], SourceRange]] = {}
    members: list[ClassMember] = []
    for member in decoded.members:
        source_range = member.range
        if member.defining_path is not None:
            ranges = ranges_by_path.get(member.defining_path)
            if ranges is None:
                ranges = _source_ranges_for_path(db, member.defining_path)
                ranges_by_path[member.defining_path] = ranges
            source_range = ranges.get((member.name, member.range.start.line), source_range)
        members.append(replace(member, range=source_range))
    return replace(decoded, members=tuple(members))


__all__ = [
    "ClassMember",
    "ClassModel",
    "ModuleSymbolTable",
    "Parameter",
    "Reference",
    "ReferenceQueryResult",
    "Signature",
    "Symbol",
    "WorkspaceSymbolEntry",
    "WorkspaceSymbolIndex",
    "class_model",
    "find_references",
    "module_symbol_table",
    "workspace_symbol_index",
]
