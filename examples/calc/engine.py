"""The ``calc`` engine: parser + incremental query graph + output action.

Grammar (line-oriented; nothing beyond this):

    # comment
    include "constants.calc"
    let alpha = beta + 2
    let beta = 40
    emit alpha

Semantics:

- Bindings resolve by *name* across the whole file (forward references allowed).
- Operators: integer ``+`` and ``-`` only.
- ``include`` reads another ``.calc`` file through the single shared
  ``FileResource`` (the path is the resource node); only referenced includes are
  resolved.
- A binding cycle (``let a = b`` / ``let b = a``) yields a deterministic
  diagnostic — detected structurally before any cross-query recursion, so the
  kernel's ``CycleError`` is never relied upon.

Query graph (each layer is a kernel-cached node):

    calc_source (cutoff=semantic token)  -> raw text; comment/whitespace edits backdate
    parse_calc                           -> (includes, bindings, emits, diagnostics)
    binding_table                        -> merged name->expr over referenced includes
    binding_expr                         -> one name's expression (backdates when unchanged)
    binding_cycles                       -> names participating in a reference cycle
    evaluate_name                        -> value or diagnostic (per-name, cross-query recursion)
    emit_names                           -> the root file's ``emit`` declarations
    calc_emit  (@action)                 -> one ``<name>.out`` per emit

Regexes are inlined as string literals (not module-level ``re.Pattern``
singletons) because the kernel walks query-reachable functions' captures and
rejects ``Pattern`` values; the ``re`` module itself is a supported capture.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TypeAlias

from pyinc import Database, FileResource, Output, action, query

# A term is (sign, kind, value): sign in {+1, -1}; kind in {"int", "name"}; value
# is the digit string or identifier. An expression is a flat sequence of terms
# combined left-to-right — sufficient for integer + / - and fully snapshot-safe.
_Term: TypeAlias = tuple[int, str, str]
_Expr: TypeAlias = tuple[_Term, ...]

# parse_calc payload: (includes, bindings, emits, diagnostics)
_Binding: TypeAlias = tuple[str, _Expr]
_Diagnostic: TypeAlias = tuple[str, str]
_ParsePayload: TypeAlias = tuple[
    tuple[str, ...], tuple[_Binding, ...], tuple[str, ...], tuple[_Diagnostic, ...]
]

# evaluate_name payload: (status, value, code, message) — uniform shape so the
# value field is always an int (0 on error) and narrowing is trivial.
_EvalResult: TypeAlias = tuple[str, int, str, str]

_MAX_INCLUDE_DEPTH = 64

_FILES = FileResource()  # ONE shared file resource; the path is the node key.


@dataclass(frozen=True)
class _Parsed:
    includes: tuple[str, ...]
    bindings: tuple[_Binding, ...]
    emits: tuple[str, ...]
    diagnostics: tuple[_Diagnostic, ...]


def _is_identifier(text: str) -> bool:
    return re.fullmatch(r"[A-Za-z_]\w*", text) is not None


def _parse_expr(rhs: str) -> tuple[_Expr | None, str | None]:
    tokens = re.findall(r"[A-Za-z_]\w*|\d+|[+-]", rhs)
    if "".join(tokens) != re.sub(r"\s+", "", rhs):
        return None, "unexpected characters in expression"
    if not tokens:
        return None, "empty expression"
    terms: list[_Term] = []
    sign = 1
    idx = 0
    if tokens[idx] in ("+", "-"):
        sign = 1 if tokens[idx] == "+" else -1
        idx += 1
    while True:
        if idx >= len(tokens) or tokens[idx] in ("+", "-"):
            return None, "expected operand"
        tok = tokens[idx]
        kind = "int" if tok.isdigit() else "name"
        terms.append((sign, kind, tok))
        idx += 1
        if idx >= len(tokens):
            return tuple(terms), None
        op = tokens[idx]
        if op not in ("+", "-"):
            return None, "expected operator"
        sign = 1 if op == "+" else -1
        idx += 1


def _semantic_token(source: str) -> _ParsePayload:
    """Parse to the canonical (includes, bindings, emits, diagnostics) payload.

    Used both as the parse layer and as ``calc_source``'s cutoff token, so
    comment-only and whitespace-only edits map to an equal token and backdate.
    """
    includes: list[str] = []
    bindings: list[_Binding] = []
    emits: list[str] = []
    diagnostics: list[_Diagnostic] = []

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        rest = rest.strip()
        if head == "include":
            match = re.fullmatch(r'"([^"]*)"', rest)
            if match is None:
                diagnostics.append(("calc-parse-error", f"malformed include: {raw_line!r}"))
            else:
                includes.append(match.group(1))
        elif head == "let":
            match = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(.+)", rest)
            if match is None:
                diagnostics.append(("calc-parse-error", f"malformed binding: {raw_line!r}"))
                continue
            name, expr_text = match.group(1), match.group(2)
            expr, error = _parse_expr(expr_text)
            if expr is None:
                diagnostics.append(("calc-parse-error", f"{name}: {error}"))
            else:
                bindings.append((name, expr))
        elif head == "emit":
            if _is_identifier(rest):
                emits.append(rest)
            else:
                diagnostics.append(("calc-parse-error", f"malformed emit: {raw_line!r}"))
        else:
            diagnostics.append(("calc-parse-error", f"unrecognized line: {raw_line!r}"))

    return (tuple(includes), tuple(bindings), tuple(emits), tuple(diagnostics))


def _parse(source: str) -> _Parsed:
    """Convenience wrapper returning a named structure (test/inspection only;
    not reachable from any ``@query``)."""
    includes, bindings, emits, diagnostics = _semantic_token(source)
    return _Parsed(includes, bindings, emits, diagnostics)


def _resolve_include(current_file: str, target: str) -> str:
    return os.path.normpath(os.path.join(os.path.dirname(current_file), target))


@query(cutoff=_semantic_token)
def calc_source(db: Database, path: str) -> str:
    """Raw text of a ``.calc`` file. Comment/whitespace-only edits backdate."""
    return _FILES.read(db, path)


@query
def parse_calc(db: Database, path: str) -> _ParsePayload:
    return _semantic_token(calc_source(db, path))


@query
def binding_table(db: Database, root_path: str) -> tuple[_Binding, ...]:
    """Merged name->expr map over the root file and its referenced includes.

    Traversed iteratively within this single query so the dependency edges are
    exactly the root plus the includes actually reached (an unreferenced file is
    never read). First binding of a name wins; later duplicates are dropped.
    """
    entries: list[_Binding] = []
    seen: set[str] = set()
    visited: set[str] = {os.path.normpath(root_path)}
    queue: list[str] = [root_path]
    depth = 0
    while queue and depth < _MAX_INCLUDE_DEPTH:
        nxt: list[str] = []
        for current in queue:
            try:
                includes, bindings, _emits, _diags = parse_calc(db, current)
            except FileNotFoundError:
                continue
            for name, expr in bindings:
                if name not in seen:
                    seen.add(name)
                    entries.append((name, expr))
            for target in includes:
                resolved = _resolve_include(current, target)
                if resolved not in visited:
                    visited.add(resolved)
                    nxt.append(resolved)
        queue = nxt
        depth += 1
    return tuple(entries)


@query
def binding_expr(db: Database, root_path: str, name: str) -> tuple[bool, _Expr]:
    """One name's expression. Re-executes on any edit but backdates when this
    name's expression is unchanged, so dependents are reused unless ``name``
    actually changed."""
    for bound_name, expr in binding_table(db, root_path):
        if bound_name == name:
            return (True, expr)
    return (False, ())


@query
def binding_cycles(db: Database, root_path: str) -> tuple[str, ...]:
    """Names that participate in a reference cycle (structural; consulted before
    any cross-query recursion so the kernel's CycleError is never triggered)."""
    table: dict[str, _Expr] = dict(binding_table(db, root_path))
    refs: dict[str, set[str]] = {
        name: {value for _sign, kind, value in expr if kind == "name" and value in table}
        for name, expr in table.items()
    }

    cyclic: set[str] = set()
    color: dict[str, int] = {}  # 0=unvisited, 1=on-stack, 2=done

    def visit(node: str, path: list[str]) -> None:
        color[node] = 1
        path.append(node)
        for nxt in sorted(refs.get(node, set())):
            state = color.get(nxt, 0)
            if state == 1:  # back-edge: nxt..top-of-stack are all cyclic
                cyclic.update(path[path.index(nxt):])
            elif state == 0:
                visit(nxt, path)
        path.pop()
        color[node] = 2

    for start in sorted(refs):
        if color.get(start, 0) == 0:
            visit(start, [])
    return tuple(sorted(cyclic))


@query
def evaluate_name(db: Database, root_path: str, name: str) -> _EvalResult:
    """Evaluate one name to an integer value or a deterministic diagnostic."""
    if name in binding_cycles(db, root_path):
        return ("error", 0, "calc-cycle", f"{name} is part of a binding cycle")
    bound, expr = binding_expr(db, root_path, name)
    if not bound:
        return ("error", 0, "calc-unbound", f"{name} is not bound")
    total = 0
    for sign, kind, value in expr:
        if kind == "int":
            total += sign * int(value)
        else:
            sub = evaluate_name(db, root_path, value)
            if sub[0] == "error":
                return sub
            total += sign * sub[1]
    return ("value", total, "", "")


@query
def emit_names(db: Database, root_path: str) -> tuple[str, ...]:
    _includes, _bindings, emits, _diags = parse_calc(db, root_path)
    return emits


def _render(result: _EvalResult) -> str:
    status, value, code, message = result
    if status == "value":
        return str(value)
    return f"ERROR {code}: {message}"


@action(tool="calc")
def calc_emit(db: Database, root_path: str) -> list[Output]:
    """Emit one ``<name>.out`` file per ``emit`` declaration in the root file.

    A missing *root* file surfaces as ``FileNotFoundError`` from the resource
    read (``emit_names`` must read the root to know what to emit); a missing
    *include* degrades gracefully, since ``binding_table`` guards that read.
    """
    return [
        Output.text(f"{name}.out", _render(evaluate_name(db, root_path, name)) + "\n")
        for name in emit_names(db, root_path)
    ]
