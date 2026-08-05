# LSP Reference

`pyinc-tools lsp` is a stdio Language Server Protocol server for Python
workspaces. This reference lists the protocol methods it advertises and the
limits visible to editors. Installation, initialization options, and editor
configuration are in the [`pyinc-tools` guide](pyinc-tools-guide.md).

## Position and document model

- The server negotiates UTF-8, UTF-16, or UTF-32 positions and defaults to
  UTF-16 when the client has no supported preference.
- Ranges are zero-based and end-exclusive.
- Document synchronization is full-text. Incremental patches are not applied.
- Open documents use an in-memory overlay in the workspace mirror. Closing a
  document discards its overlay and returns analysis to the saved file.
- Only paths inside the configured workspace are accepted.

## Method matrix

| Method | Result | User-visible limits |
|---|---|---|
| `initialize`, `initialized`, `shutdown`, `exit` | Ordered server lifecycle and negotiated capabilities. | One workspace per server process. Requests before initialization or after shutdown fail. |
| `textDocument/didOpen`, `textDocument/didChange`, `textDocument/didSave`, `textDocument/didClose` | Maintains editor overlays and republishes diagnostics. | `didChange` expects full document text; `didSave` reads the saved file rather than accepting text. |
| `workspace/didChangeWatchedFiles` | Refreshes changed, created, or deleted files from disk. | Paths outside the workspace are rejected. The built-in polling watcher can provide the same refresh path. |
| `textDocument/documentSymbol` | Functions, classes, variables, and imports in one file. | Python files only; malformed source may produce an empty or partial result. |
| `workspace/symbol` | Case-insensitive name filtering across workspace symbols. | Workspace declarations only; installed and stdlib symbols are not indexed. |
| `textDocument/hover` | Markdown name, kind, annotation, and signature for a resolved declaration. | No evaluated types, docstring rendering, or hover for non-workspace targets. |
| `textDocument/completion` | Lexical names, import-module/name candidates, and statically resolved workspace members. | No auto-imports, snippets, keyword filtering, installed/stdlib members, or members that require runtime type inference. |
| `textDocument/signatureHelp` | Signature and active positional parameter for a resolved workspace call. | No overload selection or inferred callable types; unresolved receivers and non-workspace calls return no result. |
| `textDocument/definition` | Declaration of the resolved workspace symbol. | Installed, stdlib, missing, and ambiguous imports have no navigable location. |
| `textDocument/declaration` | Declaration location for the resolved workspace binding. | Same conservative workspace-only resolution as definition. |
| `textDocument/typeDefinition` | Class location named by a declared annotation. | No type inference; unannotated values and unsupported compound or ambiguous annotations return no result. |
| `textDocument/references` | Verified workspace references, optionally including the declaration. | Dynamic attribute access, unresolved receiver chains, and ambiguous re-exports are omitted. |
| `textDocument/documentHighlight` | Read/write highlights in the current document. | Only verified references to workspace symbols are highlighted. |
| `textDocument/linkedEditingRange` | Same-file ranges that can be edited together. | Cross-file occurrences are excluded; use rename for a workspace edit. |
| `textDocument/prepareRename`, `textDocument/rename` | Validates a target and returns workspace text edits. | Import aliases cannot be renamed from the alias occurrence. Edits are identifier-only and inherit reference-resolution limits. |
| `textDocument/codeAction` | Quick fixes associated with diagnostics. | Only quick fixes are returned. Current fixes remove an unused/unresolved import or retarget an unambiguous single-name import. |
| `textDocument/foldingRange` | Import, class, function, and multiline block folds. | Python source only; malformed files may return no folds. |
| `textDocument/selectionRange` | Nested syntax selections for requested positions. | Each position is handled independently; invalid positions have no useful expansion. |
| `textDocument/documentLink` | Links workspace import names to source files. | Non-workspace, missing, and ambiguous imports do not become links. |
| `textDocument/codeLens` | Reference-count lenses on workspace declarations. | Only declarations with a resolvable workspace identity receive a lens. |
| `textDocument/prepareCallHierarchy`, `callHierarchy/incomingCalls`, `callHierarchy/outgoingCalls` | Direct workspace callers and callees. | Functions, methods, and classes only; dynamic calls and non-workspace targets are omitted. |
| `textDocument/prepareTypeHierarchy`, `typeHierarchy/supertypes`, `typeHierarchy/subtypes` | Direct workspace class relationships. | No metaclass relationship, inferred type, or installed/stdlib base navigation. Clients request each next level separately. |
| `textDocument/inlayHint` | Parameter-name hints for positional arguments. | No variable or return-type hints. Keyword, spread, dynamically resolved, and non-workspace calls are omitted. |
| `textDocument/semanticTokens/full`, `textDocument/semanticTokens/range` | Namespace, class, function, method, parameter, and variable tokens. A `from ... import ...` use is classified by the workspace declaration it resolves to. | No delta response. Use-site classification covers lexical names, not general attribute chains. Imports that resolve outside the workspace, or ambiguously, are left unclassified. |
| `textDocument/diagnostic`, `workspace/diagnostic` | Pull diagnostics with stable unchanged/full reports. | Diagnostics cover the shipped analyses; this is not a general Python type checker or linter. |
| `textDocument/publishDiagnostics` | Push diagnostics after open/change/save/close and filesystem refreshes. | Identical payloads are deduplicated per document. Closed clean documents may receive an empty publication to clear stale editor state. |
| `workspace/willRenameFiles` | Import edits for renamed Python module files. | Package-directory renames, renamed-file internal imports, and attribute-use rewrites are not handled. |
| `workspace/willDeleteFiles` | Removes imports that refer to deleted Python module files. | Package deletes and downstream attribute-use cleanup are not handled. |

## Analysis boundary

Resolution is intentionally conservative. A result is returned only when the
workspace source establishes a specific binding. That means local shadowing,
rebinding, wildcard exports, import cycles, and conditional definitions can
turn a plausible target into no result rather than a guess.

The server analyzes Python source and the dependency/configuration files used by
the shipped integrations. It does not evaluate code, import workspace modules,
run a type checker, inspect installed package source, execute notebook cells,
or synthesize locations for the standard library.

## Diagnostics

Workspace diagnostics combine the public integration results. The editor can
receive, among others:

- Python syntax and source-analysis diagnostics;
- missing or ambiguous workspace imports;
- undeclared or version-mismatched dependencies;
- selected unused workspace `from ... import ...` bindings; and
- diagnostics from the root Python project configuration and requirements
  chain when those files participate in dependency analysis.

Unused-import reporting is deliberately narrow. It skips package initializer
files, star imports, installed/stdlib imports, and bindings that are visibly
re-exported. A use that cannot be resolved conservatively is not counted.

## File-operation edits

File rename/delete edits target individual `.py` module files. They update
consumer import statements that can be rewritten without guessing. They do not
rename packages, update arbitrary string references, rewrite attribute use
sites, or fix relative imports inside a file that moved to a different package.
Review the returned workspace edit before applying it to a structural move.

## Not advertised

The server does not advertise formatting, code formatting-on-save, range
formatting, implementation navigation, document colors, inline values,
monikers, notebook synchronization, call/type hierarchy beyond direct edges,
semantic-token deltas, completion-item resolution, code-lens resolution, or
inlay-hint resolution.
