# LSP Reference

`pyinc-tools lsp` is a stdio Language Server Protocol server for Python
workspaces. This reference lists the protocol methods it handles and the
limits visible to editors. Installation, initialization options, and editor
configuration are in the [`pyinc-tools` guide](pyinc-tools-guide.md).

## Position and document model

- The server negotiates UTF-8, UTF-16, or UTF-32 positions. It defaults to
  UTF-16 when the client prefers none of them.
- Ranges are zero-based and end-exclusive.
- Document synchronization is full-text. The server reads the `text` of the
  last change in a `didChange` as the whole document, so each change must
  carry all of it.
- The text of an open document replaces that file's copy in the workspace
  mirror, a directory on disk. Saving or closing the document copies the
  saved file back into the mirror.
- A request about a document outside the workspace root, or with a scheme
  other than `file`, gets null. `textDocument/diagnostic` gets an empty
  report instead. A notification about such a document is dropped.
- Position conversion ends a line only at `\n`, `\r\n`, or `\r`, as LSP
  does. Completion, prepareRename, rename, semantic tokens, code lens, call
  and type hierarchy, the quick fix that retargets an import, and
  `workspace/willRenameFiles` find a line's text with `str.splitlines()`,
  which also ends a line at a form feed or a Unicode line separator. In a
  file that holds one, those methods read the wrong line after it.
- On Windows, a URI with a percent-encoded drive colon, such as
  `file:///c%3A/src/app.py`, does not map to its file, because the colon is
  not read as a drive.

## Method matrix

| Method | Result | User-visible limits |
|---|---|---|
| `initialize`, `initialized`, `shutdown`, `exit` | Ordered server lifecycle and negotiated capabilities. | One workspace per server process. Requests before initialization or after shutdown fail. |
| `textDocument/didOpen`, `textDocument/didChange`, `textDocument/didSave`, `textDocument/didClose` | Maintains editor overlays and republishes diagnostics. | `textDocument/didChange` expects the full document text. `textDocument/didSave` reads the saved file and ignores any text sent with it. |
| `workspace/didChangeWatchedFiles` | Refreshes changed, created, or deleted files from disk. | A path outside the workspace is skipped, and the rest of the batch is refreshed. The built-in polling watcher can provide the same refresh path. |
| `textDocument/documentSymbol` | Functions, classes, variables, and imports in one file. | Python files only. Malformed source may give an empty or partial result. |
| `workspace/symbol` | Case-insensitive name filtering across workspace symbols. | Workspace declarations only. Installed and stdlib symbols are outside the index. |
| `textDocument/hover` | Markdown name, kind, annotation, and signature for a resolved declaration. | Resolves names as definition does. Workspace targets only, with no evaluated types or docstring rendering. |
| `textDocument/completion` | Lexical names, import-module/name candidates, and statically resolved workspace members. | Returns at most 200 items and sets `isIncomplete` when the list was cut. Offers no auto-imports or snippets. Keywords match by prefix, with no check that one fits the context. Omits installed and stdlib members, and members that need runtime type inference. |
| `textDocument/signatureHelp` | Signature and active positional parameter for a resolved workspace call. | No overload selection or inferred callable types. Unresolved receivers and non-workspace calls return no result. |
| `textDocument/definition` | Declaration of the resolved workspace symbol. | Follows `from m import name as alias` imports, including a chain of them. Installed, stdlib, missing, and ambiguous imports have no navigable location. |
| `textDocument/declaration` | The binding in the current file that the name refers to. | Current file only. A name bound by an import gets its name in that import. Definition follows it to the defining module. |
| `textDocument/typeDefinition` | Class location named by a declared annotation. | Uses declared annotations only, with no type inference. Unannotated values and unsupported compound or ambiguous annotations return no result. |
| `textDocument/references` | Verified workspace references, optionally including the declaration. | Finds a use spelled with the alias of `from m import name as alias`. Misses a use spelled with an alias of that alias, and a use imported from a module that re-exports the alias. Dynamic attribute access, unresolved receiver chains, and ambiguous re-exports are omitted. |
| `textDocument/documentHighlight` | Read/write highlights in the current document. | Only verified references to workspace symbols are highlighted. |
| `textDocument/linkedEditingRange` | Same-file ranges that can be edited together. | Current file only. Returns null when the cursor is on a use spelled with an import alias. Use rename for a workspace edit. |
| `textDocument/prepareRename`, `textDocument/rename` | Validates a target and returns workspace text edits. | Import aliases cannot be renamed from the alias occurrence. Renaming a definition rewrites the imported name on a `from m import name as alias` line and leaves uses spelled with the alias. There is no check that the new name collides with an existing one. Edits change identifiers only and share the reference-resolution limits. |
| `textDocument/codeAction` | Quick fixes associated with diagnostics. | Quick fixes only. The fixes remove an unused or unresolved import, or retarget an unambiguous single-name import. |
| `textDocument/foldingRange` | Import, class, function, and multiline block folds. | Python source only. Malformed files may return no folds. |
| `textDocument/selectionRange` | Nested syntax selections for requested positions. | Each position is handled independently. Invalid positions have no useful expansion. |
| `textDocument/documentLink` | Links workspace import names to source files. | Only imports that resolve to one workspace file become links. |
| `textDocument/codeLens` | Reference-count lenses on workspace declarations. | Only declarations with a resolvable workspace identity receive a lens. |
| `textDocument/prepareCallHierarchy`, `callHierarchy/incomingCalls`, `callHierarchy/outgoingCalls` | Direct workspace callers and callees. | Functions, methods, and classes only. Dynamic calls and non-workspace targets are omitted. |
| `textDocument/prepareTypeHierarchy`, `typeHierarchy/supertypes`, `typeHierarchy/subtypes` | Direct workspace class relationships. | Omits metaclass relationships, inferred types, and navigation to installed or stdlib bases. Clients request each next level separately. |
| `textDocument/inlayHint` | Parameter-name hints for positional arguments. | Parameter-name hints only, with no variable or return-type hints. Keyword, spread, dynamically resolved, and non-workspace calls are omitted. |
| `textDocument/semanticTokens/full`, `textDocument/semanticTokens/range` | Namespace, class, function, method, parameter, and variable tokens. A `from ... import ...` use is classified by the workspace declaration it resolves to. | Full responses only, with no deltas. Use-site classification covers lexical names and skips general attribute chains. Imports that resolve outside the workspace, or ambiguously, are left unclassified. |
| `textDocument/diagnostic`, `workspace/diagnostic` | Pull diagnostics with stable unchanged/full reports. | Not advertised, because the server already pushes the same diagnostics. Answered for a client that sends them anyway. Diagnostics cover the shipped analyses only, with no general Python type checking or linting. |
| `textDocument/publishDiagnostics` | Push diagnostics after `initialized`, open/change/save/close, and filesystem refreshes. Pushes reach the client in the order their analyses ran. The server pushes to every client, whatever pull support the client declares. | Identical payloads are deduplicated per document. A file whose diagnostics clear receives one empty publication. |
| `workspace/willRenameFiles` | Import edits for renamed Python module files. | Module files only. Package-directory renames, imports inside the renamed file, and attribute-use rewrites are out of scope. |
| `workspace/willDeleteFiles` | Removes imports that refer to deleted Python module files. | Module files only. Package deletes and downstream attribute-use cleanup are out of scope. |

## Delivery and errors

One worker thread runs the analyses whose diagnostics are pushed, and sends
them. `initialized`, open, change, save, close,
`workspace/didChangeWatchedFiles`, and each built-in watcher refresh schedule
an analysis on it. A `didChange` that repeats the document's last open or
change text schedules none, unless the document was saved or closed since.
Edits that arrive while an analysis runs are analyzed together when it ends,
so diagnostics follow a pause in typing by one analysis. An analysis holds the
session, so a request can wait for one running analysis to end. `shutdown` is
answered without waiting.

The server sends no requests to the client. It reports no progress, reads no
configuration after `initialize`, and does not cancel work on
`$/cancelRequest`.

| Message | Response |
|---|---|
| A body that is not valid JSON | `-32700` Parse error. The server keeps serving. |
| A body over 16 MiB | Read and discarded, then `-32700` Parse error. The server keeps serving. |
| Broken headers | `-32700` Parse error, then the server exits with status 1. |
| A request the server refuses on its parameters, such as a missing URI or a position outside the document | `-32602` Invalid params. |
| A request the server cannot carry out, such as a rename from an alias occurrence | `-32803` RequestFailed, with a message that says why. |
| A handler that raises | `-32603` Internal error for a request. The traceback goes to stderr, and the server keeps serving. |
| `exit` without `shutdown` | The server exits with status 1. |

`initialize` fails with `-32803` RequestFailed when the root is the home
directory or a filesystem root, or when the workspace holds more Python and
manifest files than the limit. A root that is not an existing directory, or a
workspace that holds a link to a target outside the root, fails it with
`-32602` Invalid params. The guide's
[initialization options](pyinc-tools-guide.md#initialization-options) set the
exclusions and the file limit, and name the watcher values that fail it the
same way.

## Analysis boundary

Resolution is conservative by design. The server returns a result only when
the workspace source establishes a specific binding. Local shadowing,
rebinding, wildcard exports, import cycles, and conditional definitions can
therefore leave a plausible target with no result. The server never guesses.
Go-to-definition, references, and rename find a module-level name whatever
line first binds it, including a name first bound through `global` in a
function, by a loop, or on a continuation line.

The server analyzes Python source and the dependency and configuration files
the shipped integrations use. It infers no types. It never evaluates code,
imports workspace modules, inspects installed package source, or executes
notebook cells. No request navigates into an installed package or the
standard library.

## Diagnostics

Workspace diagnostics combine the public integration results. The guide's
[diagnostic code table](pyinc-tools-guide.md#diagnostic-codes) lists each code
with its severity, source, and meaning, and says when `undeclared-import` and
`unused-import` are reported.

For `unused-import`, any read that resolves to the module-level binding counts
as a use, including an attribute receiver such as `submodule.attr` and a name
inside a string annotation. A function's own local of the same name does not
count.

## File-operation edits

File rename and delete edits target individual `.py` module files. They
update consumer import statements that can be rewritten without guessing.
Packages, arbitrary string references, attribute use sites, and relative
imports inside a file moved to another package stay as they are. Review the
returned workspace edit before applying it to a structural move.

## Not advertised

The `initialize` result leaves out these server capabilities, named as in the
LSP 3.17 `ServerCapabilities`:

- `implementationProvider`, `colorProvider`, `monikerProvider`,
  `inlineValueProvider`, and `executeCommandProvider`;
- `documentFormattingProvider`, `documentRangeFormattingProvider`, and
  `documentOnTypeFormattingProvider`;
- `diagnosticProvider`, because the server pushes diagnostics;
- `notebookDocumentSync`;
- `workspace.workspaceFolders`, so the server analyzes one root and ignores
  folder changes;
- `willSave` and `willSaveWaitUntil` under `textDocumentSync`; and
- `willCreate`, `didCreate`, `didRename`, and `didDelete` under
  `workspace.fileOperations`.

The advertised capabilities omit semantic-token deltas and the resolve
requests for completion items, document links, code lenses, inlay hints, code
actions, and workspace symbols.
