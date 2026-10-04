# LSP Reference

`pyinc-tools lsp` is a stdio Language Server Protocol server for Python
workspaces. This reference lists the protocol methods it advertises and the
limits visible to editors. Installation, initialization options, and editor
configuration are in the [`pyinc-tools` guide](pyinc-tools-guide.md).

## Position and document model

- The server negotiates UTF-8, UTF-16, or UTF-32 positions. It defaults to
  UTF-16 when the client prefers none of them.
- Ranges are zero-based and end-exclusive.
- Document synchronization is full-text. Each change must carry the whole
  document, because incremental patches are not applied.
- Open documents use an in-memory overlay in the workspace mirror. Closing a
  document discards its overlay and returns analysis to the saved file.
- The server accepts paths inside the configured workspace only.

## Method matrix

| Method | Result | User-visible limits |
|---|---|---|
| `initialize`, `initialized`, `shutdown`, `exit` | Ordered server lifecycle and negotiated capabilities. | One workspace per server process. Requests before initialization or after shutdown fail. |
| `textDocument/didOpen`, `textDocument/didChange`, `textDocument/didSave`, `textDocument/didClose` | Maintains editor overlays and republishes diagnostics. | `textDocument/didChange` expects the full document text. `textDocument/didSave` reads the saved file and ignores any text sent with it. |
| `workspace/didChangeWatchedFiles` | Refreshes changed, created, or deleted files from disk. | Paths outside the workspace are rejected. The built-in polling watcher can provide the same refresh path. |
| `textDocument/documentSymbol` | Functions, classes, variables, and imports in one file. | Python files only. Malformed source may give an empty or partial result. |
| `workspace/symbol` | Case-insensitive name filtering across workspace symbols. | Workspace declarations only. Installed and stdlib symbols are outside the index. |
| `textDocument/hover` | Markdown name, kind, annotation, and signature for a resolved declaration. | Workspace targets only, with no evaluated types or docstring rendering. |
| `textDocument/completion` | Lexical names, import-module/name candidates, and statically resolved workspace members. | Offers no auto-imports, snippets, or keyword filtering. Omits installed and stdlib members, and members that need runtime type inference. |
| `textDocument/signatureHelp` | Signature and active positional parameter for a resolved workspace call. | No overload selection or inferred callable types. Unresolved receivers and non-workspace calls return no result. |
| `textDocument/definition` | Declaration of the resolved workspace symbol. | Installed, stdlib, missing, and ambiguous imports have no navigable location. |
| `textDocument/declaration` | Declaration location for the resolved workspace binding. | Same conservative workspace-only resolution as definition. |
| `textDocument/typeDefinition` | Class location named by a declared annotation. | Uses declared annotations only, with no type inference. Unannotated values and unsupported compound or ambiguous annotations return no result. |
| `textDocument/references` | Verified workspace references, optionally including the declaration. | Dynamic attribute access, unresolved receiver chains, and ambiguous re-exports are omitted. |
| `textDocument/documentHighlight` | Read/write highlights in the current document. | Only verified references to workspace symbols are highlighted. |
| `textDocument/linkedEditingRange` | Same-file ranges that can be edited together. | Current file only. Use rename for a workspace edit. |
| `textDocument/prepareRename`, `textDocument/rename` | Validates a target and returns workspace text edits. | Import aliases cannot be renamed from the alias occurrence. Edits change identifiers only and share the reference-resolution limits. |
| `textDocument/codeAction` | Quick fixes associated with diagnostics. | Quick fixes only. Current fixes remove an unused or unresolved import, or retarget an unambiguous single-name import. |
| `textDocument/foldingRange` | Import, class, function, and multiline block folds. | Python source only. Malformed files may return no folds. |
| `textDocument/selectionRange` | Nested syntax selections for requested positions. | Each position is handled independently. Invalid positions have no useful expansion. |
| `textDocument/documentLink` | Links workspace import names to source files. | Only imports that resolve to one workspace file become links. |
| `textDocument/codeLens` | Reference-count lenses on workspace declarations. | Only declarations with a resolvable workspace identity receive a lens. |
| `textDocument/prepareCallHierarchy`, `callHierarchy/incomingCalls`, `callHierarchy/outgoingCalls` | Direct workspace callers and callees. | Functions, methods, and classes only. Dynamic calls and non-workspace targets are omitted. |
| `textDocument/prepareTypeHierarchy`, `typeHierarchy/supertypes`, `typeHierarchy/subtypes` | Direct workspace class relationships. | Omits metaclass relationships, inferred types, and navigation to installed or stdlib bases. Clients request each next level separately. |
| `textDocument/inlayHint` | Parameter-name hints for positional arguments. | Parameter-name hints only, with no variable or return-type hints. Keyword, spread, dynamically resolved, and non-workspace calls are omitted. |
| `textDocument/semanticTokens/full`, `textDocument/semanticTokens/range` | Namespace, class, function, method, parameter, and variable tokens. A `from ... import ...` use is classified by the workspace declaration it resolves to. | Full responses only, with no deltas. Use-site classification covers lexical names and skips general attribute chains. Imports that resolve outside the workspace, or ambiguously, are left unclassified. |
| `textDocument/diagnostic`, `workspace/diagnostic` | Pull diagnostics with stable unchanged/full reports. | Diagnostics cover the shipped analyses only, with no general Python type checking or linting. |
| `textDocument/publishDiagnostics` | Push diagnostics after open/change/save/close and filesystem refreshes. Pushes reach the client in the order their analyses ran. | Identical payloads are deduplicated per document. Closed clean documents may receive an empty publication to clear stale editor state. |
| `workspace/willRenameFiles` | Import edits for renamed Python module files. | Module files only. Package-directory renames, imports inside the renamed file, and attribute-use rewrites are out of scope. |
| `workspace/willDeleteFiles` | Removes imports that refer to deleted Python module files. | Module files only. Package deletes and downstream attribute-use cleanup are out of scope. |

## Analysis boundary

Resolution is conservative by design. The server returns a result only when
the workspace source establishes a specific binding. Local shadowing,
rebinding, wildcard exports, import cycles, and conditional definitions can
therefore leave a plausible target with no result. The server never guesses.

The server analyzes Python source and the dependency and configuration files
the shipped integrations use. It never evaluates code, imports workspace
modules, runs a type checker, inspects installed package source, executes
notebook cells, or synthesizes locations for the standard library.

## Diagnostics

Workspace diagnostics combine the public integration results. The editor can
receive, among others:

- Python syntax and source-analysis diagnostics;
- missing or ambiguous workspace imports;
- undeclared or version-mismatched dependencies;
- selected unused workspace `from ... import ...` bindings; and
- diagnostics from the root Python project configuration and requirements
  chain when those files participate in dependency analysis.

Unused-import reporting is narrow by design. It skips package initializer
files, star imports, installed and stdlib imports, and bindings that are
visibly re-exported. Only a use that resolves conservatively counts.

## File-operation edits

File rename and delete edits target individual `.py` module files. They
update consumer import statements that can be rewritten without guessing.
Packages, arbitrary string references, attribute use sites, and relative
imports inside a file moved to another package stay as they are. Review the
returned workspace edit before applying it to a structural move.

## Not advertised

The server advertises none of these capabilities: formatting, code
formatting-on-save, range formatting, implementation navigation, document
colors, inline values, monikers, notebook synchronization, call/type hierarchy
beyond direct edges, semantic-token deltas, completion-item resolution,
document-link resolution, code-lens resolution, or inlay-hint resolution.
