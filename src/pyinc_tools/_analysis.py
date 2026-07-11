from __future__ import annotations

import ast
import keyword
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from typing import Literal, cast, overload

from pyinc import Database
from pyinc._python_lexing import identifier_tokens
from pyinc.integrations import (
    DocumentMap,
    ModuleSymbolTable,
    Parameter,
    ResolvedImportRef,
    ScopeTree,
    Signature,
    SourcePosition,
    SourceRange,
    Symbol,
    SymbolId,
    module_analysis,
    module_symbol_table,
    scope_tree,
    symbol_at,
)

from ._document import (
    _next_source_line_start,
    _replace_source_line,
    _source_line_bounds,
    _source_offset_to_position,
    _source_position_to_offset,
)
from ._models import (
    CompletionItem,
    CompletionItemKind,
    DocumentLink,
    FoldingRange,
    InlayHint,
    ResolutionKind,
    ResolvedTarget,
    SelectionRange,
    SemanticToken,
    SemanticTokenModifier,
    SemanticTokenType,
    SignatureParameterInfo,
)

_MAX_RESOLUTION_DEPTH = 8


def _terminal_target(
    original_module: str,
    qualified_name: str,
    resolution: ResolutionKind,
    *,
    defining_module: str | None = None,
    defining_path: str | None = None,
    source_range: SourceRange | None = None,
    distribution_name: str | None = None,
    distribution_version: str | None = None,
    follow_depth: int = 0,
    trail: tuple[str, ...] = (),
) -> ResolvedTarget:
    return ResolvedTarget(
        original_module=original_module,
        qualified_name=qualified_name,
        resolution=resolution,
        defining_module=defining_module,
        defining_path=defining_path,
        range=source_range,
        distribution_name=distribution_name,
        distribution_version=distribution_version,
        follow_depth=follow_depth,
        trail=trail,
    )


def _symbol_for_id(
    db: Database, root: str, symbol_id: SymbolId
) -> tuple[ModuleSymbolTable, Symbol | None]:
    table = module_symbol_table(db, root, symbol_id.path)
    matching = [
        item for item in table.symbols if item.qualified_name.rsplit(".", 1)[-1] == symbol_id.name
    ]
    symbol = next(
        (item for item in matching if item.range == symbol_id.declaration),
        None,
    )
    if symbol is None:
        symbol = next(
            (
                item
                for item in matching
                if item.range.start.line == symbol_id.declaration.start.line
            ),
            None,
        )
    return table, symbol


def _target_from_symbol_id(
    db: Database,
    root: str,
    original_module: str,
    requested_name: str,
    symbol_id: SymbolId,
    trail: tuple[str, ...],
) -> ResolvedTarget:
    table, symbol = _symbol_for_id(db, root, symbol_id)
    qualified_name = symbol.qualified_name if symbol is not None else symbol_id.name
    source_range = symbol.range if symbol is not None else symbol_id.declaration
    return _terminal_target(
        original_module,
        qualified_name,
        "workspace",
        defining_module=table.module,
        defining_path=symbol_id.path,
        source_range=source_range,
        follow_depth=1
        if table.module != original_module or qualified_name != requested_name
        else 0,
        trail=trail,
    )


def target_from_symbol_id(db: Database, root: str, symbol_id: SymbolId) -> ResolvedTarget:
    """Describe an already-resolved public symbol identity."""

    table = module_symbol_table(db, root, symbol_id.path)
    return _target_from_symbol_id(
        db,
        root,
        table.module,
        symbol_id.name,
        symbol_id,
        (f"{table.module}:{symbol_id.name}",),
    )


def _resolve_at_known_positions(
    db: Database,
    root: str,
    path: str,
    qualified_name: str,
    symbol: Symbol | None,
) -> SymbolId | None:
    if symbol is not None:
        resolved = symbol_at(db, root, path, symbol.range.start)
        if resolved is not None:
            return resolved
    bare_name = qualified_name.rsplit(".", 1)[-1]
    lexical = scope_tree(db, path)
    occurrences = [
        occurrence
        for occurrence in lexical.occurrences
        if occurrence.name == bare_name and not occurrence.is_declaration
    ]
    for occurrence in occurrences:
        resolved = symbol_at(db, root, path, occurrence.range.start)
        if resolved is not None:
            return resolved
    return None


def _matching_import(
    db: Database, root: str, path: str, symbol: Symbol
) -> ResolvedImportRef | None:
    source_module = symbol.import_source_module
    if source_module is None:
        return None
    source_name = symbol.import_source_name
    analysis = module_analysis(db, root, path)
    return next(
        (
            item
            for item in analysis.resolved_imports
            if item.module == source_module and item.imported_name == source_name
        ),
        None,
    )


def _import_is_module_target(symbol: Symbol, imported: ResolvedImportRef) -> bool:
    if symbol.kind == "import_alias":
        return True
    source_name = symbol.import_source_name
    resolved_module = imported.resolved_module
    return (
        source_name is not None
        and resolved_module is not None
        and (resolved_module == source_name or resolved_module.endswith(f".{source_name}"))
    )


def resolve_target(
    db: Database,
    root: str,
    path: str,
    qualified_name: str,
    *,
    _visited: frozenset[tuple[str, str]] = frozenset(),
    _trail: tuple[tuple[str, str], ...] = (),
) -> ResolvedTarget:
    """Resolve a declaration through only public integration contracts."""

    table = module_symbol_table(db, root, path)
    original_module = table.module
    key = (path, qualified_name)
    path_trail = (*_trail, key)
    trail = tuple(f"{module}:{name}" for module, name in path_trail)
    if key in _visited or len(_visited) >= _MAX_RESOLUTION_DEPTH:
        return _terminal_target(
            original_module,
            qualified_name,
            "ambiguous",
            follow_depth=len(_visited),
            trail=trail,
        )

    symbol = next(
        (item for item in table.symbols if item.qualified_name == qualified_name),
        None,
    )
    resolved_id = _resolve_at_known_positions(db, root, path, qualified_name, symbol)
    if resolved_id is not None:
        return _target_from_symbol_id(db, root, original_module, qualified_name, resolved_id, trail)

    if symbol is not None and symbol.kind in {
        "function",
        "method",
        "class",
        "class_variable",
        "variable",
    }:
        return _terminal_target(
            original_module,
            symbol.qualified_name,
            "workspace",
            defining_module=table.module,
            defining_path=path,
            source_range=symbol.range,
            trail=trail,
        )

    if symbol is not None and symbol.kind in {"import_alias", "from_import_alias"}:
        imported = _matching_import(db, root, path, symbol)
        if imported is None:
            return _terminal_target(original_module, qualified_name, "missing", trail=trail)
        resolution = imported.resolution
        if resolution != "workspace":
            return _terminal_target(
                original_module,
                qualified_name,
                resolution,
                distribution_name=imported.distribution_name,
                distribution_version=imported.distribution_version,
                trail=trail,
            )
        if imported.resolved_path is not None and _import_is_module_target(symbol, imported):
            return _terminal_target(
                original_module,
                qualified_name,
                "workspace",
                defining_module=imported.resolved_module,
                defining_path=imported.resolved_path,
                follow_depth=1,
                trail=trail,
            )
        if imported.resolved_path is not None and symbol.import_source_name is not None:
            candidate = resolve_target(
                db,
                root,
                imported.resolved_path,
                symbol.import_source_name,
                _visited=_visited | {key},
                _trail=path_trail,
            )
            return ResolvedTarget(
                original_module=original_module,
                qualified_name=candidate.qualified_name,
                resolution=candidate.resolution,
                defining_module=candidate.defining_module,
                defining_path=candidate.defining_path,
                range=candidate.range,
                distribution_name=candidate.distribution_name,
                distribution_version=candidate.distribution_version,
                follow_depth=candidate.follow_depth + 1,
                trail=candidate.trail,
            )
        return _terminal_target(original_module, qualified_name, "missing", trail=trail)

    analysis = module_analysis(db, root, path)
    wildcard_matches: list[ResolvedTarget] = []
    dynamic_provider = False
    next_visited = _visited | {key}
    for imported in analysis.resolved_imports:
        if imported.imported_name != "*" or imported.resolution != "workspace":
            continue
        if imported.resolved_path is None:
            continue
        dependency = next(
            (item for item in analysis.dependencies if item.path == imported.resolved_path),
            None,
        )
        provider = module_symbol_table(db, root, imported.resolved_path)
        if "dynamic __all__" in provider.impurity_reasons:
            dynamic_provider = True
            continue
        if dependency is None or qualified_name not in dependency.exports:
            continue
        candidate = resolve_target(
            db,
            root,
            imported.resolved_path,
            qualified_name,
            _visited=next_visited,
            _trail=path_trail,
        )
        if candidate.resolution == "workspace":
            wildcard_matches.append(candidate)

    unique_matches = {
        (item.defining_path, item.range, item.qualified_name): item for item in wildcard_matches
    }
    if len(unique_matches) == 1:
        return next(iter(unique_matches.values()))
    if len(unique_matches) > 1 or dynamic_provider:
        return _terminal_target(
            original_module,
            qualified_name,
            "ambiguous",
            follow_depth=len(next_visited),
            trail=trail,
        )
    return _terminal_target(original_module, qualified_name, "missing", trail=trail)


@overload
def _parse_python(source: str, *, mode: Literal["exec"] = "exec") -> ast.Module: ...


@overload
def _parse_python(source: str, *, mode: Literal["eval"]) -> ast.Expression: ...


def _parse_python(
    source: str, *, mode: Literal["exec", "eval"] = "exec"
) -> ast.Module | ast.Expression:
    """Parse Python and normalize AST UTF-8 byte columns to code points."""

    parsed = ast.parse(source, mode=mode)
    document = DocumentMap(source)
    for node in ast.walk(parsed):
        lineno = getattr(node, "lineno", None)
        col_offset = getattr(node, "col_offset", None)
        if isinstance(lineno, int) and isinstance(col_offset, int):
            node.col_offset = document.from_ast(  # type: ignore[attr-defined]
                lineno, col_offset
            ).character
        end_lineno = getattr(node, "end_lineno", None)
        end_col_offset = getattr(node, "end_col_offset", None)
        if isinstance(end_lineno, int) and isinstance(end_col_offset, int):
            node.end_col_offset = document.from_ast(  # type: ignore[attr-defined]
                end_lineno, end_col_offset
            ).character
    return cast(ast.Module | ast.Expression, parsed)


def _normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _line_char_to_offset(source: str, line: int, character: int) -> int | None:
    return _source_position_to_offset(source, line, character)


def _identifier_at_source_position(source: str, line: int, character: int) -> str | None:
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

    Recognises a bare identifier (``foo``) and the terminal identifier of an
    attribute access. Returns None when the preceding token is not usable — a
    closing bracket, a literal, a Python keyword, or the name of a `def` /
    `class` definition header (which is not a call site). A single-dot access
    retains its owner for compatibility with display-only callers; deeper
    chains return their rightmost identifier because semantic resolution uses
    the separately returned source position.
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
    if k >= 0 and source[k] == ".":
        # `<lhs>.name(` — capture the owner only when it is a single bare Name.
        m = k - 1
        while m >= 0 and source[m] in " \t":
            m -= 1
        if m < 0 or not (source[m].isalnum() or source[m] == "_"):
            return name
        lhs_end = m + 1
        while m >= 0 and (source[m].isalnum() or source[m] == "_"):
            m -= 1
        lhs = source[m + 1 : lhs_end]
        if not lhs or lhs[0].isdigit() or keyword.iskeyword(lhs):
            return name
        p = m
        while p >= 0 and source[p] in " \t":
            p -= 1
        if p >= 0 and source[p] == ".":
            # A deeper chain (`a.b.name(`) — LHS is not a bare Name.
            return name
        return f"{lhs}.{name}"
    return name


def _identifier_start_before(source: str, paren_pos: int) -> int | None:
    """Code-point offset of the rightmost callee identifier before ``(``."""

    cursor = paren_pos - 1
    while cursor >= 0 and source[cursor] in " \t":
        cursor -= 1
    if cursor < 0 or not (source[cursor].isalnum() or source[cursor] == "_"):
        return None
    while cursor >= 0 and (source[cursor].isalnum() or source[cursor] == "_"):
        cursor -= 1
    return cursor + 1


def _position_for_offset(source: str, offset: int) -> SourcePosition:
    return _source_offset_to_position(source, offset)


def _find_call_at_position(
    source: str, line: int, character: int
) -> tuple[str, int, SourcePosition] | None:
    """Locate the call-expression enclosing the cursor.

    Returns ``(function_name, active_parameter_index, name_position)`` or
    ``None``. ``name_position`` points at the rightmost callee identifier and
    is suitable for public position-based symbol resolution.

    The scanner runs forward over `source`, skipping comments and string
    literals, and tracks a stack of open brackets. The topmost open `(`
    whose preceding token is a usable identifier is the enclosing call;
    its accumulated comma count yields the active parameter index. Bare-name
    calls (``foo(``) and attribute calls (including ``pkg.sub.foo(``) are
    detected. Semantic support for the receiver chain is decided later by the
    shared position-based resolver. Subscripted calls (``factory[T](``) are
    not detected.
    """
    target = _line_char_to_offset(source, line, character)
    if target is None:
        return None

    stack: list[tuple[str, str | None, int | None, int]] = []
    n = len(source)
    i = 0
    while i < n and i < target:
        c = source[i]
        if c == "#":
            next_line = _next_source_line_start(source, i)
            i = n if next_line is None else next_line
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
                if ch in "\r\n":
                    next_line = _next_source_line_start(source, j)
                    j = n if next_line is None else next_line
                    break
                j += 1
            i = j
            continue
        if c in "([{":
            name = _identifier_immediately_before(source, i) if c == "(" else None
            name_start = _identifier_start_before(source, i) if name is not None else None
            stack.append((c, name, name_start, 0))
            i += 1
            continue
        if c in ")]}":
            opener = "(" if c == ")" else ("[" if c == "]" else "{")
            if stack and stack[-1][0] == opener:
                stack.pop()
            i += 1
            continue
        if c == "," and stack:
            opener_top, name_top, name_start_top, commas = stack[-1]
            stack[-1] = (opener_top, name_top, name_start_top, commas + 1)
        i += 1

    for opener, name, name_start, commas in reversed(stack):
        if opener == "(" and name is not None and name_start is not None:
            return name, commas, _position_for_offset(source, name_start)
    return None


# Completion is intentionally *line-local* and declaration-driven: it never
# infers runtime types. ``CompletionContext`` is the shape the scanner hands to
# the session, tagged by ``kind``:
#   ("name", prefix)                 — a bare identifier being typed
#   ("attribute", owner, prefix)     — ``owner.<prefix>`` where owner is a bare name
#   ("from_import", module, prefix)  — ``from <module> import <prefix>``
#   ("import_module", prefix)        — ``import <prefix>`` / ``from <prefix>``
CompletionContext = tuple[str, ...]

_FROM_IMPORT_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.*)$")
_FROM_MODULE_RE = re.compile(r"^\s*from\s+([\w.]*)$")
_IMPORT_MODULE_RE = re.compile(r"^\s*import\s+(?:[\w.]+\s*,\s*)*([\w.]*)$")


def _completion_head_in_string_or_comment(head: str) -> bool:
    """Best-effort: is the caret inside a string or line comment on this line?

    Scans the pre-caret text of the current line only. Triple-quoted strings
    spanning lines are not modelled (documented limitation); this keeps
    completion from firing inside ordinary single-line strings and comments.
    """
    quote: str | None = None
    k = 0
    n = len(head)
    while k < n:
        c = head[k]
        if quote is not None:
            if c == "\\":
                k += 2
                continue
            if c == quote:
                quote = None
            k += 1
            continue
        if c in ("'", '"'):
            quote = c
            k += 1
            continue
        if c == "#":
            return True
        k += 1
    return quote is not None


def _completion_token_before(head: str) -> str:
    """The trailing ``[\\w.]*`` run immediately before the caret."""
    i = len(head)
    while i > 0 and (head[i - 1].isalnum() or head[i - 1] in "_."):
        i -= 1
    return head[i:]


def _find_completion_context(source: str, line: int, character: int) -> CompletionContext | None:
    """Classify what the caret at ``(line, character)`` is completing.

    Returns ``None`` when nothing sensible can be offered (inside a string or
    comment, or an attribute access whose owner is not a bare name)."""
    lines = source.splitlines()
    if not (0 <= line < len(lines)):
        # Allow a caret one past the last line (empty trailing line).
        if line == len(lines):
            head = ""
        else:
            return None
    else:
        text = lines[line]
        head = text[: max(0, min(character, len(text)))]

    if _completion_head_in_string_or_comment(head):
        return None

    from_import = _FROM_IMPORT_RE.match(head)
    if from_import is not None:
        module = from_import.group(1)
        after = from_import.group(2)
        # The identifier currently being typed is the trailing word; anything
        # with a dot in this position is out of scope.
        last = after.rsplit(",", 1)[-1].strip()
        if last and not last.replace("_", "").isalnum():
            return None
        return ("from_import", module, last)

    from_module = _FROM_MODULE_RE.match(head)
    if from_module is not None:
        return ("import_module", from_module.group(1))

    import_module = _IMPORT_MODULE_RE.match(head)
    if import_module is not None:
        return ("import_module", import_module.group(1))

    run = _completion_token_before(head)
    if "." in run:
        owner, _, prefix = run.rpartition(".")
        # Accept a bare name (``M.``) or a dotted owner whose every component
        # is an identifier (``pkg.sub.``, ``pkg.sub.M.``); reject anything with
        # an empty / numeric component (a leading dot, ``1.``, etc.). The
        # session decides which dotted owners actually resolve to a workspace
        # module or module-class.
        if not all(part.isidentifier() for part in owner.split(".")):
            return None
        return ("attribute", owner, prefix)
    return ("name", run)


_SYMBOL_TO_COMPLETION_KIND: dict[str, CompletionItemKind] = {
    "function": "function",
    "method": "method",
    "class": "class",
    "class_variable": "field",
    "variable": "variable",
    "import_alias": "module",
    "from_import_alias": "variable",
}

_BINDING_TO_COMPLETION_KIND: dict[str, CompletionItemKind] = {
    "function": "function",
    "class": "class",
    "parameter": "variable",
    "variable": "variable",
    "import_alias": "module",
    "from_import_alias": "variable",
    "loop_target": "variable",
    "with_target": "variable",
    "exception_target": "variable",
    "pattern_target": "variable",
}

# Upper bound on returned items so a broad, empty-prefix request stays bounded;
# editors filter client-side as the user keeps typing.
_COMPLETION_LIMIT = 200


def _repair_caret_line(source: str, line: int) -> str:
    """Return ``source`` with line ``line`` replaced by ``pass`` at its original
    indentation.

    The caret line is typically the only unparseable part of a buffer mid-edit
    (e.g. a trailing ``owner.``). Substituting ``pass`` — rather than blanking
    the line, which would leave an enclosing ``def``/``class`` with an empty
    body — lets the file parse while keeping every top-level import and
    definition intact, which is all the local symbol table and owner resolution
    need."""
    bounds = _source_line_bounds(source)
    if not 0 <= line < len(bounds):
        return source
    start, content_end, _next_start = bounds[line]
    text = source[start:content_end]
    indent = text[: len(text) - len(text.lstrip())]
    return _replace_source_line(source, line, f"{indent}pass")


def _source_parses(text: str) -> bool:
    try:
        _parse_python(text)
    except SyntaxError:
        return False
    return True


def _keyword_completions(prefix: str) -> list[CompletionItem]:
    return [
        CompletionItem(label=kw, kind="keyword", detail=None, sort_text=f"3{kw}")
        for kw in keyword.kwlist
        if kw.startswith(prefix)
    ]


def _build_signature_label(
    name: str,
    signature: Signature,
    defaults: Mapping[str, str] | None = None,
) -> tuple[str, tuple[SignatureParameterInfo, ...]]:
    """Render a ``def name(...)`` label and per-parameter substring offsets.

    ``defaults`` maps a parameter name to the source text of its default value
    (``ast.unparse``d); parameters absent from the mapping render without one.
    Spacing follows PEP 8: ``name: ann = default`` when annotated, ``name=default``
    otherwise. When ``defaults`` is ``None`` the output is byte-identical to the
    annotation-only rendering used by hover and completion detail.
    """
    parts: list[str] = [f"def {name}("]
    info: list[SignatureParameterInfo] = []
    for index, parameter in enumerate(signature.parameters):
        if index > 0:
            parts.append(", ")
        default = defaults.get(parameter.name) if defaults else None
        if parameter.annotation is not None:
            text = f"{parameter.name}: {parameter.annotation}"
            if default is not None:
                text = f"{text} = {default}"
        else:
            text = parameter.name
            if default is not None:
                text = f"{text}={default}"
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


def _defaults_from_arguments(args: ast.arguments) -> dict[str, str]:
    """Map parameter name → ``ast.unparse``d default expression for `args`.

    Positional defaults (``args.defaults``) are tail-aligned against the
    posonly + positional parameters; keyword-only defaults (``args.kw_defaults``)
    zip 1:1 with ``args.kwonlyargs`` (a ``None`` slot means no default).
    Parameters without a default are omitted.
    """
    defaults: dict[str, str] = {}
    positional = [*args.posonlyargs, *args.args]
    positional_defaults = list(args.defaults)
    if positional_defaults:
        for arg, default in zip(
            positional[-len(positional_defaults) :],
            positional_defaults,
            strict=True,
        ):
            defaults[arg.arg] = ast.unparse(default)
    for arg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if kw_default is not None:
            defaults[arg.arg] = ast.unparse(kw_default)
    return defaults


def _parameter_defaults_from_source(source: str, lineno: int, name: str) -> dict[str, str] | None:
    """Default-value expressions for the callable named `name` at `lineno`.

    Parses `source` (the *defining* file) and locates the
    ``FunctionDef`` / ``AsyncFunctionDef`` whose header is at 1-based `lineno`
    with a matching `name`; for a ``ClassDef`` it digs into the class's
    ``__init__``. Returns the name→default mapping (see
    :func:`_defaults_from_arguments`), or ``None`` when the file fails to parse
    or no matching definition is found. ``self`` / ``cls`` carry no default, so
    the mapping already matches the self-stripped constructor signature."""
    try:
        tree = _parse_python(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno == lineno
            and node.name == name
        ):
            return _defaults_from_arguments(node.args)
        if isinstance(node, ast.ClassDef) and node.lineno == lineno and node.name == name:
            for stmt in node.body:
                if (
                    isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and stmt.name == "__init__"
                ):
                    return _defaults_from_arguments(stmt.args)
            return {}
    return None


def _collect_annotation_type_refs(
    annotation: str,
) -> tuple[tuple[str, ...], ...]:
    """Parse a detached annotation string for fallback type references.

    Each entry is either ``("name", id)`` for a bare-name reference or
    ``("attribute", lhs_id, attr)`` for an ``lhs.attr`` reference where the
    LHS is itself a bare name. Attribute chains whose LHS is not a bare
    `Name` (e.g. ``pkg.sub.Foo``) are skipped here because detached text has no
    lexical position with which to prove the receiver. Source-backed
    annotations use :func:`_annotation_type_positions` and the shared public
    resolver instead.

    A whole-string forward reference (``"Foo"``, ``"pkg.Foo | None"``) is
    unwrapped exactly once before walking. Malformed annotation text returns
    an empty tuple.
    """
    try:
        tree = _parse_python(annotation, mode="eval")
    except SyntaxError:
        return ()
    body = tree.body
    if isinstance(body, ast.Constant) and isinstance(body.value, str):
        try:
            body = _parse_python(body.value, mode="eval").body
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
        tree = _parse_python(source)
    except SyntaxError:
        return ()

    ranges: list[FoldingRange] = []

    def walk_definitions(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.decorator_list:
                    first_decorator = min(
                        node.decorator_list,
                        key=lambda decorator: (
                            decorator.lineno,
                            decorator.col_offset,
                        ),
                    )
                    start = SourcePosition(
                        first_decorator.lineno - 1,
                        first_decorator.col_offset,
                    )
                else:
                    start = SourcePosition(node.lineno - 1, node.col_offset)
                end = SourcePosition(
                    (node.end_lineno or node.lineno) - 1,
                    node.end_col_offset or node.col_offset,
                )
                if end.line > start.line:
                    ranges.append(
                        FoldingRange(
                            range=SourceRange(start, end),
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

    run_start: SourcePosition | None = None
    run_end: SourcePosition | None = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if run_start is None:
                run_start = SourcePosition(node.lineno - 1, node.col_offset)
            run_end = SourcePosition(
                (node.end_lineno or node.lineno) - 1,
                node.end_col_offset or node.col_offset,
            )
        else:
            if run_start is not None and run_end is not None and run_end.line > run_start.line:
                ranges.append(
                    FoldingRange(
                        range=SourceRange(run_start, run_end),
                        kind="imports",
                    )
                )
            run_start = None
            run_end = None
    if run_start is not None and run_end is not None and run_end.line > run_start.line:
        ranges.append(
            FoldingRange(
                range=SourceRange(run_start, run_end),
                kind="imports",
            )
        )

    ranges.sort(key=lambda item: (item.range.start, item.range.end))
    return tuple(ranges)


def _compute_selection_chain(source: str, line: int, character: int) -> tuple[SelectionRange, ...]:
    """Walk the AST of `source` and return a chain of nested ranges around the cursor.

    The chain is ordered innermost-first; each subsequent entry strictly contains its
    predecessor. Coordinates are 0-based (LSP-style) for both line and character.
    Returns `()` when the file fails to parse, the cursor is out of bounds, or no AST
    node contains the cursor.
    """
    try:
        tree = _parse_python(source)
    except SyntaxError:
        return ()

    line_bounds = _source_line_bounds(source)
    line_count = len(line_bounds)
    if line < 0 or line >= len(line_bounds):
        return ()
    line_start, line_end, _next_start = line_bounds[line]
    if character < 0 or character > line_end - line_start:
        return ()
    cursor = line_start + character

    candidates: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        start_lineno = getattr(node, "lineno", None)
        start_col = getattr(node, "col_offset", None)
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if start_lineno is None or start_col is None or end_lineno is None or end_col is None:
            continue
        if start_lineno < 1 or start_lineno > line_count:
            continue
        if end_lineno < 1 or end_lineno > line_count:
            continue
        start_offset = line_bounds[start_lineno - 1][0] + start_col
        end_offset = line_bounds[end_lineno - 1][0] + end_col
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

    selection_ranges: list[SelectionRange] = []
    for start_offset, end_offset in chain:
        start_position = _source_offset_to_position(source, start_offset)
        end_position = _source_offset_to_position(source, end_offset)
        selection_ranges.append(SelectionRange(range=SourceRange(start_position, end_position)))
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
        tree = _parse_python(source)
    except SyntaxError:
        return ()

    import_targets: dict[tuple[int, str], str] = {}
    from_targets: dict[tuple[int, str], str] = {}
    for resolved in resolved_imports:
        if resolved.resolution != "workspace" or resolved.resolved_path is None:
            continue
        if resolved.kind == "import":
            import_targets[(resolved.range.start.line + 1, resolved.module)] = (
                resolved.resolved_path
            )
        elif resolved.imported_name is not None:
            from_targets[(resolved.range.start.line + 1, resolved.imported_name)] = (
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
                        range=SourceRange(
                            SourcePosition(alias.lineno - 1, alias.col_offset),
                            SourcePosition(alias.end_lineno - 1, alias.end_col_offset),
                        ),
                        target_path=target,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*" or alias.end_lineno is None or alias.end_col_offset is None:
                    continue
                target = from_targets.get((node.lineno, alias.name))
                if target is None:
                    continue
                links.append(
                    DocumentLink(
                        range=SourceRange(
                            SourcePosition(alias.lineno - 1, alias.col_offset),
                            SourcePosition(alias.end_lineno - 1, alias.end_col_offset),
                        ),
                        target_path=target,
                    )
                )

    links.sort(key=lambda link: link.range.start)
    return tuple(links)


_CallableNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _find_callable_node(tree: ast.Module, qualified_name: str) -> _CallableNode | None:
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

    def walk(nodes: list[ast.stmt], remaining: list[str]) -> _CallableNode | None:
        head = remaining[0]
        rest = remaining[1:]
        for node in nodes:
            if isinstance(node, ast.ClassDef) and node.name == head:
                if not rest:
                    return node
                found = walk(list(node.body), rest)
                if found is not None:
                    return found
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == head:
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
            qname = f"{class_qualifier}.{node.name}" if class_qualifier else node.name
            end_lineno = node.end_lineno or node.lineno
            if node.lineno <= line <= end_lineno and qname in known_qnames:
                span = end_lineno - node.lineno
                if best is None or span < best[0]:
                    best = (span, qname)
            for body_child in node.body:
                visit(body_child, qname)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = f"{class_qualifier}.{node.name}" if class_qualifier else node.name
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


def _first_positional_param(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    """Name of a callable's first positional parameter, or ``None``.

    Positional-only parameters take precedence, then regular positionals; a
    callable that takes only ``*args`` / keyword parameters has none."""
    args = node.args
    if args.posonlyargs:
        return args.posonlyargs[0].arg
    if args.args:
        return args.args[0].arg
    return None


def _enclosing_method_context(tree: ast.Module, line: int) -> tuple[str, str] | None:
    """The class qualifier and first-parameter name of the method enclosing
    the 1-based `line`, or ``None``.

    The innermost callable containing `line` must be a ``FunctionDef`` /
    ``AsyncFunctionDef`` that is a *direct* child of a ``ClassDef`` body — a
    closure nested inside a method returns ``None``, as does a module-level
    function or a caret outside any callable. The class qualifier follows
    ``module_symbol_table``'s scheme (``Outer.Inner``; function-nested classes
    reset). The first-parameter name is returned verbatim so the caller can
    apply the literal ``self`` / ``cls`` rule; a method with no positional
    parameter yields ``None``.
    """
    best: tuple[int, tuple[str, str] | None] | None = None

    def visit(node: ast.AST, class_qualifier: str, direct_class: str | None) -> None:
        nonlocal best
        if isinstance(node, ast.ClassDef):
            qname = f"{class_qualifier}.{node.name}" if class_qualifier else node.name
            for body_child in node.body:
                visit(body_child, qname, qname)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_lineno = node.end_lineno or node.lineno
            if node.lineno <= line <= end_lineno:
                span = end_lineno - node.lineno
                first = _first_positional_param(node)
                payload = (
                    (direct_class, first)
                    if direct_class is not None and first is not None
                    else None
                )
                if best is None or span < best[0]:
                    best = (span, payload)
            for descendant in ast.iter_child_nodes(node):
                visit(descendant, "", None)
            return
        for descendant in ast.iter_child_nodes(node):
            visit(descendant, class_qualifier, None)

    visit(tree, "", None)
    return best[1] if best is not None else None


def _iter_own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Yield the descendants of `node` that share its scope.

    Descends through control-flow blocks (``if`` / ``for`` / ``while`` /
    ``with`` / ``try``) but never into nested ``def`` / ``async def`` /
    ``class`` / ``lambda`` bodies, so a scan stays inside `node`'s own scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        yield child
        yield from _iter_own_scope(child)


def _annotation_expr_for_name_at(tree: ast.Module, line: int, name: str) -> ast.expr | None:
    """Annotation expression bound to bare ``name`` visible at 1-based `line`.

    Rule A's local declaration lookup — first hit wins:

    1. a parameter named ``name`` (with an annotation) of the innermost
       function enclosing `line`;
    2. otherwise the nearest preceding ``AnnAssign`` to bare ``Name`` ``name``
       (``lineno <= line``) inside that same function's own scope — control-flow
       blocks are searched, nested ``def`` / ``class`` / ``lambda`` scopes are
       not.

    Returns the annotation node, or ``None`` when neither applies. The
    module-level fallback (priority 3) is the caller's responsibility."""
    enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    enclosing_span: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end:
                span = end - node.lineno
                if enclosing_span is None or span < enclosing_span:
                    enclosing = node
                    enclosing_span = span
    if enclosing is None:
        return None

    args = enclosing.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        params.append(args.vararg)
    if args.kwarg is not None:
        params.append(args.kwarg)
    for param in params:
        if param.arg == name and param.annotation is not None:
            return param.annotation

    best: ast.expr | None = None
    best_lineno = 0
    for stmt in _iter_own_scope(enclosing):
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name
            and stmt.lineno <= line
            and stmt.lineno > best_lineno
        ):
            best = stmt.annotation
            best_lineno = stmt.lineno
    return best


# self./cls. member views: `self` sees instance attributes plus everything the
# class view sees; `cls` never sees instance attributes.
_INSTANCE_MEMBER_KINDS: frozenset[str] = frozenset(
    {"method", "class_variable", "instance_variable"}
)
_CLASS_MEMBER_KINDS: frozenset[str] = frozenset({"method", "class_variable"})


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

    For `Name(id=name)` it's the entire Name; for any `Attribute` chain it's
    just the rightmost-attribute span (matching `find_references`'s reporting
    convention). Returns ``None`` for subscripted calls, lambdas, and other
    unsupported call shapes so the caller can skip them.
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
    if isinstance(func, ast.Attribute):
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


def _expression_name_position(node: ast.expr) -> SourcePosition | None:
    """Position of the terminal identifier in a supported expression."""

    while isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return SourcePosition(node.lineno - 1, node.col_offset)
    if isinstance(node, ast.Attribute):
        end_line = (node.end_lineno or node.lineno) - 1
        end_character = node.end_col_offset
        if end_character is None:
            return None
        return SourcePosition(end_line, end_character - len(node.attr))
    return None


def _annotation_type_positions(
    source: str,
    binding_name: str,
    binding_kind: str,
    binding_range: SourceRange,
) -> tuple[SourcePosition, ...]:
    """Locate non-string type-name expressions for a lexical binding."""

    try:
        tree = _parse_python(source)
    except SyntaxError:
        return ()

    annotation: ast.expr | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.arg)
            and node.arg == binding_name
            and node.lineno - 1 == binding_range.start.line
            and node.col_offset == binding_range.start.character
        ):
            annotation = node.annotation
            break
        if isinstance(node, ast.AnnAssign):
            target_position = _expression_name_position(node.target)
            if target_position == binding_range.start:
                annotation = node.annotation
                break
        if (
            binding_kind == "function"
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == binding_name
            and node.lineno - 1 == binding_range.start.line
        ):
            annotation = node.returns
            break
    if annotation is None:
        return ()

    positions: set[SourcePosition] = set()

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Attribute):
            position = _expression_name_position(node)
            if position is not None:
                positions.add(position)
            return
        if isinstance(node, ast.Name):
            positions.add(SourcePosition(node.lineno - 1, node.col_offset))
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(annotation)
    return tuple(sorted(positions))


def _unwrap_base_expression(node: ast.expr) -> tuple[str, ...] | None:
    """Map a ``ClassDef`` base expression to a resolver-ready tuple.

    Returns ``("name", id)`` for a bare ``Name`` or an ``"attr"`` tuple for
    an attribute chain rooted at a bare name. ``Subscript`` bases
    (``Generic[T]``, ``Base[T]``) are unwrapped to their ``value`` once, so
    ``Base[T]`` resolves to ``Base``. ``Starred`` bases, call expressions, and
    attribute expressions without a bare-name root are rejected. The caller
    resolves supported shapes by source position through the shared resolver.
    """
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return ("name", node.id)
    if isinstance(node, ast.Attribute):
        attributes = [node.attr]
        value = node.value
        while isinstance(value, ast.Attribute):
            attributes.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            return ("attr", value.id, *reversed(attributes))
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
            qname = f"{class_qualifier}.{node.name}" if class_qualifier else node.name
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
                position=SourcePosition(arg.lineno - 1, arg.col_offset),
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
    located = _normalized_name_offsets_on_line(line, node.name, node.col_offset)
    if located is None:
        return None
    return node.lineno, *located


def _normalized_name_offsets_on_line(
    line: str,
    name: str,
    minimum_character: int = 0,
) -> tuple[int, int] | None:
    """Locate an identifier even when the AST normalized its source spelling."""

    for token in identifier_tokens(line):
        if (
            token.start[1] >= minimum_character
            and unicodedata.normalize("NFKC", token.string) == name
        ):
            return token.start[1], token.end[1]
    return None


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
    source: str,
    symbol_table: ModuleSymbolTable,
    lexical: ScopeTree,
) -> tuple[SemanticToken, ...]:
    """Walk ``source``'s AST and emit semantic tokens for declarations
    (function / method / class headers and function parameters) and for bare
    ``ast.Name`` uses resolved by the shared lexical scope tree or the module
    symbol table.

    Files that fail to parse return ``()``. Token coordinates are 0-based
    (LSP-style); the returned tuple is sorted by ``(line, character)``.

    Use-site classification combines the module symbol table with the shared
    lexical scope tree.  A local binding therefore wins over an identically
    named module binding; attribute access and cross-module re-export
    following remain out of scope for semantic-token classification.
    """
    try:
        tree = _parse_python(source)
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

    occurrences = {occurrence.range: occurrence for occurrence in lexical.occurrences}
    bindings = {binding.symbol_id: binding for binding in lexical.bindings}

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
                range=SourceRange(
                    SourcePosition(lineno - 1, col_offset),
                    SourcePosition(lineno - 1, col_offset + length),
                ),
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
                line_index = arg.lineno - 1
                if not 0 <= line_index < len(lines):
                    continue
                located_argument = _normalized_name_offsets_on_line(
                    lines[line_index],
                    arg.arg,
                    arg.col_offset,
                )
                if located_argument is None:
                    continue
                argument_start, argument_end = located_argument
                emit(
                    arg.lineno,
                    argument_start,
                    argument_end - argument_start,
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
            node_range = SourceRange(
                SourcePosition(node.lineno - 1, node.col_offset),
                SourcePosition(
                    (node.end_lineno or node.lineno) - 1,
                    node.end_col_offset or node.col_offset + len(node.id),
                ),
            )
            occurrence = occurrences.get(node_range)
            binding = (
                bindings.get(occurrence.symbol_id)
                if occurrence is not None and occurrence.symbol_id is not None
                else None
            )
            if binding is not None and binding.scope_id != "module":
                if binding.kind == "function":
                    token_type: SemanticTokenType | None = "function"
                elif binding.kind == "class":
                    token_type = "class"
                elif binding.kind == "parameter":
                    token_type = "parameter"
                elif binding.kind == "import_alias":
                    token_type = "namespace"
                else:
                    token_type = "variable"
            else:
                token_type = name_to_token_type.get(node.id)
            if token_type is not None and node.lineno is not None:
                emit(
                    node.lineno,
                    node_range.start.character,
                    node_range.end.character - node_range.start.character,
                    token_type,
                    (),
                )
            return
        for descendant in ast.iter_child_nodes(node):
            walk(descendant, inside_class=inside_class)

    walk(tree, inside_class=False)
    tokens.sort(key=lambda token: token.range.start)
    return tuple(tokens)


__all__ = [
    "_BINDING_TO_COMPLETION_KIND",
    "_CLASS_MEMBER_KINDS",
    "_COMPLETION_LIMIT",
    "_INSTANCE_MEMBER_KINDS",
    "_SYMBOL_TO_COMPLETION_KIND",
    "_annotation_expr_for_name_at",
    "_annotation_type_positions",
    "_build_signature_label",
    "_call_func_range",
    "_collect_annotation_type_refs",
    "_collect_outgoing_calls",
    "_compute_document_links",
    "_compute_folding_ranges",
    "_compute_selection_chain",
    "_compute_semantic_tokens",
    "_defaults_from_arguments",
    "_enclosing_callable_qname",
    "_enclosing_method_context",
    "_expression_name_position",
    "_find_call_at_position",
    "_find_callable_node",
    "_find_completion_context",
    "_first_positional_param",
    "_identifier_at_source_position",
    "_inlay_hints_for_call",
    "_iter_own_scope",
    "_keyword_completions",
    "_locate_def_name_offsets_on_header",
    "_normalize_dependency_name",
    "_parameter_defaults_from_source",
    "_parse_python",
    "_repair_caret_line",
    "_source_parses",
    "_unwrap_base_expression",
    "_walk_class_definitions",
    "resolve_target",
    "target_from_symbol_id",
]
