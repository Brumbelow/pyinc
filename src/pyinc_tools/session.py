from __future__ import annotations

import ast
import contextlib
import keyword
import os
import tempfile
import threading
import tokenize
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TypeAlias

from pyinc import Database
from pyinc.integrations import (
    Binding,
    ClassMember,
    ClassModel,
    ConfigAnalysis,
    DependencyCheckAnalysis,
    DependencyStatus,
    DependencySurface,
    Diagnostic,
    ModuleSymbolTable,
    PythonModuleAnalysis,
    PythonWorkspaceAnalysis,
    Reference,
    ReferenceQueryResult,
    RequirementsAnalysis,
    ResolvedImportRef,
    Scope,
    ScopeTree,
    Signature,
    SourcePosition,
    SourceRange,
    Symbol,
    SymbolId,
    WorkspaceSymbolIndex,
    class_model,
    find_references,
    module_analysis,
    module_symbol_table,
    request_inputs_changed,
    request_scope,
    scope_tree,
    workspace_analysis,
    workspace_config_analysis,
    workspace_dependency_check,
    workspace_requirements_analysis,
    workspace_symbol_index,
)
from pyinc.integrations import (
    symbol_at as resolve_symbol_at,
)

from ._analysis import (
    _BINDING_TO_COMPLETION_KIND,
    _CLASS_MEMBER_KINDS,
    _COMPLETION_LIMIT,
    _INSTANCE_MEMBER_KINDS,
    _SYMBOL_TO_COMPLETION_KIND,
    _annotation_expr_for_name_at,
    _annotation_type_positions,
    _build_signature_label,
    _call_func_range,
    _collect_annotation_type_refs,
    _collect_outgoing_calls,
    _compute_document_links,
    _compute_folding_ranges,
    _compute_selection_chain,
    _compute_semantic_tokens,
    _enclosing_callable_qname,
    _enclosing_method_context,
    _expression_name_position,
    _find_call_at_position,
    _find_callable_node,
    _find_completion_context,
    _inlay_hints_for_call,
    _keyword_completions,
    _normalize_dependency_name,
    _normalized_name_offsets_on_line,
    _parameter_defaults_from_source,
    _parse_python,
    _repair_caret_line,
    _source_parses,
    _unwrap_base_expression,
    _walk_class_definitions,
    from_import_semantic_token_types,
)
from ._analysis import (
    resolve_target as _resolve_target,
)
from ._analysis import (
    target_from_symbol_id as _target_from_symbol_id,
)
from ._edits import (
    _alias_list_deletion_edits,
    _find_from_module_span,
    _import_node_for_line,
    _relative_import_anchor,
    _resolve_import_from_target,
    _statement_line_span,
    _static_module_all_names,
)
from ._models import (
    AnalysisDiagnostic as AnalysisDiagnostic,
)
from ._models import (
    CallHierarchyCallSite as CallHierarchyCallSite,
)
from ._models import (
    CallHierarchyIncomingCall as CallHierarchyIncomingCall,
)
from ._models import (
    CallHierarchyItem as CallHierarchyItem,
)
from ._models import (
    CallHierarchyItemKind as CallHierarchyItemKind,
)
from ._models import (
    CallHierarchyOutgoingCall as CallHierarchyOutgoingCall,
)
from ._models import (
    CodeAction as CodeAction,
)
from ._models import (
    CodeActionEdit as CodeActionEdit,
)
from ._models import (
    CodeActionKind as CodeActionKind,
)
from ._models import (
    CodeLens as CodeLens,
)
from ._models import (
    CompletionItem as CompletionItem,
)
from ._models import (
    CompletionItemKind as CompletionItemKind,
)
from ._models import (
    DeclarationLocation as DeclarationLocation,
)
from ._models import (
    DependencyInputs as _DependencyInputs,
)
from ._models import (
    DiagnosticSeverity as DiagnosticSeverity,
)
from ._models import (
    DocumentHighlight as DocumentHighlight,
)
from ._models import (
    DocumentHighlightKind as DocumentHighlightKind,
)
from ._models import (
    DocumentLink as DocumentLink,
)
from ._models import (
    FileAnalysisResult as FileAnalysisResult,
)
from ._models import (
    FileDeletionEdit as FileDeletionEdit,
)
from ._models import (
    FileRenameEdit as FileRenameEdit,
)
from ._models import (
    FoldingRange as FoldingRange,
)
from ._models import (
    FoldingRangeKind as FoldingRangeKind,
)
from ._models import (
    InlayHint as InlayHint,
)
from ._models import (
    InlayHintKind as InlayHintKind,
)
from ._models import (
    LinkedEditingRange as LinkedEditingRange,
)
from ._models import (
    RenameEdit as RenameEdit,
)
from ._models import (
    RenameResult as RenameResult,
)
from ._models import (
    RenameStatus as RenameStatus,
)
from ._models import (
    ResolvedReferenceResult as _ResolvedReferenceResult,
)
from ._models import (
    ResolvedTarget,
)
from ._models import (
    SelectionRange as SelectionRange,
)
from ._models import (
    SemanticToken as SemanticToken,
)
from ._models import (
    SemanticTokenModifier as SemanticTokenModifier,
)
from ._models import (
    SemanticTokenType as SemanticTokenType,
)
from ._models import (
    SignatureHelp as SignatureHelp,
)
from ._models import (
    SignatureParameterInfo as SignatureParameterInfo,
)
from ._models import (
    TypeDefinitionLocation as TypeDefinitionLocation,
)
from ._models import (
    TypeHierarchyItem as TypeHierarchyItem,
)
from ._models import (
    TypeHierarchyItemKind as TypeHierarchyItemKind,
)
from ._models import (
    WorkspaceAnalysisResult as WorkspaceAnalysisResult,
)
from ._workspace import (
    DEFAULT_IGNORED_DIR_NAMES as DEFAULT_IGNORED_DIR_NAMES,
)
from ._workspace import (
    SESSION_CLOSED_MESSAGE,
    WorkspaceMirror,
    _encode_python_text,
)
from ._workspace import (
    PollingWorkspaceWatcher as PollingWorkspaceWatcher,
)

# Diagnostic codes that `code_actions_for_range` can offer a quick fix for.
_CODE_ACTION_CODES = frozenset({"unused-import", "missing-import", "unresolved-symbol"})

# Target module -> the (importing file, imported name) pairs that name it in a
# `from` import. `_reexported_names_for_module` needs one entry of this per file
# it examines; building it once per request keeps the workspace analysis from
# being re-decoded for every file in the workspace.
_ReexportIndex: TypeAlias = dict[str, list[tuple[str, str]]]


def _build_reexport_index(modules: Sequence[PythonModuleAnalysis]) -> _ReexportIndex:
    index: _ReexportIndex = {}
    for analysis in modules:
        for resolved_import in analysis.resolved_imports:
            if resolved_import.kind != "from":
                continue
            resolved_module = resolved_import.resolved_module
            imported_name = resolved_import.imported_name
            if resolved_module is None or imported_name is None:
                continue
            index.setdefault(resolved_module, []).append((analysis.path, imported_name))
    return index


class _RequestLock:
    """The session lock, which also bounds one request's view of the graph.

    Every public method holds this for the whole of its work, and nothing can
    change the mirror or the overlays while it is held, so an integration
    entrypoint asked the same question twice inside one method has to answer the
    same both times. Tying the integrations' per-request memo to the lock is
    what keeps it from outliving that guarantee: outside a session it does not
    exist, so a caller driving the integrations directly still sees its edits.

    The lock holds a kernel request span for the same reason it holds the
    memo: the stability it guarantees is exactly what ``Database.request_span``
    asks a caller to declare, so the several gets a public method fans out to
    share one request and validate each resource once. The methods that do
    rewrite the mirror mid-hold already call ``request_inputs_changed()``,
    which rolls the held span onto a fresh request.
    """

    def __init__(self, db: Database) -> None:
        self._lock = threading.RLock()
        self._db = db
        self._depth = 0
        self._span: AbstractContextManager[None] | None = None
        self._scope: AbstractContextManager[None] | None = None

    def __enter__(self) -> None:
        self._lock.acquire()
        try:
            if self._depth == 0:
                span = self._db.request_span()
                span.__enter__()
                try:
                    scope = request_scope(self._db)
                    scope.__enter__()
                except BaseException:
                    span.__exit__(None, None, None)
                    raise
                self._span = span
                self._scope = scope
            self._depth += 1
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._depth -= 1
        scope = self._scope
        span = self._span
        if self._depth == 0 and scope is not None:
            self._scope = None
            scope.__exit__(None, None, None)
        if self._depth == 0 and span is not None:
            self._span = None
            span.__exit__(None, None, None)
        self._lock.release()


class WorkspaceSession:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        mode: str = "strict",
        ignored_dir_names: tuple[str, ...] | None = None,
        exclude_globs: tuple[str, ...] = (),
    ) -> None:
        root_path = Path(root).resolve(strict=False)
        if not root_path.exists() or not root_path.is_dir():
            raise ValueError(f"{root_path!s} is not an existing workspace directory.")

        self.root = str(root_path)
        self.db = Database(mode=mode)
        self._ignored_dir_names = frozenset(ignored_dir_names or DEFAULT_IGNORED_DIR_NAMES)
        self._exclude_globs = tuple(exclude_globs)
        self._tempdir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
            prefix="pyinc-tools-"
        )
        mirror_root_path = Path(self._tempdir.name, "workspace")
        mirror_root_path.mkdir(parents=True, exist_ok=True)
        mirror_root_path = mirror_root_path.resolve(strict=True)
        self.mirror_root = str(mirror_root_path)
        self._mirror = WorkspaceMirror(
            self.root,
            self.mirror_root,
            self._ignored_dir_names,
            self._exclude_globs,
        )
        self._overlays: dict[str, str] = {}
        self._scheduled_paths: set[str] = set()
        self._state_lock = _RequestLock(self.db)
        self._watchers: set[PollingWorkspaceWatcher] = set()
        self._close_complete = threading.Event()
        self._closed = False
        self._mirror.copy_workspace()

    def close(self) -> None:
        with self._state_lock:
            if self._close_complete.is_set():
                return

            if self._closed:
                close_complete = self._close_complete
                # A watcher callback can race another thread that is closing the
                # session and joining that watcher. Waiting here would make the
                # two threads wait on each other.
                if any(watcher._runs_in_current_thread() for watcher in self._watchers):
                    return
                should_close = False
                watchers: tuple[PollingWorkspaceWatcher, ...] = ()
            else:
                self._closed = True
                close_complete = self._close_complete
                should_close = True
                watchers = tuple(self._watchers)

        if not should_close:
            close_complete.wait()
            return

        try:
            # Wake every watcher before joining any one of them. No session lock
            # is held while joining because a watcher may be finishing a refresh.
            for watcher in watchers:
                watcher._request_stop()
            for watcher in watchers:
                watcher.stop()
            self._tempdir.cleanup()
        finally:
            close_complete.set()

    def _register_watcher(self, watcher: PollingWorkspaceWatcher) -> None:
        with self._state_lock:
            self._check_open()
            self._watchers.add(watcher)

    def _unregister_watcher(self, watcher: PollingWorkspaceWatcher) -> None:
        with self._state_lock:
            self._watchers.discard(watcher)

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError(SESSION_CLOSED_MESSAGE)

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
            mirror_path.write_bytes(_encode_python_text(text))
            request_inputs_changed()
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
            result = self._build_file_result(real_path, dependency_inputs, dependency_check)
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
            workspace = workspace_analysis(self.db, self.mirror_root)
            python_analysis = self._remap_workspace_analysis(workspace)
            symbol_index = self._remap_workspace_symbol_index(
                workspace_symbol_index(self.db, self.mirror_root)
            )
            reexports = _build_reexport_index(workspace.modules)
            files = tuple(
                self._build_file_result(
                    module.path,
                    dependency_inputs,
                    dependency_check,
                    module=module,
                    reexports=reexports,
                )
                for module in python_analysis.modules
            )
            diagnostics = self._dedupe_diagnostics(
                tuple(diagnostic for file_result in files for diagnostic in file_result.diagnostics)
                + self._dependency_status_diagnostics(dependency_inputs, dependency_check)
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

    def code_actions_for_range(
        self,
        path: str | os.PathLike[str],
        start_line: int,
        start_character: int,
        end_line: int,
        end_character: int,
    ) -> tuple[CodeAction, ...]:
        """Quick fixes anchored to diagnostics intersecting a line range.

        Anchoring is line-granular (``start_character`` / ``end_character``
        are accepted for LSP shape parity but not used to trim the match):
        every diagnostic whose line falls within ``[start_line, end_line]``
        and whose code is fixable contributes its actions. The file is parsed
        once; when it does not parse, no actions are produced (every fix needs
        the AST). All actions are ``kind == "quickfix"``.
        """
        del start_character, end_character
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            result = self.analyze_file(real_path)
            anchors = [
                diagnostic
                for diagnostic in result.diagnostics
                if diagnostic.code in _CODE_ACTION_CODES
                and diagnostic.range is not None
                and start_line <= diagnostic.range.start.line <= end_line
            ]
            if not anchors:
                return ()
            source = self.source_text(real_path)
            if source is None:
                return ()
            try:
                tree = _parse_python(source)
            except SyntaxError:
                return ()
            import_nodes: list[ast.Import | ast.ImportFrom] = [
                node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            mirror_path = str(self._mirror_path_for_real(real_path))
            module_result = result.module

            actions: list[CodeAction] = []
            seen: set[tuple[str, tuple[tuple[str, SourceRange, str], ...]]] = set()
            for diagnostic in anchors:
                assert diagnostic.range is not None
                import_node = _import_node_for_line(import_nodes, diagnostic.range.start.line + 1)
                if import_node is None:
                    continue
                if diagnostic.code == "unused-import":
                    built = self._unused_import_actions(real_path, source, import_node, diagnostic)
                elif diagnostic.code == "missing-import":
                    built = self._missing_import_actions(
                        real_path, module_result, source, import_node, diagnostic
                    )
                else:  # unresolved-symbol
                    built = self._unresolved_symbol_actions(
                        real_path, mirror_path, source, import_node, diagnostic
                    )
                for action in built:
                    fingerprint = (
                        action.title,
                        tuple(
                            (
                                edit.path,
                                edit.range,
                                edit.new_text,
                            )
                            for edit in action.edits
                        ),
                    )
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    actions.append(action)
            return tuple(actions)

    def _alias_removal_edits(
        self,
        importer_path: str,
        source: str,
        node: ast.Import | ast.ImportFrom,
        dead_index: int,
    ) -> list[CodeActionEdit]:
        if len(node.names) == 1:
            span = _statement_line_span(source, node)
            if span is None:
                return []
            start_line, end_line = span
            return [
                CodeActionEdit(
                    path=importer_path,
                    range=SourceRange(
                        SourcePosition(start_line, 0),
                        SourcePosition(end_line, 0),
                    ),
                )
            ]
        return [
            CodeActionEdit(
                path=edit.path,
                range=edit.range,
            )
            for edit in _alias_list_deletion_edits(
                importer_path=importer_path,
                source=source,
                aliases=node.names,
                dead_indices=[dead_index],
            )
        ]

    def _whole_statement_edit(
        self, importer_path: str, source: str, node: ast.stmt
    ) -> CodeActionEdit | None:
        span = _statement_line_span(source, node)
        if span is None:
            return None
        start_line, end_line = span
        return CodeActionEdit(
            path=importer_path,
            range=SourceRange(
                SourcePosition(start_line, 0),
                SourcePosition(end_line, 0),
            ),
        )

    def _unused_import_actions(
        self,
        importer_path: str,
        source: str,
        node: ast.Import | ast.ImportFrom,
        diagnostic: AnalysisDiagnostic,
    ) -> list[CodeAction]:
        if not isinstance(node, ast.ImportFrom):
            return []
        # The diagnostic anchors at the alias's (lineno, col_offset). Matching
        # on both is required for parenthesised multi-line imports, where every
        # alias shares one column and only the line disambiguates them.
        dead_index = next(
            (
                i
                for i, alias in enumerate(node.names)
                if diagnostic.range is not None
                and alias.lineno == diagnostic.range.start.line + 1
                and alias.col_offset == diagnostic.range.start.character
            ),
            None,
        )
        if dead_index is None:
            return []
        alias = node.names[dead_index]
        binding = alias.asname or alias.name
        edits = self._alias_removal_edits(importer_path, source, node, dead_index)
        if not edits:
            return []
        return [
            CodeAction(
                title=f"Remove unused import {binding!r}",
                kind="quickfix",
                diagnostic=diagnostic,
                edits=tuple(edits),
            )
        ]

    def _missing_import_actions(
        self,
        importer_path: str,
        module_result: PythonModuleAnalysis | None,
        source: str,
        node: ast.Import | ast.ImportFrom,
        diagnostic: AnalysisDiagnostic,
    ) -> list[CodeAction]:
        edits: list[CodeActionEdit]
        if isinstance(node, ast.ImportFrom):
            # The from-module itself is unresolvable — the whole statement goes.
            edit = self._whole_statement_edit(importer_path, source, node)
            edits = [edit] if edit is not None else []
        else:
            if module_result is None:
                return []
            missing_modules = {
                resolved_import.module
                for resolved_import in module_result.resolved_imports
                if diagnostic.range is not None
                and resolved_import.range.start.line == diagnostic.range.start.line
                and resolved_import.resolution == "missing"
            }
            dead_indices = [
                i for i, alias in enumerate(node.names) if alias.name in missing_modules
            ]
            if not dead_indices:
                return []
            if len(dead_indices) == len(node.names):
                edit = self._whole_statement_edit(importer_path, source, node)
                edits = [edit] if edit is not None else []
            else:
                edits = [
                    CodeActionEdit(
                        path=deletion.path,
                        range=deletion.range,
                    )
                    for deletion in _alias_list_deletion_edits(
                        importer_path=importer_path,
                        source=source,
                        aliases=node.names,
                        dead_indices=dead_indices,
                    )
                ]
        if not edits:
            return []
        return [
            CodeAction(
                title="Remove unresolvable import",
                kind="quickfix",
                diagnostic=diagnostic,
                edits=tuple(edits),
            )
        ]

    def _unresolved_symbol_actions(
        self,
        importer_path: str,
        mirror_path: str,
        source: str,
        node: ast.Import | ast.ImportFrom,
        diagnostic: AnalysisDiagnostic,
    ) -> list[CodeAction]:
        if not isinstance(node, ast.ImportFrom):
            return []
        actions: list[CodeAction] = []
        for i, alias in enumerate(node.names):
            if alias.name == "*":
                continue
            resolved = _resolve_target(self.db, self.mirror_root, mirror_path, alias.name)
            if resolved.resolution != "missing":
                continue
            binding = alias.asname or alias.name
            removal = self._alias_removal_edits(importer_path, source, node, i)
            if removal:
                actions.append(
                    CodeAction(
                        title=f"Remove import of {binding!r}",
                        kind="quickfix",
                        diagnostic=diagnostic,
                        edits=tuple(removal),
                    )
                )
            retarget = self._retarget_from_module_action(
                importer_path, source, node, alias.name, diagnostic
            )
            if retarget is not None:
                actions.append(retarget)
        return actions

    def _retarget_from_module_action(
        self,
        importer_path: str,
        source: str,
        node: ast.ImportFrom,
        name: str,
        diagnostic: AnalysisDiagnostic,
    ) -> CodeAction | None:
        # Only a single-name statement can be retargeted without breaking a
        # sibling import that still resolves against the current from-module.
        if len(node.names) != 1:
            return None
        index = workspace_symbol_index(self.db, self.mirror_root)
        modules = {
            entry.module
            for entry in index.entries
            if entry.qualified_name == name
            and "." not in entry.qualified_name
            and entry.kind in ("function", "class", "variable")
        }
        if len(modules) != 1:
            return None
        target_module = next(iter(modules))
        located = _find_from_module_span(source.splitlines(), node)
        if located is None:
            return None
        line_idx, start_col, end_col = located
        edit = CodeActionEdit(
            path=importer_path,
            range=SourceRange(
                SourcePosition(line_idx, start_col),
                SourcePosition(line_idx, end_col),
            ),
            new_text=target_module,
        )
        return CodeAction(
            title=f"Import {name!r} from {target_module!r}",
            kind="quickfix",
            diagnostic=diagnostic,
            edits=(edit,),
        )

    def symbol_at(
        self,
        path: str | os.PathLike[str],
        position: SourcePosition,
    ) -> SymbolId | None:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            symbol_id = resolve_symbol_at(self.db, self.mirror_root, str(mirror_path), position)
            if symbol_id is None:
                return None
            return SymbolId(
                self._remap_path(symbol_id.path) or symbol_id.path,
                symbol_id.scope_id,
                symbol_id.name,
                symbol_id.declaration,
            )

    def _resolved_target_at(
        self,
        mirror_path: Path,
        position: SourcePosition,
    ) -> ResolvedTarget | None:
        lexical = scope_tree(self.db, str(mirror_path))
        occurrence = lexical.occurrence_at(position)
        if occurrence is None:
            return None
        if occurrence.receiver and occurrence.receiver not in {"self", "cls"}:
            root_name = occurrence.receiver.split(".", 1)[0]
            roots = [
                item
                for item in lexical.occurrences
                if item.name == root_name
                and item.range.end.line == occurrence.range.start.line
                and item.range.end <= occurrence.range.start
            ]
            roots.sort(key=lambda item: item.range.end, reverse=True)
            if not roots or roots[0].symbol_id is None:
                return None
            binding = next(
                (item for item in lexical.bindings if item.symbol_id == roots[0].symbol_id),
                None,
            )
            if binding is None:
                return None
            if (
                binding.kind not in {"import_alias", "from_import_alias", "class"}
                and binding.annotation is None
            ):
                return None
            if any(
                item.is_declaration
                and item.symbol_id == binding.symbol_id
                and binding.range.start < item.range.start < roots[0].range.start
                for item in lexical.occurrences
            ):
                return None

        symbol_id = resolve_symbol_at(
            self.db,
            self.mirror_root,
            str(mirror_path),
            position,
        )
        if symbol_id is not None:
            return self._remap_resolved_target(
                _target_from_symbol_id(self.db, self.mirror_root, symbol_id)
            )
        if occurrence.symbol_id is None:
            return None
        binding = next(
            (item for item in lexical.bindings if item.symbol_id == occurrence.symbol_id),
            None,
        )
        if binding is None or binding.kind != "from_import_alias":
            return None
        return self._remap_resolved_target(
            _resolve_target(
                self.db,
                self.mirror_root,
                str(mirror_path),
                binding.name,
            )
        )

    def _local_symbol_at(
        self,
        path: str | os.PathLike[str],
        position: SourcePosition,
    ) -> SymbolId | None:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            symbol_id = scope_tree(self.db, str(mirror_path)).symbol_at(position)
            if symbol_id is None:
                return None
            return SymbolId(
                self._remap_path(symbol_id.path) or symbol_id.path,
                symbol_id.scope_id,
                symbol_id.name,
                symbol_id.declaration,
            )

    def _local_binding_at(
        self,
        path: str | os.PathLike[str],
        position: SourcePosition,
    ) -> Binding | None:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            tree = scope_tree(self.db, str(mirror_path))
            symbol_id = tree.symbol_at(position)
            if symbol_id is None:
                return None
            return next(
                (binding for binding in tree.bindings if binding.symbol_id == symbol_id),
                None,
            )

    def find_references(
        self,
        symbol_id: SymbolId,
        *,
        include_declaration: bool = True,
    ) -> ReferenceQueryResult:
        with self._state_lock:
            self._check_open()
            target_path = self._mirror_path_for_real(self._normalize_real_path(symbol_id.path))
            if not target_path.exists() or target_path.suffix != ".py":
                raise FileNotFoundError(symbol_id.path)
            mirror_target = SymbolId(
                str(target_path),
                symbol_id.scope_id,
                symbol_id.name,
                symbol_id.declaration,
            )
            result = find_references(
                self.db,
                self.mirror_root,
                mirror_target,
                include_declaration=include_declaration,
            )
            remapped_refs = tuple(
                Reference(
                    path=self._remap_path(ref.path) or ref.path,
                    range=ref.range,
                    is_declaration=ref.is_declaration,
                )
                for ref in result.references
            )
            return ReferenceQueryResult(target=symbol_id, references=remapped_refs)

    def _find_references_by_name(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
        *,
        include_declaration: bool = True,
    ) -> _ResolvedReferenceResult:
        real_path = self._normalize_real_path(path)
        mirror_path = self._mirror_path_for_real(real_path)
        if not mirror_path.exists() or mirror_path.suffix != ".py":
            raise FileNotFoundError(real_path)
        resolved = _resolve_target(
            self.db,
            self.mirror_root,
            str(mirror_path),
            qualified_name,
        )
        remapped_target = self._remap_resolved_target(resolved)
        symbol_id = self._symbol_id_for_resolved(resolved)
        if symbol_id is None:
            return _ResolvedReferenceResult(target=remapped_target, references=())
        real_symbol_id = SymbolId(
            self._remap_path(symbol_id.path) or symbol_id.path,
            symbol_id.scope_id,
            symbol_id.name,
            symbol_id.declaration,
        )
        result = self.find_references(
            real_symbol_id,
            include_declaration=include_declaration,
        )
        return _ResolvedReferenceResult(
            target=remapped_target,
            references=result.references,
        )

    def _name_is_used_in_file(self, mirror_path: str, resolved: ResolvedTarget) -> bool:
        """Is ``resolved`` referenced from inside ``mirror_path`` itself?

        The unused-import check only ever asks about hits in the importing
        file, so it scans that one file's occurrences rather than resolving
        every same-named occurrence in the workspace and then discarding the
        other files' hits. It matches on `find_references`' rule minus the
        ``include_declaration`` filter, so a declaration of the target counts
        as a use here; that can only overreport use, and so never reports a
        live import as unused.
        """
        target = self._symbol_id_for_resolved(resolved)
        if target is None:
            return False
        for occurrence in scope_tree(self.db, mirror_path).occurrences:
            if occurrence.name != target.name:
                continue
            # An import binding names the target but is not itself a
            # declaration or usage of the target symbol.
            if occurrence.is_declaration and occurrence.symbol_id != target:
                continue
            candidate = resolve_symbol_at(
                self.db,
                self.mirror_root,
                mirror_path,
                occurrence.range.start,
            )
            if candidate == target:
                return True
        return False

    def _symbol_id_for_resolved(self, resolved: ResolvedTarget) -> SymbolId | None:
        if (
            resolved.resolution != "workspace"
            or resolved.defining_path is None
            or resolved.range is None
        ):
            return None
        return resolve_symbol_at(
            self.db,
            self.mirror_root,
            resolved.defining_path,
            resolved.range.start,
        )

    def find_document_highlights(
        self,
        path: str | os.PathLike[str],
        symbol_id: SymbolId,
    ) -> tuple[DocumentHighlight, ...]:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            result = self.find_references(symbol_id, include_declaration=True)
            highlights: list[DocumentHighlight] = []
            seen: set[SourceRange] = set()
            for ref in result.references:
                ref_real_path = self._remap_path(ref.path) or ref.path
                if ref_real_path != real_path:
                    continue
                source_range = ref.range
                if source_range in seen:
                    continue
                seen.add(source_range)
                kind: DocumentHighlightKind = "write" if ref.is_declaration else "text"
                highlights.append(
                    DocumentHighlight(
                        range=source_range,
                        kind=kind,
                    )
                )
            highlights.sort(key=lambda highlight: highlight.range.start)
            return tuple(highlights)

    def linked_editing_ranges_at(
        self,
        path: str | os.PathLike[str],
        symbol_id: SymbolId,
    ) -> tuple[LinkedEditingRange, ...]:
        highlights = self.find_document_highlights(path, symbol_id)
        return tuple(LinkedEditingRange(range=highlight.range) for highlight in highlights)

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
            _function_name, active_index, function_position = located
            resolved = self._resolved_target_at(mirror_path, function_position)
            if resolved is None:
                return None
            if resolved.resolution != "workspace":
                return None
            callable_info = self._lookup_callable_signature(resolved)
            if callable_info is None:
                return None
            display_name, signature = callable_info
            defaults = self._signature_defaults(resolved, display_name)
            label, parameters = _build_signature_label(display_name, signature, defaults)
            active_parameter = active_index if 0 <= active_index < len(parameters) else None
            return SignatureHelp(
                label=label,
                parameters=parameters,
                active_parameter=active_parameter,
            )

    def completions_at(
        self,
        path: str | os.PathLike[str],
        line: int,
        character: int,
    ) -> tuple[CompletionItem, ...]:
        """Declaration-driven completion candidates for the caret position.

        Offers bare-name, attribute (``module.``/``class.``), and import
        completions drawn from real symbol-table bindings — never inferred
        runtime types. Returns ``()`` when the caret is inside a string/comment,
        outside the workspace, or otherwise has nothing sensible to offer.
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
            context = _find_completion_context(source, line, character)
            if context is None:
                return ()

            kind = context[0]
            items: list[CompletionItem] = []
            if kind == "name":
                prefix = context[1]
                # Local symbols need the current file to parse; the caret line is
                # usually the only broken part, so analyse a repaired copy.
                with self._repaired_current_file(mirror_path, source, line) as ok:
                    if ok:
                        items += self._local_symbol_completions(
                            mirror_path,
                            prefix,
                            SourcePosition(line, character),
                        )
                items += self._workspace_module_completions(prefix, full=False)
                items += _keyword_completions(prefix)
            elif kind == "attribute":
                owner, prefix = context[1], context[2]
                with self._repaired_current_file(mirror_path, source, line) as ok:
                    if ok:
                        if owner in ("self", "cls"):
                            items += self._self_or_cls_completions(
                                mirror_path, source, line, owner, prefix
                            )
                        else:
                            binding = self._visible_binding(
                                mirror_path,
                                owner.split(".", 1)[0],
                                SourcePosition(line, character),
                            )
                            if (
                                binding is not None
                                and binding.kind != "variable"
                                and self._binding_is_rebound(
                                    mirror_path,
                                    binding,
                                    SourcePosition(line, character),
                                )
                            ):
                                binding = None
                            if binding is not None and binding.kind in (
                                "import_alias",
                                "from_import_alias",
                                "class",
                            ):
                                items += self._attribute_completions(
                                    mirror_path, owner, prefix, binding
                                )
                            if (
                                not items
                                and "." not in owner
                                and binding is not None
                                and binding.annotation is not None
                            ):
                                items += self._annotated_name_completions(
                                    mirror_path, source, line, owner, prefix
                                )
            elif kind == "from_import":
                module, prefix = context[1], context[2]
                items += self._module_member_completions(module, prefix)
            elif kind == "import_module":
                prefix = context[1]
                items += self._workspace_module_completions(prefix, full=True)

            deduped: dict[str, CompletionItem] = {}
            for item in items:
                deduped.setdefault(item.label, item)
            ordered = sorted(deduped.values(), key=lambda c: (c.sort_text, c.label))
            return tuple(ordered[:_COMPLETION_LIMIT])

    @contextlib.contextmanager
    def _repaired_current_file(self, mirror_path: Path, original: str, line: int) -> Iterator[bool]:
        """Temporarily write a caret-line-repaired copy of the current file to
        the mirror so symbol-table and resolution queries can run against a
        parseable buffer, then restore the exact original bytes.

        Yields ``True`` when a repaired, parseable buffer is in place (or the
        original already parsed), ``False`` when even the repaired buffer is
        unparseable. Held under ``self._state_lock`` for its whole lifetime, so
        the transient mirror state is never observed by other threads."""
        if _source_parses(original):
            yield True
            return
        repaired = _repair_caret_line(original, line)
        if not _source_parses(repaired):
            yield False
            return
        mirror_path.write_bytes(_encode_python_text(repaired))
        request_inputs_changed()
        try:
            yield True
        finally:
            mirror_path.write_bytes(_encode_python_text(original))
            request_inputs_changed()

    def _symbol_completion_item(
        self, label: str, symbol: Symbol, sort_group: str
    ) -> CompletionItem | None:
        kind = _SYMBOL_TO_COMPLETION_KIND.get(symbol.kind)
        if kind is None:
            return None
        detail: str | None = None
        if symbol.kind in ("function", "method") and symbol.signature is not None:
            detail, _ = _build_signature_label(label, symbol.signature)
        elif symbol.annotation:
            detail = f"{label}: {symbol.annotation}"
        return CompletionItem(
            label=label, kind=kind, detail=detail, sort_text=f"{sort_group}{label}"
        )

    def _local_symbol_completions(
        self,
        mirror_path: Path,
        prefix: str,
        position: SourcePosition,
    ) -> list[CompletionItem]:
        lexical = scope_tree(self.db, str(mirror_path))
        scope = self._innermost_scope(lexical, position)
        visible: dict[str, Binding] = {}
        while scope is not None:
            for binding in lexical.bindings:
                if binding.scope_id == scope.id:
                    visible.setdefault(binding.name, binding)
            scope = next(
                (item for item in lexical.scopes if item.id == scope.parent_id),
                None,
            )

        table = module_symbol_table(self.db, self.mirror_root, str(mirror_path))
        items: list[CompletionItem] = []
        seen: set[str] = set()
        symbols_by_range = {
            (symbol.qualified_name.rsplit(".", 1)[-1], symbol.range): symbol
            for symbol in table.symbols
        }
        for name, binding in visible.items():
            if not name.startswith(prefix):
                continue
            symbol = symbols_by_range.get((name, binding.range))
            if symbol is not None:
                item = self._symbol_completion_item(name, symbol, sort_group="0")
            else:
                kind = _BINDING_TO_COMPLETION_KIND[binding.kind]
                detail = f"{name}: {binding.annotation}" if binding.annotation else None
                item = CompletionItem(
                    label=name,
                    kind=kind,
                    detail=detail,
                    sort_text=f"0{name}",
                )
            if item is not None:
                items.append(item)
                seen.add(name)
        for symbol in table.symbols:
            name = symbol.qualified_name
            if "." in name or name in seen:  # module-level bindings only
                continue
            if not name.startswith(prefix):
                continue
            item = self._symbol_completion_item(name, symbol, sort_group="0")
            if item is not None:
                items.append(item)
        return items

    def _innermost_scope(
        self,
        lexical: ScopeTree,
        position: SourcePosition,
    ) -> Scope | None:
        scopes = lexical.scopes
        candidates = [scope for scope in scopes if scope.range.contains(position, include_end=True)]
        candidates.sort(
            key=lambda scope: (
                scope.range.end.line - scope.range.start.line,
                scope.range.end.character - scope.range.start.character,
            )
        )
        return candidates[0] if candidates else None

    def _visible_binding(
        self,
        mirror_path: Path,
        name: str,
        position: SourcePosition,
    ) -> Binding | None:
        lexical = scope_tree(self.db, str(mirror_path))
        scope = self._innermost_scope(lexical, position)
        while scope is not None:
            binding = next(
                (
                    item
                    for item in lexical.bindings
                    if item.scope_id == scope.id and item.name == name
                ),
                None,
            )
            if binding is not None:
                return binding
            scope = next(
                (item for item in lexical.scopes if item.id == scope.parent_id),
                None,
            )
        return None

    def _binding_is_rebound(
        self,
        mirror_path: Path,
        binding: Binding,
        position: SourcePosition,
    ) -> bool:
        lexical = scope_tree(self.db, str(mirror_path))
        return any(
            occurrence.is_declaration
            and occurrence.symbol_id == binding.symbol_id
            and occurrence.range != binding.range
            and occurrence.range.start <= position
            for occurrence in lexical.occurrences
        )

    def _workspace_module_completions(self, prefix: str, *, full: bool) -> list[CompletionItem]:
        index = workspace_symbol_index(self.db, self.mirror_root)
        modules = {entry.module for entry in index.entries}
        # ``full`` offers dotted module names (import position); otherwise the
        # top-level package component (bare-name position).
        names = modules if full else {module.split(".")[0] for module in modules}
        items: list[CompletionItem] = []
        for name in names:
            if name and name.startswith(prefix):
                items.append(
                    CompletionItem(label=name, kind="module", detail=None, sort_text=f"2{name}")
                )
        return items

    def _module_member_completions(self, module: str, prefix: str) -> list[CompletionItem]:
        """Top-level names of a workspace ``module`` (for ``from M import ...``)."""
        index = workspace_symbol_index(self.db, self.mirror_root)
        items: list[CompletionItem] = []
        for entry in index.entries:
            if entry.module != module:
                continue
            name = entry.qualified_name
            if "." in name or not name.startswith(prefix):
                continue
            kind = _SYMBOL_TO_COMPLETION_KIND.get(entry.kind, "variable")
            detail = f"{name}: {entry.annotation}" if entry.annotation else None
            items.append(CompletionItem(label=name, kind=kind, detail=detail, sort_text=f"0{name}"))
        return items

    def _attribute_completions(
        self,
        mirror_path: Path,
        owner: str,
        prefix: str,
        binding: Binding,
    ) -> list[CompletionItem]:
        """Members of ``owner`` when it resolves to a workspace module or class.

        A single-component ``owner`` (``M.``) keeps the shared public resolver path.
        A dotted ``owner`` is handled longest-match-first: the whole owner as a
        workspace module (``pkg.sub.``), else ``head.Class`` where ``head`` is a
        workspace module and ``Class`` a class in it (``pkg.sub.C.``, ``M.C.``).
        Instance chains (``obj.attr.``) resolve to nothing — no type inference.
        """
        if "." in owner:
            return self._dotted_owner_completions(mirror_path, owner, prefix, binding)
        return self._bare_owner_completions(mirror_path, owner, prefix)

    def _dotted_owner_completions(
        self,
        mirror_path: Path,
        owner: str,
        prefix: str,
        binding: Binding,
    ) -> list[CompletionItem]:
        # Rule 1: the dotted owner is itself a workspace module.
        if self._is_workspace_module(owner):
            imported = binding.import_source
            if imported is None or (imported != owner and not imported.startswith(f"{owner}.")):
                return []
            return self._module_member_completions(owner, prefix)
        # Rule 2: ``head.Class`` — head is a workspace module, Class a class in it.
        head, _, last = owner.rpartition(".")
        module = self._resolve_owner_module(mirror_path, head)
        if module is None:
            return []
        if binding.kind == "import_alias":
            imported = binding.import_source
            if imported is None or (imported != module and not imported.startswith(f"{module}.")):
                return []
        return self._class_member_completions_from_index(module, last, prefix)

    def _is_workspace_module(self, name: str) -> bool:
        """True when `name` is exactly a module in the workspace symbol index.

        Exact match keeps dotted-owner resolution unambiguous — module names
        are unique per path, so there is no fuzzy ``import`` guessing."""
        index = workspace_symbol_index(self.db, self.mirror_root)
        return any(entry.module == name for entry in index.entries)

    def _resolve_owner_module(self, mirror_path: Path, owner: str) -> str | None:
        """The workspace module `owner` denotes, or ``None``.

        A dotted `owner` matches a module by exact index name; a single bare
        name is resolved through the file's imports (the shared public resolver) and
        accepted only when it lands on a workspace *module* (no source range —
        a specific symbol range would mean a class / function / variable)."""
        if self._is_workspace_module(owner):
            return owner
        if "." in owner:
            return None
        resolved = self._remap_resolved_target(
            _resolve_target(self.db, self.mirror_root, str(mirror_path), owner)
        )
        if (
            resolved.resolution == "workspace"
            and resolved.range is None
            and resolved.defining_module is not None
            and self._is_workspace_module(resolved.defining_module)
        ):
            return resolved.defining_module
        return None

    def _class_member_completions_from_index(
        self, module: str, class_name: str, prefix: str
    ) -> list[CompletionItem]:
        """Members of ``module.class_name`` drawn from the workspace index.

        Returns ``[]`` unless ``class_name`` is actually a class in ``module``."""
        index = workspace_symbol_index(self.db, self.mirror_root)
        member_prefix = f"{class_name}."
        is_class = False
        items: list[CompletionItem] = []
        for entry in index.entries:
            if entry.module != module:
                continue
            qname = entry.qualified_name
            if qname == class_name and entry.kind == "class":
                is_class = True
                continue
            if not qname.startswith(member_prefix) or qname.count(".") != 1:
                continue
            member = qname.split(".", 1)[1]
            if not member.startswith(prefix):
                continue
            kind = _SYMBOL_TO_COMPLETION_KIND.get(entry.kind, "variable")
            detail = f"{member}: {entry.annotation}" if entry.annotation else None
            items.append(
                CompletionItem(label=member, kind=kind, detail=detail, sort_text=f"0{member}")
            )
        return items if is_class else []

    def _bare_owner_completions(
        self, mirror_path: Path, owner: str, prefix: str
    ) -> list[CompletionItem]:
        """Members of a single bare-name ``owner`` via the shared public resolver."""
        resolved = self._remap_resolved_target(
            _resolve_target(self.db, self.mirror_root, str(mirror_path), owner)
        )
        if resolved.resolution != "workspace" or resolved.defining_path is None:
            return []
        defining_mirror = self._mirror_path_for_real(resolved.defining_path)
        if not defining_mirror.exists() or defining_mirror.suffix != ".py":
            return []
        table = module_symbol_table(self.db, self.mirror_root, str(defining_mirror))
        owner_bare = resolved.qualified_name.rsplit(".", 1)[-1]
        owner_symbol = next(
            (
                symbol
                for symbol in table.symbols
                if symbol.qualified_name == owner_bare and "." not in symbol.qualified_name
            ),
            None,
        )

        items: list[CompletionItem] = []
        if owner_symbol is None:
            # ``owner`` is the module itself → offer its top-level bindings.
            for symbol in table.symbols:
                name = symbol.qualified_name
                if "." in name or not name.startswith(prefix):
                    continue
                item = self._symbol_completion_item(name, symbol, sort_group="0")
                if item is not None:
                    items.append(item)
            return items
        if owner_symbol.kind == "class":
            # ``owner`` is a class → offer the flattened CLASS view (own +
            # inherited methods and class vars, no instance attributes) from the
            # `class_model` surface, so `Derived.` sees members from workspace
            # bases just like `self.`/`cls.`/annotated-name completion do.
            model = self._remap_class_model(
                class_model(self.db, self.mirror_root, str(defining_mirror), owner_bare)
            )
            for member in model.members:
                if member.kind not in _CLASS_MEMBER_KINDS or not member.name.startswith(prefix):
                    continue
                items.append(self._class_member_completion_item(member))
            return items
        # Owner is a function/variable/etc — no member completion.
        return []

    def _self_or_cls_completions(
        self,
        mirror_path: Path,
        source: str,
        line: int,
        owner: str,
        prefix: str,
    ) -> list[CompletionItem]:
        """Own members of the class enclosing a ``self.``/``cls.`` caret.

        The enclosing method is resolved from the (caret-line-repaired) buffer;
        the owner identifier must be the method's literal first parameter
        (``self`` → instance view, ``cls`` → class view). The declaration-only
        member set comes from the ``class_model`` integration surface — no type
        inference, own members only (Stage 1)."""
        parse_source = source if _source_parses(source) else _repair_caret_line(source, line)
        try:
            tree = _parse_python(parse_source)
        except SyntaxError:
            return []
        context = _enclosing_method_context(tree, line + 1)
        if context is None:
            return []
        class_qualifier, first_param = context
        if owner != first_param:
            return []
        view = _INSTANCE_MEMBER_KINDS if owner == "self" else _CLASS_MEMBER_KINDS

        model = self._remap_class_model(
            class_model(self.db, self.mirror_root, str(mirror_path), class_qualifier)
        )
        items: list[CompletionItem] = []
        for member in model.members:
            if member.kind not in view or not member.name.startswith(prefix):
                continue
            items.append(self._class_member_completion_item(member))
        return items

    def _class_member_completion_item(self, member: ClassMember) -> CompletionItem:
        kind: CompletionItemKind = "method" if member.kind == "method" else "field"
        detail: str | None = None
        if member.kind == "method" and member.signature is not None:
            detail, _ = _build_signature_label(member.name, member.signature)
        elif member.annotation:
            detail = f"{member.name}: {member.annotation}"
        return CompletionItem(
            label=member.name,
            kind=kind,
            detail=detail,
            sort_text=f"0{member.name}",
        )

    def _annotated_name_completions(
        self,
        mirror_path: Path,
        source: str,
        line: int,
        owner: str,
        prefix: str,
    ) -> list[CompletionItem]:
        """Rule A — instance-view completions for a bare, annotated ``owner``.

        Applies when ``owner`` is neither ``self``/``cls`` nor resolvable by the
        shared public resolver attribute path: its declared annotation is followed to
        a workspace class and that class's instance view (methods + class vars +
        instance vars, via ``class_model``) is offered. The declaration is looked
        up on the caret-line-repaired current buffer only — see
        :func:`_annotation_expr_for_name_at` — falling back to the module-level
        ``variable`` symbol's annotation. No type inference: only the annotation
        shapes accepted by :meth:`_workspace_class_from_annotation` resolve."""
        parse_source = source if _source_parses(source) else _repair_caret_line(source, line)
        try:
            tree = _parse_python(parse_source)
        except SyntaxError:
            return []
        expr = _annotation_expr_for_name_at(tree, line + 1, owner)
        if expr is not None:
            annotation_text: str | None = ast.unparse(expr)
        else:
            annotation_text = self._module_variable_annotation(mirror_path, owner)
        if annotation_text is None:
            return []
        resolved = self._workspace_class_from_annotation(mirror_path, annotation_text)
        if resolved is None or resolved.defining_path is None:
            return []
        defining_mirror = self._mirror_path_for_real(resolved.defining_path)
        if not defining_mirror.exists() or defining_mirror.suffix != ".py":
            return []
        model = self._remap_class_model(
            class_model(
                self.db,
                self.mirror_root,
                str(defining_mirror),
                resolved.qualified_name,
            )
        )
        items: list[CompletionItem] = []
        for member in model.members:
            if member.kind not in _INSTANCE_MEMBER_KINDS or not member.name.startswith(prefix):
                continue
            items.append(self._class_member_completion_item(member))
        return items

    def _module_variable_annotation(self, mirror_path: Path, name: str) -> str | None:
        """Annotation text of the module-level ``variable`` symbol ``name`` in
        the current file, or ``None`` — Rule A's priority-3 declaration lookup."""
        table = module_symbol_table(self.db, self.mirror_root, str(mirror_path))
        for symbol in table.symbols:
            if (
                symbol.qualified_name == name
                and "." not in symbol.qualified_name
                and symbol.kind == "variable"
                and symbol.annotation is not None
            ):
                return symbol.annotation
        return None

    def _workspace_class_from_annotation(
        self, mirror_path: Path, annotation_text: str
    ) -> ResolvedTarget | None:
        """Resolve ``annotation_text`` to a verified workspace ``class`` symbol.

        Accepts a bare ``Name`` (``Foo``) or a one-hop ``Attribute`` of a bare
        ``Name`` (``mod.Foo``); a whole-string forward reference (``"Foo"``,
        ``"mod.Foo"``) is unwrapped exactly once. Subscripted / generic / union /
        deep-dotted / callable shapes resolve to ``None``. ``Foo`` is resolved in
        ``mirror_path``'s module context; ``mod.Foo`` resolves ``mod`` to a
        workspace module then ``Foo`` within it (the ``_resolve_class_target``
        idiom). The target is confirmed a workspace class against its defining
        file's table by the ``(lineno, qualified_name, kind == "class")`` check
        (the ``prepare_type_hierarchy`` idiom); anything else returns ``None``."""
        try:
            body: ast.expr = _parse_python(annotation_text, mode="eval").body
        except SyntaxError:
            return None
        if isinstance(body, ast.Constant) and isinstance(body.value, str):
            try:
                body = _parse_python(body.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(body, ast.Name):
            target: tuple[str, ...] = ("name", body.id)
        elif isinstance(body, ast.Attribute) and isinstance(body.value, ast.Name):
            target = ("attr", body.value.id, body.attr)
        else:
            return None
        resolved = self._resolve_class_target(mirror_path, target)
        if (
            resolved is None
            or resolved.resolution != "workspace"
            or resolved.defining_path is None
            or resolved.range is None
        ):
            return None
        defining_mirror = self._mirror_path_for_real(resolved.defining_path)
        if not defining_mirror.exists() or defining_mirror.suffix != ".py":
            return None
        defining_table = module_symbol_table(self.db, self.mirror_root, str(defining_mirror))
        for symbol in defining_table.symbols:
            if symbol.qualified_name == resolved.qualified_name and symbol.kind == "class":
                return resolved
        return None

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
                    real_path,
                    symbol.range.start.line + 1,
                    symbol.qualified_name,
                )
                if located is None:
                    continue
                start_col, end_col = located
                result = self._find_references_by_name(
                    real_path,
                    symbol.qualified_name,
                    include_declaration=False,
                )
                if (
                    not isinstance(result.target, ResolvedTarget)
                    or result.target.resolution != "workspace"
                ):
                    continue
                count = len(result.references)
                title = f"{count} reference" if count == 1 else f"{count} references"
                lenses.append(
                    CodeLens(
                        range=SourceRange(
                            SourcePosition(symbol.range.start.line, start_col),
                            SourcePosition(symbol.range.start.line, end_col),
                        ),
                        title=title,
                    )
                )
            lenses.sort(key=lambda lens: lens.range.start)
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

        Targets resolved as stdlib / installed / ambiguous / missing,
        unproven or rebound receiver chains, subscripted calls
        (``factory[T](...)``), lambda calls, and files that fail to parse
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
            try:
                tree = _parse_python(source)
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
                    or (func_start_line == range_end_line and func_start_col >= end_character)
                ):
                    continue
                target = self._resolve_call_target(mirror_path, call.func)
                if target is None or target.resolution != "workspace":
                    continue
                callable_info = self._lookup_callable_signature(target)
                if callable_info is None:
                    continue
                _display, signature = callable_info
                hints.extend(_inlay_hints_for_call(call, signature.parameters))
            hints.sort(key=lambda hint: hint.position)
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
        - Each resolved bare ``ast.Name`` use (Load context). Local bindings
          are classified through the shared lexical scope tree, so parameters
          and local variables that shadow module bindings retain their local
          token kind. Module-level uses fall back to the symbol table's kind.
          Attribute uses and unresolved cross-module re-exports are skipped.

        Tokens are sorted by ``(line, character)`` with ``line`` /
        ``character`` 0-based (LSP-style). Files that fail to parse,
        non-``.py`` paths, and missing files raise ``FileNotFoundError``
        for the missing-file case and return ``()`` for the unparseable
        case.

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
            mirror_table = module_symbol_table(self.db, self.mirror_root, str(mirror_path))
            table = self._remap_module_symbol_table(mirror_table)
            lexical = scope_tree(self.db, str(mirror_path))
            # Resolution runs against mirror paths; only the names are consumed.
            import_token_types = from_import_semantic_token_types(
                self.db, self.mirror_root, str(mirror_path), mirror_table
            )
            return _compute_semantic_tokens(
                source, table, lexical, import_token_types=import_token_types
            )

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
        start = SourcePosition(start_line, start_character)
        end = SourcePosition(end_line, end_character) if end_line is not None else None
        for token in all_tokens:
            if token.range.start < start:
                continue
            if end is not None and token.range.start >= end:
                continue
            filtered.append(token)
        return tuple(filtered)

    def type_definitions_at(
        self,
        symbol_id: SymbolId,
    ) -> tuple[TypeDefinitionLocation, ...]:
        """Resolve the type-definition locations for ``symbol_id``.

        Reads the lexical binding's declared annotation (or a module-level
        function's return annotation), parses it as a Python expression, and
        resolves the contained type names against the declaration's module. Returns one
        `TypeDefinitionLocation(path, range)` per workspace-resolved type,
        deduplicated by path and range.

        Classes are themselves the type — clicking on a class name returns its
        own definition location. Annotated lexical bindings, including
        parameters, resolve their declared types. Import aliases, `from_import`
        aliases, wildcard-import stubs, unannotated bindings, and non-workspace
        targets return an empty tuple. Whole-string forward references (`x: "Foo"`,
        `def f() -> "Foo"`) are unwrapped and re-parsed once; partial string
        annotations (`x: "Foo" | None`) and stdlib / installed / ambiguous type
        names are skipped.
        """
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(symbol_id.path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            lexical = scope_tree(self.db, str(mirror_path))
            binding = next(
                (
                    item
                    for item in lexical.bindings
                    if item.symbol_id.scope_id == symbol_id.scope_id
                    and item.name == symbol_id.name
                    and item.range == symbol_id.declaration
                ),
                None,
            )
            if binding is None:
                return ()
            if binding.kind == "class":
                return (
                    TypeDefinitionLocation(
                        path=real_path,
                        range=symbol_id.declaration,
                    ),
                )
            annotation = binding.annotation
            if binding.kind == "function":
                defining_table = module_symbol_table(self.db, self.mirror_root, str(mirror_path))
                matched = next(
                    (
                        symbol
                        for symbol in defining_table.symbols
                        if symbol.qualified_name.rsplit(".", 1)[-1] == binding.name
                        and symbol.range == binding.range
                    ),
                    None,
                )
                annotation = (
                    matched.signature.return_annotation
                    if matched is not None and matched.signature is not None
                    else None
                )
            if annotation is None:
                return ()
            source = self.source_text(real_path)
            positions = (
                _annotation_type_positions(
                    source,
                    binding.name,
                    binding.kind,
                    binding.range,
                )
                if source is not None
                else ()
            )
            resolved_types: list[ResolvedTarget] = []
            for position in positions:
                resolved = self._resolved_target_at(mirror_path, position)
                if resolved is not None:
                    resolved_types.append(resolved)
            if not positions:
                for ref in _collect_annotation_type_refs(annotation):
                    resolved = self._resolve_annotation_type_ref(mirror_path, ref)
                    if resolved is not None:
                        resolved_types.append(resolved)
            locations: list[TypeDefinitionLocation] = []
            seen: set[tuple[str, SourceRange]] = set()
            for type_resolved in resolved_types:
                if (
                    type_resolved.resolution != "workspace"
                    or type_resolved.defining_path is None
                    or type_resolved.range is None
                ):
                    continue
                key = (type_resolved.defining_path, type_resolved.range)
                if key in seen:
                    continue
                seen.add(key)
                locations.append(
                    TypeDefinitionLocation(
                        path=type_resolved.defining_path,
                        range=type_resolved.range,
                    )
                )
            return tuple(locations)

    def declaration_location_at(
        self,
        symbol_id: SymbolId,
    ) -> DeclarationLocation | None:
        """Return the exact declaration of an already-resolved lexical ID."""

        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(symbol_id.path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            mirror_id = SymbolId(
                str(mirror_path),
                symbol_id.scope_id,
                symbol_id.name,
                symbol_id.declaration,
            )
            lexical = scope_tree(self.db, str(mirror_path))
            if not any(binding.symbol_id == mirror_id for binding in lexical.bindings):
                return None
            return DeclarationLocation(
                path=real_path,
                range=symbol_id.declaration,
            )

    def prepare_call_hierarchy(
        self,
        path: str | os.PathLike[str],
        line: int,
        character: int,
    ) -> tuple[CallHierarchyItem, ...]:
        """Return the call-hierarchy item(s) for the identifier at the cursor.

        Resolves the identifier under ``(line, character)`` (LSP-style 0-based
        coordinates) through the shared public resolver. If the resolved target is a
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
            resolved = self._resolved_target_at(mirror_path, SourcePosition(line, character))
            if resolved is None:
                return ()
            if resolved.resolution != "workspace":
                return ()
            if resolved.defining_path is None or resolved.range is None:
                return ()
            defining_mirror = self._mirror_path_for_real(resolved.defining_path)
            if not defining_mirror.exists() or defining_mirror.suffix != ".py":
                return ()
            defining_table = module_symbol_table(self.db, self.mirror_root, str(defining_mirror))
            matched: Symbol | None = None
            for symbol in defining_table.symbols:
                if symbol.qualified_name == resolved.qualified_name and symbol.kind in (
                    "function",
                    "method",
                    "class",
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
            result = self._find_references_by_name(
                real_path, qualified_name, include_declaration=False
            )
            if (
                not isinstance(result.target, ResolvedTarget)
                or result.target.resolution != "workspace"
            ):
                return ()

            grouped: dict[tuple[str, str], list[CallHierarchyCallSite]] = {}
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
                            tree_cache[ref_real_path] = _parse_python(source)
                        except SyntaxError:
                            tree_cache[ref_real_path] = None
                tree = tree_cache[ref_real_path]
                if tree is None:
                    continue
                if ref_real_path not in table_cache:
                    table_cache[ref_real_path] = self._remap_module_symbol_table(
                        module_symbol_table(self.db, self.mirror_root, str(ref_mirror_path))
                    )
                table = table_cache[ref_real_path]
                known_qnames = frozenset(
                    symbol.qualified_name
                    for symbol in table.symbols
                    if symbol.kind in ("function", "method", "class")
                )
                caller_qname = _enclosing_callable_qname(
                    tree, known_qnames, ref.range.start.line + 1
                )
                if caller_qname is None:
                    continue
                key = (ref_real_path, caller_qname)
                if key not in grouped:
                    grouped[key] = []
                    order.append(key)
                grouped[key].append(CallHierarchyCallSite(range=ref.range))

            incoming: list[CallHierarchyIncomingCall] = []
            for caller_path, caller_qname in order:
                caller_item = self._build_call_hierarchy_item(
                    caller_path, caller_qname, module_name=None
                )
                if caller_item is None:
                    continue
                sites = sorted(
                    grouped[(caller_path, caller_qname)],
                    key=lambda site: site.range.start,
                )
                incoming.append(
                    CallHierarchyIncomingCall(caller=caller_item, call_sites=tuple(sites))
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
        Each bare-name or attribute callee is resolved at its terminal source
        position through the shared lexical resolver. Proven workspace-module,
        class, ``self`` / ``cls``, and directly annotated receiver chains can
        resolve; unproven or rebound chains do not. Subscripted and lambda
        calls produce no callee. Targets that don't resolve to a workspace
        function, method, or class are skipped.
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
                tree = _parse_python(source)
            except SyntaxError:
                return ()
            body_node = _find_callable_node(tree, qualified_name)
            if body_node is None:
                return ()

            grouped: dict[tuple[str, str], list[CallHierarchyCallSite]] = {}
            order: list[tuple[str, str]] = []
            for call in _collect_outgoing_calls(body_node):
                func_range = _call_func_range(call)
                if func_range is None:
                    continue
                target_resolved = self._resolve_call_target(mirror_path, call.func)
                if target_resolved is None:
                    continue
                if (
                    target_resolved.resolution != "workspace"
                    or target_resolved.defining_path is None
                    or target_resolved.range is None
                ):
                    continue
                defining_mirror = self._mirror_path_for_real(target_resolved.defining_path)
                if not defining_mirror.exists() or defining_mirror.suffix != ".py":
                    continue
                defining_table = module_symbol_table(
                    self.db, self.mirror_root, str(defining_mirror)
                )
                matched: Symbol | None = None
                for symbol in defining_table.symbols:
                    if symbol.qualified_name == target_resolved.qualified_name and symbol.kind in (
                        "function",
                        "method",
                        "class",
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
                        range=SourceRange(
                            SourcePosition(sl, sc),
                            SourcePosition(el, ec),
                        )
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
                    key=lambda site: site.range.start,
                )
                outgoing.append(
                    CallHierarchyOutgoingCall(callee=callee_item, call_sites=tuple(sites))
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
        coordinates) through the shared public resolver. If the resolved target is
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
            resolved = self._resolved_target_at(mirror_path, SourcePosition(line, character))
            if resolved is None:
                return ()
            if resolved.resolution != "workspace":
                return ()
            if resolved.defining_path is None or resolved.range is None:
                return ()
            defining_mirror = self._mirror_path_for_real(resolved.defining_path)
            if not defining_mirror.exists() or defining_mirror.suffix != ".py":
                return ()
            defining_table = module_symbol_table(self.db, self.mirror_root, str(defining_mirror))
            matched: Symbol | None = None
            for symbol in defining_table.symbols:
                if symbol.qualified_name == resolved.qualified_name and symbol.kind == "class":
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
        classes are still navigated. Bare-name and attribute-chain bases are
        resolved at their terminal source position through the shared lexical
        resolver, so a chain such as ``pkg.sub.Foo`` works only when its root
        and each step are proven. ``Starred`` bases, call expressions, and
        unproven or rebound chains produce no entry. Only workspace ``class``
        targets contribute an item; stdlib / installed / ambiguous / missing
        bases are dropped.
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
                tree = _parse_python(source)
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
                resolved = self._resolve_class_target(
                    mirror_path, target, _expression_name_position(base)
                )
                if resolved is None:
                    continue
                if (
                    resolved.resolution != "workspace"
                    or resolved.defining_path is None
                    or resolved.range is None
                ):
                    continue
                key = (resolved.defining_path, resolved.qualified_name)
                if key in seen:
                    continue
                seen.add(key)
                defining_mirror = self._mirror_path_for_real(resolved.defining_path)
                if not defining_mirror.exists() or defining_mirror.suffix != ".py":
                    continue
                defining_table = module_symbol_table(
                    self.db, self.mirror_root, str(defining_mirror)
                )
                base_symbol: Symbol | None = None
                for symbol in defining_table.symbols:
                    if symbol.qualified_name == resolved.qualified_name and symbol.kind == "class":
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
        of bases follows the same position-based rules as
        :meth:`type_hierarchy_supertypes`; unproven or rebound chains are
        skipped. Duplicates by
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
                tree = _parse_python(source)
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
                if not candidate_mirror.exists() or candidate_mirror.suffix != ".py":
                    continue
                candidate_source = self.source_text(candidate_real_path)
                if candidate_source is None:
                    continue
                try:
                    candidate_tree = _parse_python(candidate_source)
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
                            candidate_mirror,
                            base_target,
                            _expression_name_position(base),
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
        position: SourcePosition | None = None,
    ) -> ResolvedTarget | None:
        """Resolve a ``("name", X)`` or ``("attr", L, A)`` ref to a class.

        ``("name", X)`` looks ``X`` up in ``caller_mirror_path``'s module
        imports. ``("attr", L, A)`` resolves ``L`` to a workspace module
        and then ``A`` inside that module. The resolved symbol is mapped
        back from the mirror to the real workspace before being returned.
        Mirrors :meth:`_resolve_call_target`'s shape — kept separate so
        the two resolvers can diverge without coupling.
        """
        if position is not None:
            return self._resolved_target_at(caller_mirror_path, position)
        if target[0] == "name":
            return self._remap_resolved_target(
                _resolve_target(
                    self.db,
                    self.mirror_root,
                    str(caller_mirror_path),
                    target[1],
                )
            )
        # ("attr", lhs_name, attr_name)
        return self._resolve_attr_on_module(caller_mirror_path, target[1], target[2])

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
            tree = _parse_python(source)
        except SyntaxError:
            return None
        node = _find_callable_node(tree, qualified_name)
        if node is None or not isinstance(node, ast.ClassDef):
            return None
        if node.decorator_list:
            range_start_line = min(dec.lineno for dec in node.decorator_list) - 1
            range_start_col = min(dec.col_offset for dec in node.decorator_list)
        else:
            range_start_line = node.lineno - 1
            range_start_col = node.col_offset
        range_end_line = (node.end_lineno or node.lineno) - 1
        range_end_col = node.end_col_offset or 0

        bare_name = qualified_name.rsplit(".", 1)[-1]
        located = self._locate_def_class_name_offsets(real_path, node.lineno, bare_name)
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
            range=SourceRange(
                SourcePosition(range_start_line, range_start_col),
                SourcePosition(range_end_line, range_end_col),
            ),
            selection_range=SourceRange(
                SourcePosition(selection_line, selection_start_col),
                SourcePosition(selection_line, selection_end_col),
            ),
        )

    def _resolve_attr_on_module(
        self,
        caller_mirror_path: Path,
        lhs_name: str,
        attr_name: str,
    ) -> ResolvedTarget | None:
        """Resolve ``lhs_name.attr_name`` LHS-bare-`Name`-first.

        Resolves ``lhs_name`` through ``caller_mirror_path``'s imports to a
        workspace module, then ``attr_name`` inside that module. Returns
        ``None`` when the LHS is not a workspace module (mirroring
        ``find_references``'s LHS-bare-`Name` handling). Shared by
        ``_resolve_call_target``, ``_resolve_class_target``, and
        ``signature_help_at``.
        """
        lhs_resolved = self._remap_resolved_target(
            _resolve_target(self.db, self.mirror_root, str(caller_mirror_path), lhs_name)
        )
        if lhs_resolved.resolution != "workspace" or lhs_resolved.defining_path is None:
            return None
        lhs_mirror = self._mirror_path_for_real(lhs_resolved.defining_path)
        if not lhs_mirror.exists() or lhs_mirror.suffix != ".py":
            return None
        return self._remap_resolved_target(
            _resolve_target(self.db, self.mirror_root, str(lhs_mirror), attr_name)
        )

    def _resolve_call_target(
        self,
        caller_mirror_path: Path,
        func: ast.expr,
    ) -> ResolvedTarget | None:
        if isinstance(func, ast.Subscript):
            return None
        position = _expression_name_position(func)
        if position is None:
            return None
        return self._resolved_target_at(caller_mirror_path, position)

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
            tree = _parse_python(source)
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
            range_start_line = min(dec.lineno for dec in node.decorator_list) - 1
            range_start_col = min(dec.col_offset for dec in node.decorator_list)
        else:
            range_start_line = node.lineno - 1
            range_start_col = node.col_offset
        range_end_line = (node.end_lineno or node.lineno) - 1
        range_end_col = node.end_col_offset or 0

        bare_name = qualified_name.rsplit(".", 1)[-1]
        located = self._locate_def_class_name_offsets(real_path, node.lineno, bare_name)
        if located is None:
            return None
        selection_start_col, selection_end_col = located
        selection_line = node.lineno - 1

        if module_name is None:
            mirror_path = self._mirror_path_for_real(real_path)
            if mirror_path.exists() and mirror_path.suffix == ".py":
                table = self._remap_module_symbol_table(
                    module_symbol_table(self.db, self.mirror_root, str(mirror_path))
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
            range=SourceRange(
                SourcePosition(range_start_line, range_start_col),
                SourcePosition(range_end_line, range_end_col),
            ),
            selection_range=SourceRange(
                SourcePosition(selection_line, selection_start_col),
                SourcePosition(selection_line, selection_end_col),
            ),
        )

    def _resolve_annotation_type_ref(
        self,
        defining_mirror: Path,
        ref: tuple[str, ...],
    ) -> ResolvedTarget | None:
        if ref[0] == "name":
            return self._remap_resolved_target(
                _resolve_target(
                    self.db,
                    self.mirror_root,
                    str(defining_mirror),
                    ref[1],
                )
            )
        # ("attribute", lhs_name, attr)
        lhs_resolved = self._remap_resolved_target(
            _resolve_target(self.db, self.mirror_root, str(defining_mirror), ref[1])
        )
        if lhs_resolved.resolution != "workspace" or lhs_resolved.defining_path is None:
            return None
        lhs_mirror = self._mirror_path_for_real(lhs_resolved.defining_path)
        if not lhs_mirror.exists() or lhs_mirror.suffix != ".py":
            return None
        return self._remap_resolved_target(
            _resolve_target(self.db, self.mirror_root, str(lhs_mirror), ref[2])
        )

    def _lookup_callable_signature(self, target: ResolvedTarget) -> tuple[str, Signature] | None:
        if target.defining_path is None or target.range is None:
            return None
        defining_mirror = self._mirror_path_for_real(target.defining_path)
        if not defining_mirror.exists() or defining_mirror.suffix != ".py":
            return None
        table = module_symbol_table(self.db, self.mirror_root, str(defining_mirror))
        matched = None
        for symbol in table.symbols:
            if symbol.qualified_name == target.qualified_name:
                matched = symbol
                break
        if matched is None:
            return None
        if matched.kind == "function" and matched.signature is not None:
            return (matched.qualified_name, matched.signature)
        if matched.kind == "class":
            init_qualified = f"{matched.qualified_name}.__init__"
            for inner in table.symbols:
                if inner.qualified_name == init_qualified and inner.signature is not None:
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

    def _signature_defaults(
        self, resolved: ResolvedTarget, display_name: str
    ) -> dict[str, str] | None:
        """Default-value expressions for `resolved`'s callable, extracted from
        its defining file's source (`Parameter` carries no
        default, so this is a consumer-side read). Returns ``None`` when the
        defining source is unavailable or unparseable — defaults are then
        simply omitted from the signature label."""
        if resolved.defining_path is None or resolved.range is None:
            return None
        defining_source = self.source_text(resolved.defining_path)
        if defining_source is None:
            return None
        return _parameter_defaults_from_source(
            defining_source, resolved.range.start.line + 1, display_name
        )

    def rename_symbol(
        self,
        symbol_id: SymbolId,
        new_name: str,
    ) -> RenameResult:
        bare_old = symbol_id.name
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(symbol_id.path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            if not new_name.isidentifier():
                return RenameResult(target=symbol_id, edits=(), status="invalid_identifier")
            if keyword.iskeyword(new_name):
                return RenameResult(target=symbol_id, edits=(), status="keyword_identifier")
            if new_name == bare_old:
                return RenameResult(target=symbol_id, edits=(), status="same_name")
            references = self.find_references(symbol_id, include_declaration=True)
            edits: list[RenameEdit] = []
            for ref in references.references:
                ref_real_path = self._remap_path(ref.path) or ref.path
                source_range = ref.range
                if (
                    ref.is_declaration
                    and source_range.start.character == 0
                    and source_range.end.character == 1
                ):
                    located = self._locate_def_class_name_offsets(
                        ref_real_path, source_range.start.line + 1, bare_old
                    )
                    if located is None:
                        continue
                    source_range = SourceRange(
                        SourcePosition(source_range.start.line, located[0]),
                        SourcePosition(source_range.start.line, located[1]),
                    )
                edits.append(
                    RenameEdit(
                        path=ref_real_path,
                        range=source_range,
                        new_text=new_name,
                    )
                )
            if symbol_id.scope_id == "module":
                defining_table = module_symbol_table(self.db, self.mirror_root, str(mirror_path))
                edits.extend(
                    self._collect_from_import_edits(
                        defining_module=defining_table.module,
                        bare_old=bare_old,
                        new_name=new_name,
                    )
                )
            seen: set[tuple[str, SourceRange]] = set()
            unique_edits: list[RenameEdit] = []
            for edit in edits:
                key = (edit.path, edit.range)
                if key in seen:
                    continue
                seen.add(key)
                unique_edits.append(edit)
            unique_edits.sort(key=lambda edit: (edit.path, edit.range.start))
            return RenameResult(target=symbol_id, edits=tuple(unique_edits), status="ok")

    def _locate_def_class_name_offsets(
        self, real_path: str, lineno: int, bare_old: str
    ) -> tuple[int, int] | None:
        source = self.source_text(real_path)
        if source is None:
            return None
        try:
            tree = _parse_python(source)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.lineno == lineno
                and node.name == bare_old
            ):
                lines = source.splitlines()
                line_idx = lineno - 1
                if not (0 <= line_idx < len(lines)):
                    return None
                line = lines[line_idx]
                located = _normalized_name_offsets_on_line(
                    line,
                    bare_old,
                    node.col_offset,
                )
                if located is not None:
                    return located
        return None

    def _collect_from_import_edits(
        self,
        *,
        defining_module: str,
        bare_old: str,
        new_name: str,
    ) -> list[RenameEdit]:
        workspace = self._remap_workspace_analysis(workspace_analysis(self.db, self.mirror_root))
        edits: list[RenameEdit] = []
        for module in workspace.modules:
            real_path = module.path
            source = self.source_text(real_path)
            if source is None:
                continue
            try:
                tree = _parse_python(source)
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
                            range=SourceRange(
                                SourcePosition(alias.lineno - 1, alias.col_offset),
                                SourcePosition(
                                    alias.lineno - 1,
                                    alias.col_offset + len(bare_old),
                                ),
                            ),
                            new_text=new_name,
                        )
                    )
        return edits

    def import_edits_for_file_renames(
        self,
        renames: Sequence[tuple[str | os.PathLike[str], str | os.PathLike[str]]],
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
            edits.sort(key=lambda edit: (edit.path, edit.range.start))
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
            tree = _parse_python(source)
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
                            range=SourceRange(
                                SourcePosition(alias.lineno - 1, alias.col_offset),
                                SourcePosition(
                                    alias.lineno - 1,
                                    alias.col_offset + len(alias.name),
                                ),
                            ),
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
                                range=SourceRange(
                                    SourcePosition(line_idx, start_col),
                                    SourcePosition(line_idx, end_col),
                                ),
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
                    candidate = f"{resolved}.{alias.name}" if resolved else alias.name
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
                            range=SourceRange(
                                SourcePosition(alias.lineno - 1, alias.col_offset),
                                SourcePosition(
                                    alias.lineno - 1,
                                    alias.col_offset + len(alias.name),
                                ),
                            ),
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
            edits.sort(key=lambda edit: (edit.path, edit.range.start))
            return tuple(edits)

    def _resolve_file_deletion(self, path: str | os.PathLike[str]) -> str | None:
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
            tree = _parse_python(source)
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
                                range=SourceRange(
                                    SourcePosition(start_line, 0),
                                    SourcePosition(end_line, 0),
                                ),
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
        dead_indices = [i for i, alias in enumerate(node.names) if alias.name in deleted_modules]
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
                    range=SourceRange(
                        SourcePosition(start_line, 0),
                        SourcePosition(end_line, 0),
                    ),
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
            candidate = f"{resolved_module}.{alias.name}" if resolved_module else alias.name
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
                    range=SourceRange(
                        SourcePosition(start_line, 0),
                        SourcePosition(end_line, 0),
                    ),
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
            with tokenize.open(real_path) as source_file:
                return source_file.read()
        except (OSError, SyntaxError, UnicodeError):
            return None

    def _build_file_result(
        self,
        real_path: str,
        dependency_inputs: _DependencyInputs,
        dependency_check: DependencyCheckAnalysis,
        *,
        module: PythonModuleAnalysis | None = None,
        reexports: _ReexportIndex | None = None,
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
                reexports,
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
        reexports: _ReexportIndex | None = None,
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
                    range=diagnostic.range,
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
                        range=resolved_import.range,
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
                        range=resolved_import.range,
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
                        range=resolved_import.range,
                    )
                )

            if (
                resolved_import.resolution == "workspace"
                and resolved_import.imported_name is not None
                and resolved_import.imported_name != "*"
            ):
                symbol_result = self._remap_resolved_target(
                    _resolve_target(
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
                            range=resolved_import.range,
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
                            range=resolved_import.range,
                        )
                    )

        diagnostics.extend(
            self._unused_import_diagnostics(real_path, mirror_path, module_result, reexports)
        )

        return tuple(diagnostics)

    def _unused_import_diagnostics(
        self,
        real_path: str,
        mirror_path: str,
        module_result: PythonModuleAnalysis,
        reexports: _ReexportIndex | None = None,
    ) -> list[AnalysisDiagnostic]:
        """Flag workspace ``from M import name`` bindings that are never used.

        Conservative by design (see the guide's ``unused-import``
        limitations): only ``from`` imports whose target resolves to a
        workspace module are considered, so that the occurrence scan can
        actually verify usage. ``import M`` is skipped (attribute usage is
        under-reported) and stdlib / installed targets are skipped (their
        usage cannot be verified). ``__init__.py`` files, self-alias
        re-exports (``from y import z as z``), names another workspace module
        re-imports from this file, and files with syntax errors are all left
        alone.
        """
        if Path(real_path).name == "__init__.py":
            return []
        # A parse error anywhere makes the occurrence scan unreliable.
        if module_result.diagnostics:
            return []

        # Deciding there is nothing to check needs only the module analysis, so
        # it happens before the file is read and parsed: most files import no
        # workspace name at all and never reach the scan below.
        workspace_from: dict[tuple[int, str], ResolvedImportRef] = {}
        for resolved_import in module_result.resolved_imports:
            if (
                resolved_import.kind == "from"
                and resolved_import.resolution == "workspace"
                and resolved_import.imported_name is not None
                and resolved_import.imported_name != "*"
            ):
                key = (
                    resolved_import.range.start.line + 1,
                    resolved_import.imported_name,
                )
                workspace_from[key] = resolved_import
        if not workspace_from:
            return []

        source = self.source_text(real_path)
        if source is None:
            return []
        try:
            tree = _parse_python(source)
        except SyntaxError:
            return []

        reexported = self._reexported_names_for_module(
            module_result.module, mirror_path, reexports
        )
        # A name listed in this module's own static `__all__` is an intentional
        # public re-export; removing it would break the facade's API.
        static_all = _static_module_all_names(tree)

        diagnostics: list[AnalysisDiagnostic] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if (node.lineno, alias.name) not in workspace_from:
                    continue
                # `from y import z as z` is the canonical explicit re-export.
                if alias.asname is not None and alias.asname == alias.name:
                    continue
                binding = alias.asname or alias.name
                if binding in reexported or "*" in reexported:
                    continue
                if binding in static_all:
                    continue
                resolved = _resolve_target(self.db, self.mirror_root, mirror_path, binding)
                # A binding that doesn't resolve to a workspace symbol is a
                # *broken* import (its own `unresolved-symbol` diagnostic), not
                # an unused one — leave it to that diagnostic + its quick fix.
                if resolved.resolution != "workspace":
                    continue
                if self._name_is_used_in_file(mirror_path, resolved):
                    continue
                diagnostics.append(
                    AnalysisDiagnostic(
                        path=real_path,
                        code="unused-import",
                        message=(f"Imported name {binding!r} is not used in this module."),
                        severity="hint",
                        source="pyinc.symbol_resolution",
                        range=SourceRange(
                            SourcePosition(alias.lineno - 1, alias.col_offset),
                            SourcePosition(
                                (alias.end_lineno or alias.lineno) - 1,
                                alias.end_col_offset
                                if alias.end_col_offset is not None
                                else alias.col_offset + len(alias.name),
                            ),
                        ),
                        tags=("unnecessary",),
                    )
                )
        return diagnostics

    def _reexported_names_for_module(
        self,
        file_module: str,
        self_mirror_path: str,
        reexports: _ReexportIndex | None = None,
    ) -> set[str]:
        """Names other workspace modules import ``from <file_module>``.

        Removing a ``from M import name`` binding in this file is only safe
        when the file does not itself re-export ``name`` — i.e. no *other*
        workspace module does ``from <this_module> import name`` (or
        ``from <this_module> import *``, which could re-export anything).

        A workspace request passes the index it built once for the whole run;
        single-file callers fall back to building it from the workspace
        analysis, which is the same walk this used to do per file.
        """
        if reexports is None:
            reexports = _build_reexport_index(workspace_analysis(self.db, self.mirror_root).modules)
        return {
            name
            for importer_path, name in reexports.get(file_module, ())
            if importer_path != self_mirror_path
        }

    def _dependency_inputs(self) -> _DependencyInputs:
        config = workspace_config_analysis(self.db, self.mirror_root)
        requirements = workspace_requirements_analysis(self.db, self.mirror_root)

        declared: list[str] = []
        if config is not None:
            declared.extend(config.dependencies)
            for _, group_entries in config.optional_dependency_groups:
                declared.extend(group_entries)
        if requirements is not None:
            declared.extend(requirement.raw_line for requirement in requirements.requirements)

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
        requirements_by_name: dict[str, tuple[str, SourceRange]] = {}
        if dependency_inputs.requirements is not None:
            requirements_path = dependency_inputs.requirements.path
            if only_path is None or requirements_path == only_path:
                for code, message in dependency_inputs.requirements.diagnostics:
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=requirements_path,
                            code=code,
                            message=message,
                            severity="error",
                            source="pyinc.requirements_txt",
                            range=SourceRange(SourcePosition(0, 0), SourcePosition(0, 1)),
                        )
                    )
            for requirement in dependency_inputs.requirements.requirements:
                requirements_by_name.setdefault(
                    _normalize_dependency_name(requirement.name),
                    (dependency_inputs.requirements.path, requirement.range),
                )

        config_path = (
            dependency_inputs.config.path if dependency_inputs.config is not None else None
        )

        for status in dependency_check.statuses:
            if status.status == "satisfied":
                continue

            if status.name in requirements_by_name:
                path, source_range = requirements_by_name[status.name]
            elif config_path is not None:
                path = config_path
                source_range = SourceRange(SourcePosition(0, 0), SourcePosition(0, 1))
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
                    range=source_range,
                )
            )

        if dependency_check.diagnostics:
            target_path: str | None = None
            if dependency_inputs.requirements is not None:
                target_path = dependency_inputs.requirements.path
            elif dependency_inputs.config is not None:
                target_path = dependency_inputs.config.path
            if target_path is not None and (only_path is None or target_path == only_path):
                for code, message in dependency_check.diagnostics:
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=target_path,
                            code=code,
                            message=message,
                            severity="warning",
                            source="pyinc.dependency_check",
                            range=SourceRange(SourcePosition(0, 0), SourcePosition(0, 1)),
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

    def _normalize_real_path(self, path: str | os.PathLike[str]) -> str:
        return self._mirror.normalize_real_path(path)

    def _mirror_path_for_real(self, real_path: str) -> Path:
        return self._mirror.mirror_path_for_real(real_path)

    def _sync_path_from_disk(self, real_path: str) -> None:
        self._mirror.sync_path_from_disk(real_path)
        request_inputs_changed()

    def _mirrored_content_hashes(self) -> dict[str, str]:
        """Sha256 per real path of the disk content the mirror was synced from.

        Watchers seed their first-poll baseline from this instead of the
        current disk state, so an edit landing between the mirror copy and
        watcher construction is still detected."""
        with self._state_lock:
            return self._mirror.content_hashes()

    def _remap_workspace_analysis(
        self, analysis: PythonWorkspaceAnalysis
    ) -> PythonWorkspaceAnalysis:
        return PythonWorkspaceAnalysis(
            root=self.root,
            modules=tuple(self._remap_module_analysis(module) for module in analysis.modules),
        )

    def _remap_module_analysis(self, analysis: PythonModuleAnalysis) -> PythonModuleAnalysis:
        return PythonModuleAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            module=analysis.module,
            imports=analysis.imports,
            definitions=analysis.definitions,
            diagnostics=tuple(
                Diagnostic(
                    code=diagnostic.code,
                    message=self._remap_message(diagnostic.message),
                    range=diagnostic.range,
                )
                for diagnostic in analysis.diagnostics
            ),
            resolved_imports=tuple(
                ResolvedImportRef(
                    module=item.module,
                    kind=item.kind,
                    range=item.range,
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

    def _remap_workspace_symbol_index(self, index: WorkspaceSymbolIndex) -> WorkspaceSymbolIndex:
        return WorkspaceSymbolIndex(
            root=self.root,
            entries=index.entries,
        )

    def _remap_class_model(self, model: ClassModel) -> ClassModel:
        return ClassModel(
            path=self._remap_path(model.path) or model.path,
            qualified_name=model.qualified_name,
            members=tuple(
                ClassMember(
                    name=member.name,
                    kind=member.kind,
                    range=member.range,
                    annotation=member.annotation,
                    signature=member.signature,
                    defining_path=self._remap_path(member.defining_path),
                    defining_class=member.defining_class,
                )
                for member in model.members
            ),
            unresolved_bases=model.unresolved_bases,
            truncated_bases=model.truncated_bases,
        )

    def _remap_resolved_target(self, symbol: ResolvedTarget) -> ResolvedTarget:
        return ResolvedTarget(
            original_module=symbol.original_module,
            qualified_name=symbol.qualified_name,
            resolution=symbol.resolution,
            defining_module=symbol.defining_module,
            defining_path=self._remap_path(symbol.defining_path),
            range=symbol.range,
            distribution_name=symbol.distribution_name,
            distribution_version=symbol.distribution_version,
            follow_depth=symbol.follow_depth,
            trail=symbol.trail,
        )

    def _remap_config_analysis(self, analysis: ConfigAnalysis | None) -> ConfigAnalysis | None:
        if analysis is None:
            return None
        return ConfigAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            sections=analysis.sections,
            dependencies=analysis.dependencies,
            optional_dependency_groups=analysis.optional_dependency_groups,
            tool_configs=analysis.tool_configs,
            diagnostics=tuple(
                (code, self._remap_message(message)) for code, message in analysis.diagnostics
            ),
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
            diagnostics=tuple(
                (code, self._remap_message(message)) for code, message in analysis.diagnostics
            ),
        )

    def _remap_message(self, message: str) -> str:
        """Rewrite mirror paths embedded in kernel diagnostic text.

        `pyinc.integrations.Diagnostic` has no path field, so an integration that
        needs to name a file interpolates it into the message. Under a session
        that file is the mirror copy, whose temporary directory is randomly
        named, which would make the message differ between otherwise identical
        runs.

        A `-r` target that escapes the root resolves *beside* the mirror rather
        than under it, so the mirror's parent is mapped too. That pass must come
        second: the parent is a prefix of the mirror root, so running it first
        would rewrite every ordinary mirror path.
        """

        remapped = message.replace(self.mirror_root, self.root)
        mirror_parent = str(Path(self.mirror_root).parent)
        return remapped.replace(mirror_parent, str(Path(self.root).parent))

    def _remap_path(self, path: str | None) -> str | None:
        if path is None:
            return None
        candidate = Path(path)
        try:
            relative_path = candidate.relative_to(self._mirror.mirror_root_path)
        except ValueError:
            return path
        return str(Path(self.root, relative_path))

    def _dedupe_diagnostics(
        self, diagnostics: tuple[AnalysisDiagnostic, ...]
    ) -> tuple[AnalysisDiagnostic, ...]:
        seen: set[tuple[str, str, str, str, str, SourceRange | None]] = set()
        ordered: list[AnalysisDiagnostic] = []
        for diagnostic in diagnostics:
            key = (
                diagnostic.path,
                diagnostic.code,
                diagnostic.message,
                diagnostic.severity,
                diagnostic.source,
                diagnostic.range,
            )
            if key in seen:
                continue
            seen.add(key)
            ordered.append(diagnostic)
        return tuple(ordered)
