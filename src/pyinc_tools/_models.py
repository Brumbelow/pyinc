from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pyinc.integrations import (
    ConfigAnalysis,
    DependencyCheckAnalysis,
    ModuleSymbolTable,
    PythonModuleAnalysis,
    PythonWorkspaceAnalysis,
    Reference,
    RequirementsAnalysis,
    SourcePosition,
    SourceRange,
    SymbolId,
    WorkspaceSymbolIndex,
)

DiagnosticSeverity = Literal["error", "warning", "information", "hint"]
ResolutionKind = Literal[
    "workspace",
    "stdlib",
    "installed",
    "external",
    "ambiguous",
    "missing",
]


@dataclass(frozen=True)
class AnalysisDiagnostic:
    path: str
    code: str
    message: str
    severity: DiagnosticSeverity
    source: str
    range: SourceRange | None = None
    tags: tuple[str, ...] = ()


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
    "invalid_identifier",
    "keyword_identifier",
    "same_name",
]


@dataclass(frozen=True)
class RenameEdit:
    path: str
    range: SourceRange
    new_text: str


@dataclass(frozen=True)
class RenameResult:
    target: SymbolId
    edits: tuple[RenameEdit, ...]
    status: RenameStatus


@dataclass(frozen=True)
class FileRenameEdit:
    """A text edit returned by ``import_edits_for_file_renames``.

    ``range`` is zero-based, end-exclusive, and measured in Unicode code
    points.
    """

    path: str
    range: SourceRange
    new_text: str


@dataclass(frozen=True)
class FileDeletionEdit:
    """A text edit returned by ``import_edits_for_file_deletions``.

    Represents the deletion of an ``import`` / ``from`` statement (or a
    single alias inside one) that references a Python file that is about
    to be removed from the workspace. The edit's range covers the source
    span to be removed; ``new_text`` is always the empty string.

    ``range`` is zero-based, end-exclusive, and measured in Unicode code
    points.
    """

    path: str
    range: SourceRange
    new_text: str = ""


CodeActionKind = Literal["quickfix"]


@dataclass(frozen=True)
class CodeActionEdit:
    """A single text edit produced by a code action.

    ``range`` is zero-based, end-exclusive, and measured in Unicode code
    points; ``new_text`` is the empty string for deletion-style fixes and the
    replacement text for retarget-style fixes.
    """

    path: str
    range: SourceRange
    new_text: str = ""


@dataclass(frozen=True)
class CodeAction:
    """A quick fix anchored to a single diagnostic.

    ``diagnostic`` is the analysis diagnostic the fix resolves; the LSP layer
    echoes it back (converted) in the ``diagnostics`` field of the response so
    the client can associate the action with the problem. ``edits`` are the
    workspace text edits that apply the fix.
    """

    title: str
    kind: CodeActionKind
    diagnostic: AnalysisDiagnostic
    edits: tuple[CodeActionEdit, ...]


DocumentHighlightKind = Literal["text", "read", "write"]


@dataclass(frozen=True)
class DocumentHighlight:
    range: SourceRange
    kind: DocumentHighlightKind


@dataclass(frozen=True)
class LinkedEditingRange:
    range: SourceRange


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


CompletionItemKind = Literal[
    "function",
    "method",
    "class",
    "variable",
    "field",
    "module",
    "keyword",
]


@dataclass(frozen=True)
class CompletionItem:
    label: str
    kind: CompletionItemKind
    detail: str | None
    sort_text: str


FoldingRangeKind = Literal["imports", "comment", "region"]


@dataclass(frozen=True)
class FoldingRange:
    range: SourceRange
    kind: FoldingRangeKind


@dataclass(frozen=True)
class SelectionRange:
    range: SourceRange


@dataclass(frozen=True)
class DocumentLink:
    range: SourceRange
    target_path: str


@dataclass(frozen=True)
class CodeLens:
    range: SourceRange
    title: str


@dataclass(frozen=True)
class TypeDefinitionLocation:
    path: str
    range: SourceRange


@dataclass(frozen=True)
class DeclarationLocation:
    """Location where the identifier under the cursor is declared.

    Unlike definition and type-definition, declaration stops at the binding
    statement in the current file. ``range`` uses zero-based, end-exclusive
    Unicode-code-point geometry.
    """

    path: str
    range: SourceRange


InlayHintKind = Literal["parameter", "type"]


@dataclass(frozen=True)
class InlayHint:
    position: SourcePosition
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
    range: SourceRange
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
    range: SourceRange
    selection_range: SourceRange


@dataclass(frozen=True)
class CallHierarchyCallSite:
    range: SourceRange


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

    ``range`` covers the class block and ``selection_range`` spans its bare
    name. ``qualified_name`` follows the module symbol-table convention.
    """

    name: str
    kind: TypeHierarchyItemKind
    path: str
    qualified_name: str
    detail: str | None
    range: SourceRange
    selection_range: SourceRange


@dataclass(frozen=True)
class DependencyInputs:
    config: ConfigAnalysis | None
    requirements: RequirementsAnalysis | None
    declared_dependencies: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedTarget:
    original_module: str
    qualified_name: str
    resolution: ResolutionKind
    defining_module: str | None
    defining_path: str | None
    range: SourceRange | None
    distribution_name: str | None
    distribution_version: str | None
    follow_depth: int
    trail: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedReferenceResult:
    target: ResolvedTarget
    references: tuple[Reference, ...]


_PUBLIC_MODELS = (
    AnalysisDiagnostic,
    FileAnalysisResult,
    WorkspaceAnalysisResult,
    RenameEdit,
    RenameResult,
    FileRenameEdit,
    FileDeletionEdit,
    CodeActionEdit,
    CodeAction,
    DocumentHighlight,
    LinkedEditingRange,
    SignatureParameterInfo,
    SignatureHelp,
    CompletionItem,
    FoldingRange,
    SelectionRange,
    DocumentLink,
    CodeLens,
    TypeDefinitionLocation,
    DeclarationLocation,
    InlayHint,
    SemanticToken,
    CallHierarchyItem,
    CallHierarchyCallSite,
    CallHierarchyIncomingCall,
    CallHierarchyOutgoingCall,
    TypeHierarchyItem,
)

# These classes historically lived in ``pyinc_tools.session``. Keeping that
# module identity preserves reprs and pickle payloads while session re-exports
# the exact class objects from this module.
for _model in _PUBLIC_MODELS:
    _model.__module__ = "pyinc_tools.session"


__all__ = [
    "AnalysisDiagnostic",
    "CallHierarchyCallSite",
    "CallHierarchyIncomingCall",
    "CallHierarchyItem",
    "CallHierarchyItemKind",
    "CallHierarchyOutgoingCall",
    "CodeAction",
    "CodeActionEdit",
    "CodeActionKind",
    "CodeLens",
    "CompletionItem",
    "CompletionItemKind",
    "DeclarationLocation",
    "DiagnosticSeverity",
    "DocumentHighlight",
    "DocumentHighlightKind",
    "DocumentLink",
    "FileAnalysisResult",
    "FileDeletionEdit",
    "FileRenameEdit",
    "FoldingRange",
    "FoldingRangeKind",
    "InlayHint",
    "InlayHintKind",
    "LinkedEditingRange",
    "RenameEdit",
    "RenameResult",
    "RenameStatus",
    "SelectionRange",
    "SemanticToken",
    "SemanticTokenModifier",
    "SemanticTokenType",
    "SignatureHelp",
    "SignatureParameterInfo",
    "TypeDefinitionLocation",
    "TypeHierarchyItem",
    "TypeHierarchyItemKind",
    "WorkspaceAnalysisResult",
]
