from __future__ import annotations

import ast
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, overload

from pyinc._python_lexing import identifier_tokens
from pyinc.core import query
from pyinc.integrations.python_source import source_text
from pyinc.integrations.source_geometry import DocumentMap, SourcePosition, SourceRange
from pyinc.runtime import Database

from ._decoding import decoded

ScopeKind: TypeAlias = Literal[
    "module", "class", "function", "lambda", "comprehension", "type_alias"
]
BindingKind: TypeAlias = Literal[
    "function",
    "class",
    "parameter",
    "variable",
    "import_alias",
    "from_import_alias",
    "loop_target",
    "with_target",
    "exception_target",
    "pattern_target",
    "type_alias",
    "type_parameter",
]

PositionPayload: TypeAlias = tuple[int, int]
RangePayload: TypeAlias = tuple[PositionPayload, PositionPayload]
ScopePayload: TypeAlias = tuple[str, ScopeKind, RangePayload, str | None]
BindingPayload: TypeAlias = tuple[str, str, BindingKind, str, RangePayload, str | None, str | None]
OccurrencePayload: TypeAlias = tuple[str, RangePayload, str | None, bool, str | None, bool]
ScopeTreePayload: TypeAlias = tuple[
    str,
    tuple[ScopePayload, ...],
    tuple[BindingPayload, ...],
    tuple[OccurrencePayload, ...],
]


@dataclass(frozen=True)
class SymbolId:
    """Stable identity for a lexical binding."""

    path: str
    scope_id: str
    name: str
    declaration: SourceRange


@dataclass(frozen=True)
class Scope:
    id: str
    kind: ScopeKind
    range: SourceRange
    parent_id: str | None


@dataclass(frozen=True)
class Binding:
    symbol_id: SymbolId
    name: str
    kind: BindingKind
    scope_id: str
    range: SourceRange
    annotation: str | None = None
    import_source: str | None = None


@dataclass(frozen=True)
class SymbolOccurrence:
    name: str
    range: SourceRange
    symbol_id: SymbolId | None
    is_declaration: bool
    receiver: str | None = None
    is_deletion: bool = False


@dataclass(frozen=True)
class ScopeTree:
    path: str
    scopes: tuple[Scope, ...]
    bindings: tuple[Binding, ...]
    occurrences: tuple[SymbolOccurrence, ...]

    def symbol_at(self, position: SourcePosition) -> SymbolId | None:
        candidates = [
            item for item in self.occurrences if item.range.contains(position, include_end=False)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                item.range.end.line - item.range.start.line,
                item.range.end.character - item.range.start.character,
            )
        )
        return candidates[0].symbol_id

    def occurrence_at(self, position: SourcePosition) -> SymbolOccurrence | None:
        candidates = [
            item for item in self.occurrences if item.range.contains(position, include_end=False)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                item.range.end.line - item.range.start.line,
                item.range.end.character - item.range.start.character,
            )
        )
        return candidates[0]


@dataclass
class _MutableScope:
    id: str
    kind: ScopeKind
    source_range: SourceRange
    parent: _MutableScope | None
    globals: set[str]
    nonlocals: set[str]
    local_names: set[str]
    receiver_name: str | None = None
    receiver_class: _MutableScope | None = None


@dataclass(frozen=True)
class _Declaration:
    scope: _MutableScope
    name: str
    kind: BindingKind
    source_range: SourceRange
    annotation: str | None
    import_source: str | None
    receiver: str | None
    order: int


@dataclass(frozen=True)
class _Use:
    scope: _MutableScope
    name: str
    source_range: SourceRange
    receiver: str | None = None
    order: int = 0
    is_deletion: bool = False


_AST_TYPE_ALIAS = getattr(ast, "TypeAlias", None)


def _is_type_alias(node: ast.AST) -> bool:
    return isinstance(_AST_TYPE_ALIAS, type) and isinstance(node, _AST_TYPE_ALIAS)


def _normalized_identifier(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _dotted_expr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_expr_name(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
    return ""


class _ScopeBuilder:
    def __init__(self, path: str, source: str, tree: ast.Module) -> None:
        self.path = path
        self.source = source
        self.document = DocumentMap(source)
        end_line = max(len(self.document.lines) - 1, 0)
        end_character = len(self.document.lines[end_line])
        root_range = SourceRange(SourcePosition(0, 0), SourcePosition(end_line, end_character))
        self.root = _MutableScope("module", "module", root_range, None, set(), set(), set())
        self.scopes: list[_MutableScope] = [self.root]
        self.declarations: list[_Declaration] = []
        self.uses: list[_Use] = []
        self._event_counter = 0
        normalized_source = "\n".join(self.document.lines)
        self._tokens = identifier_tokens(normalized_source)
        self._visit_statements(tree.body, self.root)

    def build(self) -> ScopeTree:
        grouped: dict[tuple[str, str], list[_Declaration]] = {}
        for declaration in self.declarations:
            owner = self._binding_owner(
                declaration.scope,
                declaration.name,
                receiver=declaration.receiver,
                order=declaration.order,
            )
            if owner is None:
                continue
            owner.local_names.add(declaration.name)
            grouped.setdefault((owner.id, declaration.name), []).append(declaration)

        symbols: dict[tuple[str, str], SymbolId] = {}
        bindings: list[Binding] = []
        for (scope_id, name), declarations in grouped.items():
            declarations.sort(key=lambda item: (item.source_range.start, item.source_range.end))
            first = declarations[0]
            symbol_id = SymbolId(self.path, scope_id, name, first.source_range)
            symbols[(scope_id, name)] = symbol_id
            bindings.append(
                Binding(
                    symbol_id=symbol_id,
                    name=name,
                    kind=first.kind,
                    scope_id=scope_id,
                    range=first.source_range,
                    annotation=first.annotation,
                    import_source=first.import_source,
                )
            )

        occurrences: list[SymbolOccurrence] = []
        for declaration in self.declarations:
            owner = self._binding_owner(
                declaration.scope,
                declaration.name,
                receiver=declaration.receiver,
                order=declaration.order,
            )
            occurrences.append(
                SymbolOccurrence(
                    declaration.name,
                    declaration.source_range,
                    symbols.get((owner.id, declaration.name)) if owner is not None else None,
                    True,
                    declaration.receiver,
                    False,
                )
            )
        for use in self.uses:
            if use.receiver is None:
                use_owner = self._resolve_owner(use.scope, use.name, use.order)
            elif use.receiver in {"self", "cls"}:
                use_owner = self._proven_receiver_class(
                    use.scope,
                    use.receiver,
                    use.order,
                )
            else:
                use_owner = None
            use_symbol_id = symbols.get((use_owner.id, use.name)) if use_owner is not None else None
            occurrences.append(
                SymbolOccurrence(
                    use.name,
                    use.source_range,
                    use_symbol_id,
                    False,
                    use.receiver,
                    use.is_deletion,
                )
            )

        scopes = tuple(
            Scope(item.id, item.kind, item.source_range, item.parent.id if item.parent else None)
            for item in self.scopes
        )
        bindings.sort(key=lambda item: (item.range.start, item.name))
        occurrences.sort(key=lambda item: (item.range.start, item.range.end))
        return ScopeTree(self.path, scopes, tuple(bindings), tuple(occurrences))

    def _new_scope(self, kind: ScopeKind, node: ast.AST, parent: _MutableScope) -> _MutableScope:
        source_range = self.document.ast_range(node)
        scope_id = f"{kind}@{source_range.start.line}:{source_range.start.character}"
        scope = _MutableScope(scope_id, kind, source_range, parent, set(), set(), set())
        self.scopes.append(scope)
        return scope

    def _declare(
        self,
        scope: _MutableScope,
        name: str,
        kind: BindingKind,
        source_range: SourceRange,
        *,
        annotation: str | None = None,
        import_source: str | None = None,
        receiver: str | None = None,
    ) -> None:
        if name not in scope.globals and name not in scope.nonlocals:
            scope.local_names.add(name)
        order = self._next_event()
        self.declarations.append(
            _Declaration(
                scope,
                name,
                kind,
                source_range,
                annotation,
                import_source,
                receiver,
                order,
            )
        )

    def _use(
        self,
        scope: _MutableScope,
        name: str,
        source_range: SourceRange,
        receiver: str | None = None,
        *,
        is_deletion: bool = False,
    ) -> None:
        self.uses.append(
            _Use(
                scope,
                name,
                source_range,
                receiver,
                self._next_event(),
                is_deletion,
            )
        )

    def _next_event(self) -> int:
        order = self._event_counter
        self._event_counter += 1
        return order

    def _binding_owner(
        self,
        scope: _MutableScope,
        name: str,
        *,
        receiver: str | None = None,
        order: int,
    ) -> _MutableScope | None:
        if receiver in {"self", "cls"}:
            return self._proven_receiver_class(scope, receiver, order)
        if name in scope.globals:
            return self.root
        if name in scope.nonlocals:
            return self._nearest_enclosing_binding(scope.parent, name)
        return scope

    def _nearest_enclosing_binding(
        self, scope: _MutableScope | None, name: str
    ) -> _MutableScope | None:
        current = scope
        while current is not None and current.kind != "module":
            if current.kind != "class" and name in current.local_names:
                return current
            current = current.parent
        return None

    def _resolve_owner(
        self,
        scope: _MutableScope,
        name: str,
        order: int,
    ) -> _MutableScope | None:
        if name in scope.globals:
            return self.root if name in self.root.local_names else None
        if name in scope.nonlocals:
            return self._nearest_enclosing_binding(scope.parent, name)
        if scope.kind == "class":
            if self._class_binding_precedes(scope, name, order):
                return scope
        elif name in scope.local_names:
            return scope

        current = scope
        parent = current.parent
        while parent is not None:
            # A method/function does not close over the class namespace.
            if (
                current.kind
                in {
                    "function",
                    "lambda",
                    "comprehension",
                    "type_alias",
                }
                and parent.kind == "class"
            ):
                current = parent
                parent = parent.parent
                continue
            if parent.kind == "class":
                current = parent
                parent = parent.parent
                continue
            if name in parent.local_names:
                return parent
            current = parent
            parent = parent.parent
        return None

    def _class_binding_precedes(
        self,
        scope: _MutableScope,
        name: str,
        order: int,
    ) -> bool:
        if name in scope.globals or name in scope.nonlocals:
            return False
        return any(
            declaration.scope is scope
            and declaration.receiver is None
            and declaration.name == name
            and declaration.order < order
            for declaration in self.declarations
        )

    def _proven_receiver_class(
        self,
        scope: _MutableScope,
        receiver: str,
        order: int,
    ) -> _MutableScope | None:
        owner = self._resolve_owner(scope, receiver, order)
        if owner is None or owner.receiver_name != receiver or owner.receiver_class is None:
            return None
        if any(
            declaration.receiver is None
            and declaration.name == receiver
            and declaration.kind != "parameter"
            and declaration.order < order
            and self._binding_owner(
                declaration.scope,
                declaration.name,
                order=declaration.order,
            )
            is owner
            for declaration in self.declarations
        ):
            return None
        return owner.receiver_class

    def _set_directives(self, scope: _MutableScope, statements: list[ast.stmt]) -> None:
        for statement in statements:
            for node in _walk_without_nested_scopes(statement):
                if isinstance(node, ast.Global):
                    scope.globals.update(node.names)
                elif isinstance(node, ast.Nonlocal):
                    scope.nonlocals.update(node.names)

    def _visit_statements(self, statements: list[ast.stmt], scope: _MutableScope) -> None:
        self._set_directives(scope, statements)
        for statement in statements:
            self._visit(statement, scope)

    def _visit(self, node: ast.AST, scope: _MutableScope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._declare(scope, node.name, "function", self._header_name_range(node))
            for decorator in node.decorator_list:
                self._visit(decorator, scope)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self._visit(default, scope)
            child = self._new_scope("function", node, scope)
            type_params = tuple(getattr(node, "type_params", ()))
            self._declare_type_parameters(type_params, child)
            annotation_scope = child if type_params else scope
            if node.returns is not None:
                self._visit_annotation(node.returns, annotation_scope)
            self._configure_method_receiver(node, child, scope)
            self._set_directives(child, node.body)
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                self._declare(
                    child,
                    argument.arg,
                    "parameter",
                    self._argument_name_range(argument),
                    annotation=ast.unparse(argument.annotation)
                    if argument.annotation is not None
                    else None,
                )
                if argument.annotation is not None:
                    self._visit_annotation(argument.annotation, annotation_scope)
            for optional_argument in (node.args.vararg, node.args.kwarg):
                if optional_argument is not None:
                    self._declare(
                        child,
                        optional_argument.arg,
                        "parameter",
                        self._argument_name_range(optional_argument),
                        annotation=ast.unparse(optional_argument.annotation)
                        if optional_argument.annotation is not None
                        else None,
                    )
                    if optional_argument.annotation is not None:
                        self._visit_annotation(optional_argument.annotation, annotation_scope)
            for statement in node.body:
                self._visit(statement, child)
            return

        if isinstance(node, ast.ClassDef):
            self._declare(scope, node.name, "class", self._header_name_range(node))
            for decorator in node.decorator_list:
                self._visit(decorator, scope)
            child = self._new_scope("class", node, scope)
            type_params = tuple(getattr(node, "type_params", ()))
            self._declare_type_parameters(type_params, child)
            base_scope = child if type_params else scope
            for base in node.bases:
                self._visit(base, base_scope)
            for keyword in node.keywords:
                self._visit(keyword.value, base_scope)
            self._visit_statements(node.body, child)
            return

        if _is_type_alias(node):
            alias_name = getattr(node, "name", None)
            if isinstance(alias_name, ast.Name):
                self._declare(
                    scope,
                    alias_name.id,
                    "type_alias",
                    self.document.ast_range(alias_name),
                )
            child = self._new_scope("type_alias", node, scope)
            self._declare_type_parameters(tuple(getattr(node, "type_params", ())), child)
            value = getattr(node, "value", None)
            if isinstance(value, ast.AST):
                self._visit(value, child)
            return

        if isinstance(node, ast.Lambda):
            child = self._new_scope("lambda", node, scope)
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                self._declare(child, argument.arg, "parameter", self._argument_name_range(argument))
            for optional_argument in (node.args.vararg, node.args.kwarg):
                if optional_argument is not None:
                    self._declare(
                        child,
                        optional_argument.arg,
                        "parameter",
                        self._argument_name_range(optional_argument),
                    )
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self._visit(default, scope)
            self._visit(node.body, child)
            return

        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            child = self._new_scope("comprehension", node, scope)
            if node.generators:
                self._visit(node.generators[0].iter, scope)
            for index, generator in enumerate(node.generators):
                if index:
                    self._visit(generator.iter, child)
                self._visit_target(generator.target, child, "loop_target")
                for condition in generator.ifs:
                    self._visit(condition, child)
            if isinstance(node, ast.DictComp):
                self._visit(node.key, child)
                self._visit(node.value, child)
            else:
                self._visit(node.elt, child)
            return

        if isinstance(node, ast.NamedExpr):
            target_scope = scope
            while target_scope.kind == "comprehension" and target_scope.parent is not None:
                target_scope = target_scope.parent
            self._visit(node.value, scope)
            self._visit_target(node.target, target_scope, "variable")
            return

        if isinstance(node, ast.Name):
            source_range = self.document.ast_range(node)
            if isinstance(node.ctx, (ast.Load, ast.Del)):
                self._use(
                    scope,
                    node.id,
                    source_range,
                    is_deletion=isinstance(node.ctx, ast.Del),
                )
            else:
                self._declare(scope, node.id, "variable", source_range)
            return

        if isinstance(node, ast.Attribute):
            self._visit(node.value, scope)
            source_range = self.document.ast_range(node)
            attr_start = SourcePosition(
                source_range.end.line,
                source_range.end.character - len(node.attr),
            )
            attr_range = SourceRange(attr_start, source_range.end)
            receiver = _dotted_expr_name(node.value)
            if receiver in {"self", "cls"}:
                if isinstance(node.ctx, (ast.Load, ast.Del)):
                    self._use(scope, node.attr, attr_range, receiver)
                else:
                    self._declare(
                        scope,
                        node.attr,
                        "variable",
                        attr_range,
                        receiver=receiver,
                    )
            elif isinstance(node.ctx, (ast.Load, ast.Del)):
                self._use(scope, node.attr, attr_range, receiver)
            return

        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".", 1)[0]
                self._declare(
                    scope,
                    name,
                    "import_alias",
                    self._alias_name_range(alias, name),
                    import_source=alias.name,
                )
            return

        if isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                self._declare(
                    scope,
                    name,
                    "from_import_alias",
                    self._alias_name_range(alias, name),
                    import_source=f"{module}:{alias.name}",
                )
            return

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(node, ast.Assign):
                self._visit(node.value, scope)
                for target in node.targets:
                    self._visit_target(target, scope, "variable")
            elif isinstance(node, ast.AnnAssign):
                self._visit_annotation(node.annotation, scope)
                if node.value is not None:
                    self._visit(node.value, scope)
                self._visit_target(
                    node.target,
                    scope,
                    "variable",
                    annotation=ast.unparse(node.annotation),
                )
            else:
                self._visit_augmented_target(node.target, scope)
                self._visit(node.value, scope)
            return

        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._visit(node.iter, scope)
            self._visit_target(node.target, scope, "loop_target")
            self._visit_statements(node.body, scope)
            self._visit_statements(node.orelse, scope)
            return

        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self._visit(item.context_expr, scope)
                if item.optional_vars is not None:
                    self._visit_target(item.optional_vars, scope, "with_target")
            self._visit_statements(node.body, scope)
            return

        if isinstance(node, ast.ExceptHandler):
            if node.type is not None:
                self._visit(node.type, scope)
            if node.name is not None:
                self._declare(
                    scope,
                    node.name,
                    "exception_target",
                    self._text_name_range(node, node.name),
                )
            self._visit_statements(node.body, scope)
            return

        if isinstance(node, ast.Match):
            self._visit(node.subject, scope)
            for case in node.cases:
                self._visit_pattern(case.pattern, scope)
                if case.guard is not None:
                    self._visit(case.guard, scope)
                self._visit_statements(case.body, scope)
            return

        if isinstance(node, (ast.Global, ast.Nonlocal)):
            seen: dict[str, int] = {}
            for name in node.names:
                occurrence = seen.get(name, 0)
                self._use(
                    scope,
                    name,
                    self._text_name_range(node, name, occurrence=occurrence),
                )
                seen[name] = occurrence + 1
            return

        for ast_child in ast.iter_child_nodes(node):
            self._visit(ast_child, scope)

    def _configure_method_receiver(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        child: _MutableScope,
        parent: _MutableScope,
    ) -> None:
        if parent.kind != "class":
            return
        positional = (*node.args.posonlyargs, *node.args.args)
        if not positional:
            return
        decorator_names = {_dotted_expr_name(item) for item in node.decorator_list}
        if "staticmethod" in decorator_names:
            return
        expected = "cls" if "classmethod" in decorator_names else "self"
        if positional[0].arg != expected:
            return
        child.receiver_name = expected
        child.receiver_class = parent

    def _visit_augmented_target(self, target: ast.expr, scope: _MutableScope) -> None:
        """Record the single read and write performed by an augmented target."""

        if isinstance(target, ast.Name):
            source_range = self.document.ast_range(target)
            self._use(scope, target.id, source_range)
            self._declare(scope, target.id, "variable", source_range)
            return
        if isinstance(target, ast.Attribute):
            self._visit(target.value, scope)
            source_range = self.document.ast_range(target)
            end = source_range.end
            attr_range = SourceRange(
                SourcePosition(end.line, end.character - len(target.attr)), end
            )
            receiver = _dotted_expr_name(target.value)
            if receiver in {"self", "cls"}:
                self._use(scope, target.attr, attr_range, receiver)
                self._declare(
                    scope,
                    target.attr,
                    "variable",
                    attr_range,
                    receiver=receiver,
                )
            else:
                self._use(scope, target.attr, attr_range, receiver)
            return
        self._visit(target, scope)

    def _visit_annotation(self, annotation: ast.expr, scope: _MutableScope) -> None:
        if not (isinstance(annotation, ast.Constant) and isinstance(annotation.value, str)):
            if isinstance(annotation, (ast.Name, ast.Attribute)):
                self._visit(annotation, scope)
                return
            for child in ast.iter_child_nodes(annotation):
                if isinstance(child, ast.expr):
                    self._visit_annotation(child, scope)
                else:
                    self._visit(child, scope)
            return
        full = self.document.ast_range(annotation)
        if full.start.line != full.end.line:
            return
        segment = self.document.line(full.start.line)[full.start.character : full.end.character]
        value_start = segment.find(annotation.value)
        if value_start < 0:
            return
        try:
            parsed = ast.parse(annotation.value, mode="eval")
        except SyntaxError:
            return
        inner_document = DocumentMap(annotation.value)
        base_character = full.start.character + value_start
        for node in ast.walk(parsed):
            if isinstance(node, ast.Name):
                inner = inner_document.ast_range(node)
                source_range = SourceRange(
                    SourcePosition(full.start.line, base_character + inner.start.character),
                    SourcePosition(full.start.line, base_character + inner.end.character),
                )
                self._use(scope, node.id, source_range)
            elif isinstance(node, ast.Attribute):
                inner = inner_document.ast_range(node)
                end = SourcePosition(full.start.line, base_character + inner.end.character)
                start = SourcePosition(end.line, end.character - len(node.attr))
                receiver = _dotted_expr_name(node.value)
                self._use(scope, node.attr, SourceRange(start, end), receiver)

    def _visit_target(
        self,
        target: ast.AST,
        scope: _MutableScope,
        kind: BindingKind,
        *,
        annotation: str | None = None,
    ) -> None:
        if isinstance(target, ast.Name):
            self._declare(
                scope,
                target.id,
                kind,
                self.document.ast_range(target),
                annotation=annotation,
            )
            return
        if isinstance(target, ast.Attribute):
            self._visit(target, scope)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._visit_target(item, scope, kind, annotation=annotation)
            return
        if isinstance(target, ast.Starred):
            self._visit_target(target.value, scope, kind, annotation=annotation)
            return
        if isinstance(target, ast.Subscript):
            # Container and index expressions are reads even though the
            # subscript as a whole is an assignment target.
            self._visit(target, scope)

    def _declare_type_parameters(
        self, parameters: tuple[ast.AST, ...], scope: _MutableScope
    ) -> None:
        for parameter in parameters:
            name = getattr(parameter, "name", None)
            if not isinstance(name, str):
                continue
            self._declare(
                scope,
                name,
                "type_parameter",
                self._text_name_range(parameter, name),
            )
            bound = getattr(parameter, "bound", None)
            if isinstance(bound, ast.AST):
                self._visit(bound, scope)
            default_value = getattr(parameter, "default_value", None)
            if isinstance(default_value, ast.AST):
                self._visit(default_value, scope)

    def _visit_pattern(self, pattern: ast.pattern, scope: _MutableScope) -> None:
        if (
            isinstance(pattern, ast.MatchAs)
            and pattern.name is not None
            or isinstance(pattern, ast.MatchStar)
            and pattern.name is not None
        ):
            self._declare(
                scope,
                pattern.name,
                "pattern_target",
                self._text_name_range(pattern, pattern.name),
            )
        elif isinstance(pattern, ast.MatchMapping) and pattern.rest is not None:
            self._declare(
                scope,
                pattern.rest,
                "pattern_target",
                self._text_name_range(pattern, pattern.rest),
            )
        for child in ast.iter_child_nodes(pattern):
            if isinstance(child, ast.pattern):
                self._visit_pattern(child, scope)
            else:
                self._visit(child, scope)

    def _header_name_range(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> SourceRange:
        return self._text_name_range(node, node.name)

    def _argument_name_range(self, argument: ast.arg) -> SourceRange:
        return self._text_name_range(argument, argument.arg)

    def _alias_name_range(self, alias: ast.alias, name: str) -> SourceRange:
        return self._text_name_range(
            alias,
            name,
            reverse=alias.asname is not None,
        )

    def _text_name_range(
        self,
        node: ast.AST,
        name: str,
        *,
        occurrence: int = 0,
        reverse: bool = False,
    ) -> SourceRange:
        full = self.document.ast_range(node)
        tokens = [
            token
            for token in self._tokens
            if _normalized_identifier(token.string) == name
            and full.start <= SourcePosition(token.start[0] - 1, token.start[1])
            and SourcePosition(token.end[0] - 1, token.end[1]) <= full.end
        ]
        if isinstance(node, ast.ExceptHandler):
            as_tokens = [
                token
                for token in self._tokens
                if token.string == "as"
                and full.start <= SourcePosition(token.start[0] - 1, token.start[1])
                and SourcePosition(token.end[0] - 1, token.end[1]) <= full.end
            ]
            if as_tokens:
                as_end = SourcePosition(as_tokens[0].end[0] - 1, as_tokens[0].end[1])
                tokens = [
                    token
                    for token in tokens
                    if SourcePosition(token.start[0] - 1, token.start[1]) >= as_end
                ]
        if reverse or isinstance(node, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)):
            tokens.reverse()
        if occurrence < len(tokens):
            token = tokens[occurrence]
            return SourceRange(
                SourcePosition(token.start[0] - 1, token.start[1]),
                SourcePosition(token.end[0] - 1, token.end[1]),
            )
        return SourceRange(
            full.start,
            SourcePosition(full.start.line, full.start.character + len(name)),
        )


def _walk_without_nested_scopes(root: ast.AST) -> tuple[ast.AST, ...]:
    found: list[ast.AST] = []

    def walk(node: ast.AST) -> None:
        found.append(node)
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(root)
    return tuple(found)


def _position_payload(position: SourcePosition) -> PositionPayload:
    return position.line, position.character


def _range_payload(source_range: SourceRange) -> RangePayload:
    return _position_payload(source_range.start), _position_payload(source_range.end)


def _decode_position(payload: PositionPayload) -> SourcePosition:
    return SourcePosition(*payload)


def _decode_range(payload: RangePayload) -> SourceRange:
    return SourceRange(_decode_position(payload[0]), _decode_position(payload[1]))


@query
def scope_tree_payload(db: Database, path: str) -> ScopeTreePayload:
    source = source_text(db, path)
    try:
        parsed = ast.parse(source, filename=path)
    except SyntaxError:
        return path, tuple(), tuple(), tuple()
    result = _ScopeBuilder(path, source, parsed).build()
    scopes = tuple(
        (item.id, item.kind, _range_payload(item.range), item.parent_id) for item in result.scopes
    )
    bindings = tuple(
        (
            item.symbol_id.scope_id,
            item.name,
            item.kind,
            item.scope_id,
            _range_payload(item.range),
            item.annotation,
            item.import_source,
        )
        for item in result.bindings
    )
    occurrences = tuple(
        (
            item.name,
            _range_payload(item.range),
            item.symbol_id.scope_id if item.symbol_id is not None else None,
            item.is_declaration,
            item.receiver,
            item.is_deletion,
        )
        for item in result.occurrences
    )
    return path, scopes, bindings, occurrences


# Every payload below is declared as nested tuples of primitives, and `freeze`
# leaves such a value as plain tuples, so what `db.get` hands back in any mode is
# already the payload. Thawing it again only walks and copies the whole tree --
# on a workspace-sized request that copy dominated the cost of decoding.
def _decode_scope_tree(payload: ScopeTreePayload) -> ScopeTree:
    result_path, scopes_payload, bindings_payload, occurrences_payload = payload
    scopes = tuple(
        Scope(scope_id, kind, _decode_range(source_range), parent_id)
        for scope_id, kind, source_range, parent_id in scopes_payload
    )
    binding_by_key: dict[tuple[str, str], SymbolId] = {}
    bindings: list[Binding] = []
    for (
        symbol_scope,
        name,
        kind,
        scope_id,
        source_range,
        annotation,
        import_source,
    ) in bindings_payload:
        decoded_range = _decode_range(source_range)
        symbol_id = SymbolId(result_path, symbol_scope, name, decoded_range)
        binding_by_key[(symbol_scope, name)] = symbol_id
        bindings.append(
            Binding(symbol_id, name, kind, scope_id, decoded_range, annotation, import_source)
        )
    occurrences = tuple(
        SymbolOccurrence(
            name,
            _decode_range(source_range),
            binding_by_key.get((symbol_scope, name)) if symbol_scope is not None else None,
            is_declaration,
            receiver,
            is_deletion,
        )
        for (
            name,
            source_range,
            symbol_scope,
            is_declaration,
            receiver,
            is_deletion,
        ) in occurrences_payload
    )
    return ScopeTree(result_path, scopes, tuple(bindings), occurrences)


def scope_tree(db: Database, path: str | os.PathLike[str]) -> ScopeTree:
    normalized = str(Path(path).resolve(strict=False))
    payload = db.get(scope_tree_payload, normalized)
    return decoded("scope_tree", (payload,), lambda: _decode_scope_tree(payload))


@overload
def symbol_at(
    db: Database,
    root_or_path: str | os.PathLike[str],
    path_or_position: SourcePosition,
) -> SymbolId | None: ...


@overload
def symbol_at(
    db: Database,
    root_or_path: str | os.PathLike[str],
    path_or_position: str | os.PathLike[str],
    position: SourcePosition,
) -> SymbolId | None: ...


def symbol_at(
    db: Database,
    root_or_path: str | os.PathLike[str],
    path_or_position: str | os.PathLike[str] | SourcePosition,
    position: SourcePosition | None = None,
) -> SymbolId | None:
    """Resolve the lexical symbol covering ``position``.

    Passing a workspace root enables conservative cross-module resolution for
    direct imports and module attributes. The two-argument form is purely local.
    """

    if position is None:
        if not isinstance(path_or_position, SourcePosition):
            raise TypeError("symbol_at(db, path, position) requires SourcePosition")
        root: str | None = None
        path = os.fspath(root_or_path)
        actual_position = path_or_position
    else:
        if isinstance(path_or_position, SourcePosition):
            raise TypeError("symbol_at(db, root, path, position) requires a path")
        root = os.fspath(root_or_path)
        path = os.fspath(path_or_position)
        actual_position = position

    tree = scope_tree(db, path)
    occurrence = tree.occurrence_at(actual_position)
    if occurrence is None:
        return None
    if root is None:
        return occurrence.symbol_id

    if occurrence.receiver is not None and occurrence.receiver not in {"self", "cls"}:
        # Attribute occurrences must never fall back to resolving the attribute
        # name as an unrelated bare name. If the receiver chain is shadowed,
        # rebound, or otherwise unproven, the conservative result is no symbol.
        return _resolve_attribute(db, root, path, tree, occurrence)

    symbol_id = occurrence.symbol_id
    if symbol_id is None:
        from pyinc.integrations.symbol_resolution import resolve_qualified_name

        return _symbol_id_for_resolved(db, resolve_qualified_name(db, root, path, occurrence.name))
    binding = next((item for item in tree.bindings if item.symbol_id == symbol_id), None)
    if binding is None:
        return symbol_id
    if binding.kind == "import_alias":
        return None
    if binding.kind != "from_import_alias":
        return symbol_id

    from pyinc.integrations.symbol_resolution import resolve_qualified_name

    return _symbol_id_for_resolved(db, resolve_qualified_name(db, root, path, binding.name))


def _symbol_id_for_resolved(db: Database, resolved: object) -> SymbolId | None:
    defining_path = getattr(resolved, "defining_path", None)
    qualified_name = getattr(resolved, "qualified_name", None)
    if not isinstance(defining_path, str) or not isinstance(qualified_name, str):
        return None
    name = qualified_name.rsplit(".", 1)[-1]
    tree = scope_tree(db, defining_path)
    candidates = [item.symbol_id for item in tree.bindings if item.name == name]
    defining_range = getattr(resolved, "range", None)
    if isinstance(defining_range, SourceRange):
        candidates = [
            item for item in candidates if item.declaration.start.line == defining_range.start.line
        ] or candidates
    return candidates[0] if candidates else None


def _resolve_attribute(
    db: Database,
    root: str,
    path: str,
    tree: ScopeTree,
    occurrence: SymbolOccurrence,
) -> SymbolId | None:
    """Resolve only statically proven module, class, or annotated attributes."""

    from pyinc.integrations.symbol_resolution import module_symbol_table, resolve_qualified_name

    receiver_chain = occurrence.receiver
    if not receiver_chain:
        return None
    receiver_parts = receiver_chain.split(".")
    root_name = receiver_parts[0]
    receiver_occurrences = [
        item
        for item in tree.occurrences
        if item.name == root_name
        and item.range.end.line == occurrence.range.start.line
        and item.range.end <= occurrence.range.start
        and item.symbol_id is not None
    ]
    if not receiver_occurrences:
        return None
    receiver_occurrences.sort(key=lambda item: item.range.end, reverse=True)
    receiver_id = receiver_occurrences[0].symbol_id
    binding = next((item for item in tree.bindings if item.symbol_id == receiver_id), None)
    if binding is None:
        return None
    receiver_use = receiver_occurrences[0]
    if any(
        item.is_declaration
        and item.symbol_id == receiver_id
        and binding.range.start < item.range.start < receiver_use.range.start
        for item in tree.occurrences
    ):
        return None
    if any(
        item.is_deletion
        and item.symbol_id == receiver_id
        and binding.range.start < item.range.start < receiver_use.range.start
        for item in tree.occurrences
    ):
        return None

    consumed_parts = 1
    if binding.kind in {"import_alias", "from_import_alias", "class"}:
        receiver = resolve_qualified_name(db, root, path, root_name)
        if binding.kind == "import_alias" and binding.import_source is not None:
            imported_parts = binding.import_source.split(".")
            if root_name == imported_parts[0]:
                if receiver_parts[: len(imported_parts)] != imported_parts:
                    return None
                consumed_parts = len(imported_parts)
    elif binding.annotation is not None:
        annotation = binding.annotation.strip("'\"")
        receiver = resolve_qualified_name(db, root, path, annotation)
    else:
        return None

    for member_name in (*receiver_parts[consumed_parts:], occurrence.name):
        if receiver.resolution != "workspace" or receiver.defining_path is None:
            return None
        receiver_path = receiver.defining_path
        if receiver.range is None:
            receiver = resolve_qualified_name(db, root, receiver_path, member_name)
            continue
        table = module_symbol_table(db, root, receiver_path)
        receiver_symbol = next(
            (
                item
                for item in table.symbols
                if item.range == receiver.range
                and item.qualified_name == receiver.qualified_name
                and item.kind == "class"
            ),
            None,
        )
        if receiver_symbol is None:
            return None
        receiver = resolve_qualified_name(
            db,
            root,
            receiver_path,
            f"{receiver_symbol.qualified_name}.{member_name}",
        )

    return _symbol_id_for_resolved(db, receiver)


__all__ = [
    "Binding",
    "BindingKind",
    "Scope",
    "ScopeKind",
    "ScopeTree",
    "SymbolId",
    "SymbolOccurrence",
    "scope_tree",
    "symbol_at",
]
