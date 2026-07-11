# `pyinc-tools` — Consumer Tooling Guide

`pyinc-tools` is the editor- and watcher-facing consumer layer that ships with
the `pyinc` distribution. It builds only on the stable `pyinc.integrations`
public surface and does not widen the kernel semver contract. LSP wiring and
push-based filesystem watchers are architectural non-goals for the kernel itself
(see [docs/architecture.md](architecture.md)); they live here.

The package exposes two executables:

- `pyinc-tools analyze` — one-shot workspace or file analysis, with an optional
  threaded `--watch` mode.
- `pyinc-tools lsp` — a stdio LSP adapter with document/workspace symbols,
  diagnostics, hover, goto-definition, and find-references.

Both build on the same `WorkspaceSession` — a mirrored workspace with editor
overlays, so editor edits never touch the user's source tree.

## Install

```bash
pip install pyinc
```

The `pyinc-tools` entry point is installed alongside the library. Zero runtime
dependencies; pure-Python, stdlib-only.

```bash
pyinc-tools --help
pyinc-tools --version
# usage: pyinc-tools [-h] [--version] {analyze,lsp} ...
```

The version is read from the installed `pyinc` distribution metadata. Exit
status `0` means success, `1` means workspace or analysis failure (and is also
the required LSP status for `exit` before `shutdown`), and `2` means invalid
command-line usage.

## `pyinc-tools analyze`

```bash
pyinc-tools analyze <root> [--path PATH] [--watch]
                           [--debounce-ms DEBOUNCE_MS]
                           [--poll-interval-ms POLL_INTERVAL_MS]
                           [--indent INDENT]
```

Runs `WorkspaceSession.analyze_workspace()` (or `analyze_file(PATH)` if `--path`
is given) once and prints the result as JSON. The output payload is the full
`WorkspaceAnalysisResult` dataclass: Python module analysis, workspace symbol
index, dependency check, per-file results, and deduped diagnostics. For a
minimal workspace containing `app.py` (which imports `greet` from `helper.py`):

```json
{
  "root": "/path/to/workspace",
  "files": [
    {
      "path": "/path/to/workspace/app.py",
      "module": { "module": "app", "imports": [ ... ], "definitions": [ ... ],
                  "resolved_imports": [ ... ], "dependencies": [ ... ] },
      "symbols": { "symbols": [
        { "qualified_name": "greet", "kind": "from_import_alias",
          "range": {"start": {"line": 0, "character": 19},
                    "end": {"line": 0, "character": 24}},
          "import_source_module": "helper", "import_source_name": "greet" },
        { "qualified_name": "main", "kind": "function",
          "range": {"start": {"line": 3, "character": 4},
                    "end": {"line": 3, "character": 8}},
          "signature": { "parameters": [], "return_annotation": "str" } }
      ] },
      "dependency_check": { "diagnostics": [], "statuses": [], "undeclared_imports": [] },
      "diagnostics": []
    },
    ...
  ],
  "diagnostics": []
}
```

### Watch mode

```bash
pyinc-tools analyze <root> --watch
```

Wraps the session in a `PollingWorkspaceWatcher` and prints a `{"changed_paths",
"analysis"}` JSON record every time one or more files quiesce for the debounce
window (default 200 ms). `--debounce-ms` tunes the window; `--poll-interval-ms`
tunes how often the watcher re-scans the filesystem (default: half the debounce
window, minimum 50 ms). The watcher runs on a dedicated daemon thread, so the
main loop stays responsive to Ctrl-C.

Polling is the only mode shipped. Push-based watchers (`inotify`, `FSEvents`,
`ReadDirectoryChangesW`) are deliberately out of scope — they would pull in
platform-specific dependencies and do not fit the pure-stdlib boundary. If you
need sub-200 ms latency on large workspaces, wrap the session yourself with a
platform watcher and call `session.refresh_paths(paths)` on each event.

### Live polling from Python

```python
from pyinc_tools.session import PollingWorkspaceWatcher, WorkspaceSession

def on_change(changed: tuple[str, ...]) -> None:
    # Runs on the watcher thread; keep this short or offload to a queue.
    print("changed:", changed)

with WorkspaceSession("/path/to/workspace") as session:
    watcher = PollingWorkspaceWatcher(session, debounce_ms=200)
    with watcher:
        watcher.start(on_change, interval_s=0.1)
        # ... do other work ...
```

`start()` spawns a daemon thread; `stop()` signals it via `threading.Event` and
`join`s with a 5-second timeout. Exceptions raised by `on_change` are forwarded
to the optional `on_error` hook (default: a one-line `stderr` log) without
killing the thread. `poll()` remains available for synchronous use but raises
`RuntimeError` while the thread is running — one driver at a time.

## `pyinc-tools lsp`

```bash
pyinc-tools lsp [--root ROOT]
```

Starts a JSON-RPC-over-stdio LSP server. `--root` is a fallback workspace root
used only if the client omits `rootUri` / `workspaceFolders` on `initialize`.
The protocol surface follows LSP 3.18. The server selects the first supported
entry in the client's `general.positionEncodings` preference list (`utf-8`,
`utf-16`, or `utf-32`) and defaults to UTF-16. Internal integration positions
remain zero-based Unicode-code-point offsets. Inbound framing accepts at most
64 KiB of headers and a 16 MiB message body; malformed, oversized, or excessively
deep JSON is rejected at the protocol boundary.

### Advertised capabilities

```json
{
  "capabilities": {
    "positionEncoding": "utf-16",
    "textDocumentSync": { "openClose": true, "change": 1,
                          "save": { "includeText": false } },
    "documentSymbolProvider": true,
    "workspaceSymbolProvider": true,
    "hoverProvider": true,
    "completionProvider": {
      "triggerCharacters": ["."],
      "resolveProvider": false
    },
    "definitionProvider": true,
    "declarationProvider": true,
    "typeDefinitionProvider": true,
    "referencesProvider": true,
    "documentHighlightProvider": true,
    "linkedEditingRangeProvider": true,
    "renameProvider": { "prepareProvider": true },
    "codeActionProvider": { "codeActionKinds": ["quickfix"] },
    "signatureHelpProvider": {
      "triggerCharacters": ["(", ","],
      "retriggerCharacters": [","]
    },
    "foldingRangeProvider": true,
    "selectionRangeProvider": true,
    "documentLinkProvider": { "resolveProvider": false },
    "codeLensProvider": { "resolveProvider": false },
    "callHierarchyProvider": true,
    "typeHierarchyProvider": true,
    "inlayHintProvider": { "resolveProvider": false },
    "semanticTokensProvider": {
      "legend": {
        "tokenTypes": ["namespace", "class", "function", "method", "parameter", "variable"],
        "tokenModifiers": ["declaration", "async"]
      },
      "full": true,
      "range": true
    },
    "diagnosticProvider": {
      "identifier": "pyinc-tools",
      "interFileDependencies": true,
      "workspaceDiagnostics": true
    },
    "workspace": {
      "fileOperations": {
        "willRename": {
          "filters": [
            { "scheme": "file",
              "pattern": { "glob": "**/*.py", "matches": "file" } }
          ]
        },
        "willDelete": {
          "filters": [
            { "scheme": "file",
              "pattern": { "glob": "**/*.py", "matches": "file" } }
          ]
        }
      }
    }
  },
  "serverInfo": { "name": "pyinc-tools", "version": "<installed version>" }
}
```

### Initialization options

The server honors the following keys under `params.initializationOptions`:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `pyinc.watcher.enabled` | bool | `true` | Start a threaded polling watcher on `initialize` and publish diagnostics when it detects filesystem changes outside the editor (e.g. `git pull`, formatter scripts). |
| `pyinc.watcher.debounceMs` | int | `200` | How long a change must quiesce before the watcher acts on it. |
| `pyinc.watcher.intervalMs` | int | `max(debounceMs / 2, 50)` | How often the watcher re-scans the workspace. |
| `pyinc.workspace.exclude` | string[] | `[]` | Glob patterns omitted from the workspace mirror and watcher. |

If your editor already emits `workspace/didChangeWatchedFiles` reliably, set
`pyinc.watcher.enabled: false` to avoid the redundant thread. The server
deduplicates identical `publishDiagnostics` payloads per URI, so enabling both
channels does not produce duplicate messages.

### Supported LSP methods

| Method | Behavior |
|---|---|
| `initialize` / `shutdown` / `exit` | Standard lifecycle: requests before initialization or after shutdown are rejected, repeated initialization is rejected, `exit` after `shutdown` returns status 0, and `exit` without `shutdown` returns status 1. |
| `textDocument/didOpen` / `didChange` / `didSave` / `didClose` | Open/change edits land in the session overlay (full-text `change: 1`); save/close clear it and resync authoritative disk bytes. |
| `workspace/didChangeWatchedFiles` | Triggers `refresh_paths` for the listed URIs. |
| `textDocument/documentSymbol` | Per-file symbols from `module_symbol_table`. |
| `workspace/symbol` | Case-insensitive substring filter over `workspace_symbol_index`. |
| `textDocument/hover` | Markdown `def foo(x: int) -> int` / `class Foo` / `x: int`, plus a `*re-exported from*` line for import aliases. |
| `textDocument/definition` | Single `Location` via position-based `symbol_at`; follows conservative cross-module re-exports and directly imported dotted module chains. |
| `textDocument/declaration` | Single-entry `Location[]` pointing at the *binding statement* in the current file (distinct from `textDocument/definition`, which follows `import` / `from … import` chains through to the imported target's file). The cursor is resolved in the current lexical scope and the returned range is the binding's exact `SourceRange`. For workspace `function` / `class` / `variable` / `class_variable` / `method` symbols, the declaration coincides with the definition. Import aliases point at their local binding even when the imported module is outside the workspace. Whitespace positions, unknown identifiers, and files outside the workspace return `[]`. |
| `textDocument/typeDefinition` | `Location[]` pointing at the declared type of the symbol under the cursor. The symbol's annotation is parsed as a Python expression and its names are resolved conservatively. Generics (`list[Foo]`), unions (`Foo \| Bar`), and qualified attribute types (`pkg.Foo`) contribute one exact `SourceRange` per workspace-resolved type, deduplicated by `(path, range)`. Whole-string forward references are unwrapped once. Unsupported or non-workspace targets return `[]`. |
| `textDocument/references` | `Location[]` via position-based `symbol_at` followed by `find_references(SymbolId)`; honors `context.includeDeclaration` and emits each occurrence's exact `SourceRange`. Only workspace-resolved targets are indexed. |
| `textDocument/documentHighlight` | `DocumentHighlight[]` for the symbol under the cursor, scoped to the current file. The declaration site is reported with `kind: 3` (Write); other occurrences with `kind: 1` (Text). Every occurrence uses its exact `SourceRange`. Cross-file references returned by `find_references` are filtered out — workspace-wide highlighting is `textDocument/references`'s job. Stdlib / installed / ambiguous targets return `[]`. |
| `textDocument/linkedEditingRange` | `{ranges}` or `null`. The range set is exactly `textDocument/documentHighlight`'s file-scoped occurrences for the symbol under the cursor, so every range covers the same identifier and can be edited simultaneously. The optional `wordPattern` is omitted to avoid imposing an ASCII-only pattern on Unicode Python identifiers. In-file only — workspace-wide edits still go through `textDocument/rename`. Unknown identifiers, whitespace positions, non-workspace targets, and files outside the workspace return `null`. |
| `textDocument/prepareRename` / `textDocument/rename` | `prepareRename` resolves the cursor to a workspace `SymbolId` and returns its exact identifier range and placeholder, or `null` when the target cannot be renamed safely. `rename` resolves the same cursor target and returns a `WorkspaceEdit` over the declaration and every reference proven by the shared lexical resolver; invalid identifiers, keywords, and speculative targets are rejected. |
| `textDocument/foldingRange` | `FoldingRange[]` for the requested document. AST-walked: `def`/`async def`/`class` blocks emit a generic-region fold (no `kind` field) starting at the header line — or the first decorator line if any decorators are attached — and ending at the AST range; class bodies recurse so methods fold independently. Every entry includes `startLine`, `startCharacter`, `endLine`, and `endCharacter`, with scalar character fields converted to the negotiated position encoding. Consecutive top-level `import` / `from … import` statements are coalesced into one `kind: "imports"` fold; multi-line parenthesised imports collapse on their own. Single-line definitions and single-line single imports emit no fold. Files that fail to parse return `[]`. |
| `textDocument/selectionRange` | `SelectionRange[]` (one entry per requested position). Each entry is a chain of nested ranges encoded via the recursive `parent` field: innermost first, each parent strictly contains its child. The chain is computed by parsing the document once, normalizing AST byte columns to code points, and collecting every node whose `SourceRange` contains the cursor; duplicates are collapsed and the result is filtered to a strict containment chain ordered by length. Files that fail to parse, positions outside the source, or positions that no AST node covers fall back to a single zero-width range at the cursor so the LSP result length always matches `params.positions` length. |
| `textDocument/documentLink` | `DocumentLink[]` for the requested document. The server walks the document's AST and emits one link per `ast.alias` whose enclosing `Import` / `ImportFrom` resolves to a workspace file. For `import M [as alias]` the link spans the whole `M [as alias]` clause and points at `M`'s resolved file; for `from M import a, b` each imported name is linked individually to its own resolved path (a submodule import like `from pkg import child` resolves to `child.py`, not `pkg/__init__.py`). Stdlib / installed / missing / ambiguous targets and `from M import *` emit no link. Files that fail to parse return `[]`. |
| `textDocument/codeLens` | `CodeLens[]` for the requested document. One lens is emitted above every top-level `def` / `async def` / `class` in the file; the range spans the bare-name identifier on the definition's header line (decorated definitions still report on the `def` line, not the decorator line). The lens's `command` is `{title: "<N> reference[s]", command: ""}`, where `N` is the count returned by `find_references` with `include_declaration=False` restricted to workspace targets. Methods (`kind: "method"`), nested classes (dotted qualified names), class variables, and import aliases intentionally emit no lens; this view remains a top-level API overview. Non-workspace targets, unparseable files, and files with no qualifying symbols return `[]`. The empty `command` string follows pylsp's convention so the lens displays as plain hint text without binding to an editor-specific action. |
| `textDocument/completion` | `CompletionItem[]` (`{isIncomplete: false, items}`) for the caret, drawn only from bindings proven by the shared lexical scope graph — never inferred runtime types. The caret line is repaired to `pass` before analysis so a mid-edit `owner.` still resolves. Contexts: bare-name prefix (visible current-file bindings, workspace top-level module names, keywords); attributes whose complete owner chain is proven to be a workspace module or class; `self.` / `cls.` inside the owning method; directly annotated values whose declared type resolves to a workspace class; and import positions (`from pkg import <prefix>`, `import <prefix>`). Class views are flattened over workspace base classes depth-first, left-to-right, first-definition-wins (not C3 MRO), bounded at `MAX_BASE_DEPTH = 8`. Unproven or rebound chains, stdlib / installed owners, unsupported annotations, strings, and comments yield `[]`. Each item carries `label` / `kind` / `detail` / `sortText`; `resolveProvider` is `false`. |
| `textDocument/signatureHelp` | `SignatureHelp` for the call expression enclosing the cursor. A forward source scanner finds the topmost open `(` whose preceding token is a usable identifier, counts top-level commas to derive `activeParameter`, and resolves the identifier through the shared position-based lexical resolver. Bare-name calls and proven dotted module calls such as `M.foo(` and `pkg.sub.foo(` are supported. Functions surface their declared signature; classes surface `<Class>.__init__` with a leading `self` / `cls` stripped, or an empty constructor signature when no `__init__` is defined. Parameter default values are rendered into the label (`name: ann = default` / `name=default`, extracted from the defining file's source). Stdlib / installed / ambiguous targets, unproven or rebound attribute chains, subscripted calls (`factory[T](`), and `def`/`class` definition headers all return `null`. Parameters use LSP `[start, end]` substring offsets into the signature label. |
| `textDocument/prepareCallHierarchy` | `CallHierarchyItem[]` or `null`. Resolves the identifier under the cursor through the shared position-based lexical resolver; if the target is a workspace `function`, `method`, or `class`, returns a single item describing the declaring def/class. The item's `range` covers the whole def block (including decorator lines if any), and `selectionRange` is the bare-name span on the header line. The item's `data` field carries `{"path": str, "qualified_name": str}` which the server reads back on `callHierarchy/incomingCalls` and `callHierarchy/outgoingCalls`. Variables, import aliases, `from_import` aliases, wildcard-import stubs, and stdlib / installed / ambiguous / missing targets return `null`. |
| `callHierarchy/incomingCalls` | `CallHierarchyIncomingCall[]`. Calls `find_references(include_declaration=False)` on the item's target and groups references by their innermost enclosing workspace-known def/class in the same file (qualifier follows `module_symbol_table`'s ClassDef-only nesting scheme, so a reference inside `class C: def m(self): ...` is attributed to `C.m`). References inside nested function bodies bubble up to the next enclosing function or method that's in the symbol table; module-top-level references are dropped because there is no caller item to attribute them to. `fromRanges` are AST occurrence ranges, including the rightmost-attribute span for `M.foo()` style references. |
| `callHierarchy/outgoingCalls` | `CallHierarchyOutgoingCall[]`. Parses the item's declaring file, locates the matching `def` / `async def` / `class`, and walks its body without descending into nested callable or class scopes. Bare calls and attribute calls whose complete receiver chain is statically proven resolve through the shared lexical resolver. Workspace `function` / `method` / `class` targets contribute callees; unproven or rebound chains, subscripted calls, and lambda calls produce no callee. `fromRanges` are exact callee ranges. |
| `textDocument/prepareTypeHierarchy` | `TypeHierarchyItem[]` or `null`. Resolves the identifier under the cursor through the shared position-based lexical resolver; if the target is a workspace `class`, returns a single item describing the declaring `ClassDef`. The item's `range` covers the whole class block (including decorator lines if any), and `selectionRange` is the bare-name span on the header line. The item's `data` field carries `{"path": str, "qualified_name": str}` which the server reads back on `typeHierarchy/supertypes` and `typeHierarchy/subtypes`. Functions, methods, variables, import aliases, `from_import` aliases, wildcard-import stubs, and stdlib / installed / ambiguous / missing targets all return `null`. |
| `typeHierarchy/supertypes` | `TypeHierarchyItem[]`. Parses the item's declaring file, locates the matching `ClassDef`, and resolves each base through the shared lexical resolver. `Subscript` bases (`Generic[T]`, `Base[T]`) are unwrapped to their value once. Bare names and proven dotted workspace-module chains can resolve; starred bases, call expressions, and unproven or rebound chains produce no entry. Only workspace `class` targets contribute items; stdlib / installed / ambiguous / missing bases are dropped. Duplicates by `(path, qualified_name)` are collapsed. |
| `typeHierarchy/subtypes` | `TypeHierarchyItem[]`. Walks the workspace once via `workspace_analysis`, visiting every `ClassDef` recursively (qualified-name nesting follows `module_symbol_table`: `Outer.Inner`). For each candidate's `bases` list, each base is unwrapped (subscript dropped) and resolved through the candidate's module imports using the same rules as `typeHierarchy/supertypes`; a candidate is a subtype iff at least one resolved base points at the target `(path, qualified_name)`. The target itself is excluded from the result. Duplicates by `(path, qualified_name)` are collapsed; output is sorted by `(path, qualified_name)`. Only direct subtypes are returned — clients drill down by calling the endpoint recursively on each result. |
| `textDocument/inlayHint` | `InlayHint[]` for parameter-name hints at call sites inside the requested `range`. The server walks `ast.Call` nodes whose callee resolves through the shared lexical resolver to a workspace `function` or `class`, looks up the signature, and emits one hint per positional argument with label `"<paramname>:"`, `kind: 2` (Parameter), and `paddingRight: true`. Class constructions surface `<Class>.__init__` with a leading `self` / `cls` stripped. Hints are suppressed when the argument name already matches the parameter; `*args`, starred arguments, unproven or rebound chains, non-workspace targets, subscripted calls, keyword arguments, and unparseable files are handled conservatively. |
| `textDocument/semanticTokens/full` | `SemanticTokens` payload (`{data: int[]}`) for the requested document. The server parses the document (overlay or on-disk) once with `ast.parse` and emits one token per `def` / `async def` / `class` header (type `function` / `method` / `class`, modifier `declaration`, plus `async` for `async def`), per function parameter (type `parameter`, modifier `declaration`), and per resolved bare `ast.Name` use. Use-site classifications combine the shared lexical scope tree with the module symbol table: a parameter or local variable that shadows a module binding keeps its local token kind. Attribute uses and unresolved cross-module re-exports are skipped. Tokens are emitted in `(line, character)` order and encoded into the LSP wire format as five integers per token `[deltaLine, deltaStart, length, tokenType, tokenModifiers]`, where `tokenModifiers` is a bitmask over the legend positions. Files that fail to parse return `{"data": []}`. |
| `textDocument/semanticTokens/range` | `SemanticTokens` payload (`{data: int[]}`) for the slice of the requested document covered by the half-open LSP range `[params.range.start, params.range.end)`. Implementation reuses the same full-document AST walk as `semanticTokens/full` and then filters by token start position: a token at `(line, character)` is included iff its start is `>= range.start` and `< range.end`. The retained tokens are encoded into the wire format on their own (the delta cursor is reset, so the first emitted token's `deltaLine` / `deltaStart` are absolute). Files that fail to parse and missing files return `{"data": []}`. No server-side per-document state is held — every `range` request is independent. |
| `workspace/willRenameFiles` | `WorkspaceEdit` or `null`. For each `{oldUri, newUri}` pair the server walks every Python file in the workspace and emits text edits that update the `import` and `from` statements which reference the renamed file's module name. Three rewrite shapes: (1) `import <old_module> [as alias]` → the dotted-module span becomes `<new_module>` (the `as` clause is preserved); (2) `from <old_module> import …` → the dotted-module span (including any leading dots) is rewritten — the existing `level` is preserved when both old and new modules live under the same package anchor, otherwise the statement is rewritten to absolute form (`from <new_module> import …`, `level == 0`); (3) `from <pkg> import <leaf> [as alias]` where `<pkg>.<leaf> == old_module` and `old_module`/`new_module` share the same parent package → the leaf is rewritten to `new_module`'s leaf (`as` clause preserved). Renames are silently skipped when either path is outside the workspace, isn't a `.py` file, is `__init__.py` (package rename — separate feature), or yields the same module name; the request returns `null` when no edits are needed. Multiple renames in one request are batched against the *current* workspace state — no chaining is attempted. |
| `workspace/willDeleteFiles` | `WorkspaceEdit` or `null`. For each `{uri}` entry the server walks every Python file in the workspace and emits text edits that remove the `import` and `from` statements which would become broken once the file is gone. Three deletion shapes: (1) `import <deleted_module> [as alias]` → the whole statement is removed (range spans the line including its trailing newline) when it's the only alias; otherwise only the dead alias plus its adjacent comma is removed (`import a, b` with `a` deleted → `import b`); (2) `from <deleted_module> import …` → the whole statement is removed (every imported name's source module is gone); (3) `from <pkg> import <leaf> [as alias]` where `<pkg>.<leaf> == deleted_module` → the whole statement is removed when it's the only imported name, else only the dead leaf plus its adjacent comma is removed. Deletions are silently skipped when the path is outside the workspace, isn't a `.py` file, or is `__init__.py` (package delete — separate feature); the request returns `null` when no edits are needed. Importers that are themselves part of the same delete batch are skipped (no point editing a file the client is about to remove). Multiple deletions in one request are batched against the *current* workspace state. |
| `textDocument/codeAction` | `CodeAction[]` (all `kind: "quickfix"`), or `[]`. The server recomputes diagnostics for the document (stateless, pull-diagnostics style), keeps those whose line falls inside the request `range` (character offsets are not used to trim — anchoring is line-granular), and turns each into a fix. Each returned action echoes its anchor `Diagnostic` in `diagnostics` and carries a `WorkspaceEdit` under `edit` (`{"changes": {uri: [TextEdit]}}`). Three anchors are handled: `unused-import` → *"Remove unused import 'name'"* (removes the alias, or the whole statement when it is the sole name); `missing-import` → *"Remove unresolvable import"* (same deletion machinery); `unresolved-symbol` → *"Remove import of 'name'"* plus, **only** when exactly one workspace module exposes a top-level `function` / `class` / `variable` of that name in `workspace_symbol_index` and the statement imports just that one name, *"Import 'name' from '<module>'"* (rewrites the from-module span). `context.only` is honored — a request that does not admit `quickfix` gets `[]`. Files that do not parse yield `[]` (every fix needs the AST). |
| `textDocument/diagnostic` | Pull-model (LSP 3.18) single-document report. Runs `analyze_file` on the requested document and returns a `RelatedFullDocumentDiagnosticReport` (`{kind: "full", resultId, items}`) whose `items` are the same `Diagnostic` objects the push channel emits for that file (codes: `missing-import`, `ambiguous-import`, `undeclared-import`, `unresolved-symbol`, `ambiguous-symbol`, `unused-import`, plus `python_source` parse errors). The `unused-import` items carry `severity: 4` (Hint) and `tags: [1]` (Unnecessary) so editors fade the binding. `resultId` is a SHA-256 over the diagnostic signatures — including `tags`, so a diagnostic gaining or losing a tag re-issues — so when the client echoes a matching `previousResultId` the server replies `{kind: "unchanged", resultId}` instead of resending. A clean file returns a full report with `items: []`; a pull for a URI outside the workspace returns an empty full report rather than failing. |
| `workspace/diagnostic` | Pull-model (LSP 3.18) workspace report. Runs `analyze_workspace` once and returns `{items: [...]}` with one report per analyzed `.py` file (plus any config / requirements file that carries dependency or requirements-parse diagnostics), sorted by path. Recursive requirements failures such as `missing-requirements-file` and `cycle` are reported against the real root `requirements.txt`. Each report is a `WorkspaceFullDocumentDiagnosticReport` (`{kind: "full", uri, version: null, resultId, items}`); files that are now clean still get an empty-`items` report so the client can clear stale problems. `version` is always `null` (the session tracks overlays, not LSP document versions). When the client supplies `previousResultIds` (`[{uri, value}]`), any file whose freshly computed `resultId` matches its previous value is returned as `{kind: "unchanged", uri, version: null, resultId}`. The pull channel is stateless — `resultId`s are pure functions of the current diagnostics, so it coexists with the push channel without extra bookkeeping. |

## Editor wiring

pyinc-tools is a generic stdio LSP server; any LSP client that speaks stdio can
attach it. It currently targets Python source files (`.py`).

### Neovim (built-in `vim.lsp`)

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = "python",
  callback = function(args)
    vim.lsp.start({
      name = "pyinc-tools",
      cmd = { "pyinc-tools", "lsp" },
      root_dir = vim.fs.root(args.buf, { "pyproject.toml", ".git" }),
    })
  end,
})
```

### Emacs (`eglot`)

```elisp
(with-eval-after-load 'eglot
  (add-to-list 'eglot-server-programs
               '(python-mode . ("pyinc-tools" "lsp"))))
```

If you already run `pyright` or `pylsp` against Python, either run pyinc-tools
in addition (eglot supports multiple servers with `:add-server`) or pick one —
pyinc-tools focuses on incremental symbol analysis and dependency diagnostics,
not type checking.

### VS Code

VS Code does not run stdio language servers directly from a CLI; a thin
extension that wraps `pyinc-tools lsp` with `vscode-languageclient` is required.
No first-party extension ships in this release. A generic "LSP bridge"
extension (e.g. one that lets the user point at an arbitrary stdio server
command) is sufficient.

## Overlay model

`WorkspaceSession` copies relevant Python source and supported configuration
files into a temporary mirror root once on construction. The root
`requirements.txt` file's recursive in-workspace `-r` / `-c` closure is copied
regardless of filename suffix; constraint contents remain outside declared
dependency evaluation. Ignored directories and configured exclusion globs are
omitted. Escaping symlinks and Windows junctions are rejected; directory links
are not traversed.
Editor buffer edits arrive via
`set_overlay(path, text)`
and are written to the mirror, never to the user's source tree. Disk edits
picked up by the watcher (or `workspace/didChangeWatchedFiles`) are synced back
from the real path into the mirror via `refresh_paths`. Analysis always runs
against mirror-root paths; every result path is then remapped back to the real
root before it is returned to the caller.

Consequences:

- An editor crash, kill -9, or ungraceful shutdown leaves the user's files
  exactly as last saved. The mirror is a `tempfile.TemporaryDirectory` and is
  cleaned on `session.close()`.
- `WorkspaceSession.source_text(path)` returns the overlay text if one is set,
  else the on-disk contents decoded with Python's PEP 263/BOM rules.
- Watcher snapshots use content hashes, so edits are detected even when size
  and modification time are unchanged.
- File symlinks are rejected; directory symlinks and Windows junctions are not
  traversed. Copy and refresh reads revalidate components at the read boundary
  and verify the opened target's identity and regular-file type. POSIX traversal
  opens components without following links and reopens the parent to compare its
  filesystem identity immediately before opening the file. POSIX cannot portably
  prevent a hostile rename in the final interval between that check and the open,
  so workspace roots must not be renamed concurrently by non-cooperating
  processes. This keeps one lexical workspace path mapped to one mirror path
  during cooperative synchronization.
- Position-based `WorkspaceSession.symbol_at(path, SourcePosition(...))`
  resolves through the mirror and returns a real-workspace `SymbolId`.
  `WorkspaceSession.find_references(symbol_id)` accepts only that resolved ID.

All public source geometry is a zero-based, end-exclusive `SourceRange` whose
columns count Unicode code points. The LSP boundary converts those positions to
the negotiated UTF-8, UTF-16, or UTF-32 encoding.

Internally, `WorkspaceSession` remains the lock-owning façade while focused
modules own the implementation details: `_document` handles geometry,
`_models` owns shared result types, `_analysis` contains pure analysis and
public-surface resolution helpers, `_edits` contains pure edit construction,
`_workspace` owns mirroring and polling, and `_jsonrpc` owns raw message
framing. These modules do not import `session`, and the tools layer consumes
integrations only through the public `pyinc.integrations` surface.

## Supported vs. not yet supported

**Supported by the v3 contract:**

- Document symbols (all eight kinds: `function`, `method`, `class`,
  `class_variable`, `variable`, `import_alias`, `from_import_alias`,
  `wildcard_import_stub`).
- Workspace symbols with case-insensitive substring filter.
- Completion, via `textDocument/completion` (advertised as
  `completionProvider` with `.` as the trigger character). Completion is
  **declaration-driven**: candidates come from bindings in the shared lexical
  scope graph and conservative import resolution, never from inferred runtime
  types — the same
  stance the goto/type-definition features take. Because a mid-edit buffer is
  often unparseable at the caret (e.g. a trailing `owner.`), the server repairs
  the caret line to `pass` before analysis, which keeps every top-level import
  and definition intact for resolution while leaving the rest of the file
  untouched. The recognised contexts are:
  - **Bare-name prefix** (`wor|`): module-level symbols from the current file's
    `ModuleSymbolTable`, workspace top-level module names, and Python keywords.
  - **Attribute** `M.<prefix>` where `M` is a bare name: `M` is resolved via
    the position-based lexical resolver; if it is a **workspace module** its module-level exports
    are offered, and if it is a **workspace class** its **class view** (methods
    and class variables) is offered — flattened across workspace base classes
    (see *inheritance*, below).
  - **`self.` / `cls.` inside a method**: the caret's enclosing method is
    located in the (caret-line-repaired) buffer, and the owner must be that
    method's literal first parameter (`self` or `cls`). The enclosing class's
    members then come from the `class_model` surface: `self.` offers the
    **instance view** (methods + class variables + `self.x` instance
    attributes), `cls.` offers the **class view** (methods + class variables, no
    instance attributes). A `self.` in a `cls`-method (or at module level, or
    inside a nested function / lambda that rebinds the first parameter) offers
    nothing — the owner is not the innermost callable's first parameter.
  - **Annotated bare name** (`w.<prefix>` where `w` is neither `self`/`cls` nor
    resolvable as a module/class): the name's **declared annotation** is
    followed to a workspace class and that class's **instance view** is offered.
    The declaration is resolved in priority order: the enclosing function's
    parameter annotation first, then the nearest preceding `w: T` local
    re-annotation in that function, then a module-level annotated `variable`.
    Only a whitelist of annotation shapes resolves: a bare `Name` (`Widget`),
    a one-hop attribute of a bare name
    (`helpers.Widget`), or a whole-string forward reference (`"Widget"`,
    `"helpers.Widget"`) unwrapped exactly once. Subscripted, union, deep-dotted,
    and callable shapes (`list[Widget]`, `Widget | None`, `pkg.sub.Widget`,
    `Callable[[], Widget]`) resolve to nothing rather than half-inferring the
    wrapped class. When the same bare name already resolves as a module or class
    via the attribute path *and that class-object view yields any items*, it
    wins and the annotation is not consulted; when the view is empty (an empty
    class, or none of its members match `prefix`), the local annotation is
    consulted instead.
  - **Dotted attribute owner** `pkg.sub.<prefix>` / `pkg.sub.C.<prefix>` /
    `M.C.<prefix>`: every step must be proven to be a workspace module or
    class. A workspace module offers its exports; a proven class offers its
    members. Unimported, rebound, ambiguous, instance, and stdlib/installed
    chains yield nothing — no runtime-type inference.
  - **Import context**: `from <pkg> import <prefix>` offers the workspace
    `pkg`'s module-level names (via `workspace_symbol_index`, so it works even
    while the current file is unparseable); `import <prefix>` / `from <prefix>`
    offers workspace module names.

  **Inheritance.** The `class_model`-backed views (bare `Foo.`, `self.`, `cls.`,
  and annotated-name owners) are **flattened** over workspace base classes. The
  integration walks the base graph **depth-first, left-to-right**, keeping the
  **first definition** of each member name so a derived member shadows a base
  member of the same name (a single entry, at the derived declaration site). It
  is deliberately **not** C3 MRO. Only bases that resolve to a workspace class
  contribute; stdlib / installed / missing / ambiguous bases contribute nothing
  (`class D(OrderedDict)` gains no `dict` members). A subscripted base
  (`Base[int]`) is followed through to `Base`; a starred base (`*mixins`) is
  never followed. Cycles are cut by a visited set and the walk is bounded at
  `MAX_BASE_DEPTH = 8` derivations.

  Each item carries a `label`, a `kind` (mapped from the symbol kind:
  function/method/class/field/variable/module/keyword), and a `detail` (the
  signature label for callables, reused from signature help, or the declared
  annotation for variables). A caret inside a string or line comment, or an
  attribute owner that does not resolve to the workspace, returns no items. The
  consumer entrypoint is `WorkspaceSession.completions_at(path, line,
  character)`, returning a tuple of `CompletionItem(label, kind, detail,
  sort_text)`.
- Diagnostics: syntax errors, unresolved imports, undeclared dependencies from
  `dependency_check`, and unused workspace `from … import` bindings
  (`unused-import`, severity Hint + the `Unnecessary` tag). Delivered over both
  the push channel (`textDocument/publishDiagnostics`) and the pull-diagnostic
  channel defined by LSP 3.18 (`textDocument/diagnostic` +
  `workspace/diagnostic`, advertised via
  `diagnosticProvider`). The pull channel is stateless: `resultId`s are a
  SHA-256 over the diagnostic signatures (tags included), so an unchanged file
  answers with an `unchanged` report when the client echoes its
  `previousResultId`.
- Code actions, via `textDocument/codeAction` (advertised as
  `codeActionProvider: {codeActionKinds: ["quickfix"]}`). Diagnostics-anchored
  quick fixes only — no refactorings. The provider recomputes diagnostics for
  the document, keeps those intersecting the requested range by line, and
  offers: **remove an unused import** (`unused-import`), **remove an
  unresolvable import** (`missing-import`), and for `unresolved-symbol` a
  **remove the offending import** action plus a **retarget the from-module**
  action when exactly one workspace module exposes a top-level symbol of that
  name and the statement imports just it. Every action is `kind: "quickfix"`,
  echoes its anchor diagnostic, and ships a `WorkspaceEdit`
  (`{"changes": {uri: [TextEdit]}}`). The consumer entrypoint is
  `WorkspaceSession.code_actions_for_range(path, start_line, start_character,
  end_line, end_character)`, returning a tuple of `CodeAction(title, kind,
  diagnostic, edits)` where each `CodeActionEdit(path, range, new_text)` uses
  the public `SourceRange` contract.
- Hover on local symbols.
- Goto-definition, following `import` / `from X import Y` / single-level
  `from X import *` chains through the position-based lexical resolver.
- Goto-declaration, via `textDocument/declaration` (advertised as
  `declarationProvider: true`). Returns the *binding statement* in the
  current file for the symbol under the cursor — distinct from
  `textDocument/definition`, which follows `import` / `from … import`
  chains through to the imported target's file. The cursor is resolved in its
  lexical scope with `symbol_at`, and the returned range is the binding's exact
  `SourceRange`. For workspace
  `function` / `class` / `method` / `variable` / `class_variable`
  symbols, the declaration coincides with the definition (the
  def/class/assignment line). For `import_alias` and `from_import_alias`
  symbols, the declaration is the local `import` / `from … import` binding,
  even when the imported target is outside the workspace. Wildcard-import
  stubs have no name-specific local binding, so a reference supplied only by
  `from M import *` has no declaration result. Unknown identifiers, whitespace
  positions, and paths outside the workspace return no result. The consumer
  entrypoint `WorkspaceSession.declaration_location_at(symbol_id)` returns a
  `DeclarationLocation(path, range)` or `None`.
- Type definition, via `textDocument/typeDefinition` (advertised as
  `typeDefinitionProvider: true`). For the symbol under the cursor, the
  server resolves its exact `SymbolId`, reads the declared annotation
  (including parameter/local/class-variable annotations and function or method
  returns), and resolves each source-backed type occurrence at its real lexical
  position. Bare names and dotted attributes are accepted only when every
  module/class step is proven. Generics (`list[Foo]`), unions (`Foo | Bar`), and
  qualified attribute types (`pkg.sub.Foo`) all yield one location per
  workspace-resolved type, deduplicated by `(path, range)`. Whole-string forward references
  (`x: "Foo"`, `def f() -> "Foo"`) are unwrapped exactly once; partial
  string annotations (`x: "Foo" | None`) skip the string portion. Classes
  return their own definition location. Stdlib / installed / ambiguous
  type names are skipped via the resolver's classification. The consumer
  entrypoint `WorkspaceSession.type_definitions_at(symbol_id)`
  returns a tuple of `TypeDefinitionLocation(path, range)` dataclasses.
- Find references, via `find_references(SymbolId)`. Bare-name and proven
  attribute occurrences are verified through the same lexical resolver as
  goto-definition. Exact per-occurrence `SourceRange` values are returned, so
  editors can highlight each match precisely. Honors
  `context.includeDeclaration`. Forward-reference string annotations (e.g.
  `def g(a: 'Foo')`, `x: 'list[Foo]'`, `x: 'pkg.Foo'`, `'Foo | None'`) are
  also scanned: the walker re-parses the string with `ast.parse(..., mode="eval")`
  and emits `Name` / `Attribute` occurrences from the inner expression with
  offsets translated back to the file. Strings spanning multiple lines,
  triple-quoted strings, strings containing escape sequences, and
  implicitly-concatenated string literals are skipped to keep offset
  reconstruction unambiguous. Module-attribute access on an `import M` /
  `import M as alias` binding (e.g. `M.foo()`, `alias.foo()`) is also
  counted: the occurrence walker carries the LHS Name as a hint, and the
  verifier resolves the LHS through its `import_alias` to the target's
  defining module before checking the attribute name. Cross-module
  re-exports inside the imported module hop through transparently. Only
  the rightmost-attribute span is reported (the leading `M.` is left
  alone), so rename rewrites just the attribute portion.
- Document highlight, via `textDocument/documentHighlight` (advertised as
  `documentHighlightProvider: true`). Returns highlight ranges for the symbol
  under the cursor scoped to the current file; the declaration site uses
  `DocumentHighlightKind.Write` (3) and other occurrences use
  `DocumentHighlightKind.Text` (1). Every occurrence already carries its exact
  public range. Cross-file references that
  `find_references` would return are filtered out — full workspace-wide
  results are still available via `textDocument/references`. The consumer
  entrypoint `WorkspaceSession.find_document_highlights(path, symbol_id)`
  returns a tuple of `DocumentHighlight(range, kind)` dataclasses with `kind`
  typed as `Literal["text", "read", "write"]`.
- Linked editing, via `textDocument/linkedEditingRange` (advertised as
  `linkedEditingRangeProvider: true`). Returns the set of ranges in the
  current file that an editor should mirror while the user types — every
  range has identical content, so editing one updates them all live. The
  range set is exactly the file-scoped occurrences that
  `textDocument/documentHighlight` reports for the symbol under the cursor
  (the declaration plus every verified bare-name or proven-attribute
  reference), so all spans
  cover the same bare identifier. The optional protocol `wordPattern` is
  omitted so clients do not apply an ASCII-only restriction to valid Unicode
  Python identifiers. This is in-file only and
  intentionally lighter than `textDocument/rename`: it does not touch other
  files, so workspace-wide renames still go through `rename`. Unknown
  identifiers, whitespace cursor positions, non-workspace targets (stdlib /
  installed / ambiguous / missing), and files outside the workspace return
  `null`. The consumer entrypoint
  `WorkspaceSession.linked_editing_ranges_at(path, symbol_id)` returns a tuple
  of `LinkedEditingRange(range)` dataclasses.
  Lives entirely on top of the stable `pyinc.integrations` public surface
  (via `find_references`) — no kernel contract change and no new
  integration-layer surface.
- Threaded live polling via `PollingWorkspaceWatcher.start(...)`. LSP server
  starts one by default; opt out with `initializationOptions.pyinc.watcher.enabled=false`.
- `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:` import blocks — the symbol
  walker recognizes this pattern and walks its body for `import` / `from X import Y`
  statements. Hover and goto-definition work for any bare identifier that matches
  a symbol name, including identifiers that appear inside string annotations
  (e.g. `x: "Foo"`), since the identifier-at-position parser operates on raw
  source characters.
- `try: … except ImportError:` / `except ModuleNotFoundError:` / `except (ImportError,
  ModuleNotFoundError):` guard blocks at module top level — the symbol walker
  recognizes these patterns and walks their bodies for `import` / `from X import Y`
  statements. Symbols bound inside these guards appear in hover and goto-definition
  exactly as unconditional imports do; no "conditional top-level binding" impurity
  marker is recorded for files whose only conditional blocks are import-error guards.
- Signature help, via `textDocument/signatureHelp` (advertised as
  `signatureHelpProvider: {triggerCharacters: ["(", ","],
  retriggerCharacters: [","]}`). A forward source scanner skips comments and
  string literals (single, double, and triple-quoted) and tracks a stack of
  open brackets; the topmost open `(` whose preceding token is a usable
  identifier identifies the function being called, and the accumulated
  comma count yields `activeParameter`. Proven module chains such as `M.foo(`
  and `pkg.sub.foo(` are resolved through their lexical import binding; an
  unimported or rebound chain returns no result. Bare-name identifiers use the
  same public resolver, so cross-module re-exports hop transparently. Functions
  surface
  their declared signature; classes surface `<Class>.__init__`'s signature
  with a leading `self` / `cls` stripped, or an empty constructor signature
  when no `__init__` is defined. Parameter default values are rendered into
  the label (`name: ann = default` / `name=default`), read from the defining
  file's source since `Parameter` carries no default.
  Parameters are reported with LSP `[start, end]` substring offsets into the
  signature label so editors can highlight the active parameter precisely.
  The consumer entrypoint `WorkspaceSession.signature_help_at(path, line,
  character)` returns a `SignatureHelp(label, parameters, active_parameter)`
  dataclass.
- Folding ranges, via `textDocument/foldingRange` (advertised as
  `foldingRangeProvider: true`). The server parses the document (overlay or
  on-disk) once with `ast.parse` and emits a fold for every
  `def` / `async def` / `class` block (header line — or the first decorator
  line if any decorators are attached — kept visible, body folds), recursing
  into class bodies so methods fold independently of their enclosing class.
  Top-level `import` / `from … import` runs are coalesced into a single
  `kind: "imports"` fold spanning the first to the last line of the run;
  multi-line parenthesised imports collapse on their own. Single-line
  definitions and single-line single imports emit no fold. Files that fail to
  parse return `[]`. The consumer entrypoint
  `WorkspaceSession.folding_ranges_for_file(path)` returns a tuple of
  `FoldingRange(range, kind)` dataclasses with `kind` typed as
  `Literal["imports", "comment", "region"]`.
- Selection ranges, via `textDocument/selectionRange` (advertised as
  `selectionRangeProvider: true`). For each requested cursor position the
  server returns a chain of nested ranges (innermost-first, encoded via the
  recursive `parent` field) that powers the editor's "expand selection" /
  "shrink selection" command. The chain is computed by parsing the document
  (overlay or on-disk) once with `ast.parse` and collecting every AST node
  whose `(lineno, col_offset)`–`(end_lineno, end_col_offset)` span contains
  the cursor; duplicate-span nodes are collapsed and the result is reduced to
  a strict containment chain ordered by length so each parent is strictly
  larger than its child. Files that fail to parse, positions outside the
  source, or positions that no AST node covers fall back to a single
  zero-width range at the cursor so the LSP result length always matches
  the requested `params.positions` length. The consumer entrypoint
  `WorkspaceSession.selection_ranges_at(path, line, character)` returns a
  flat tuple of `SelectionRange(range)` dataclasses, or
  an empty tuple when no chain can be computed; the LSP layer threads the
  flat tuple into the recursive `parent` shape.
- Code lenses, via `textDocument/codeLens` (advertised as
  `codeLensProvider: {resolveProvider: false}`). One lens per top-level
  `def` / `async def` / `class` in the requested document; the lens range
  spans the bare-name identifier on the definition's header line and its
  `command` is `{title: "<N> reference[s]", command: ""}` where `N` counts
  workspace references returned by `find_references(include_declaration=
  False)`. Methods, nested classes (dotted qualified names), class
  variables, and import aliases intentionally emit no lens because this view
  is limited to the file's top-level API. Non-workspace targets, unparseable files, and
  files with no qualifying symbols return `[]`. The consumer entrypoint
  `WorkspaceSession.code_lenses_for_file(path)` returns a tuple of
  `CodeLens(range, title)` dataclasses.
- Document links, via `textDocument/documentLink` (advertised as
  `documentLinkProvider: {resolveProvider: false}`). For each
  `import` / `from … import` statement that resolves to a workspace file,
  the server emits a `DocumentLink` whose range covers the relevant alias
  span and whose target points at the resolved file. `import M [as alias]`
  emits a single link spanning the whole `M [as alias]` clause that targets
  `M`'s resolved file; `from M import a, b` emits one link per imported
  name, each targeting that name's own resolved path (so a submodule import
  like `from pkg import child` jumps to `child.py`, not `pkg/__init__.py`).
  Stdlib / installed / missing / ambiguous targets and `from M import *`
  emit no link, matching the LSP's existing scope of navigating only to
  workspace-resolved targets. Imports inside `if TYPE_CHECKING:` and
  `try: … except ImportError:` guard blocks are linked since
  `resolved_imports_for_file` walks into both. Files that fail to parse
  return `[]`. The consumer entrypoint
  `WorkspaceSession.document_links_for_file(path)` returns a tuple of
  `DocumentLink(range, target_path)` dataclasses; `target_path` is already
  remapped from the mirror root back
  to the real workspace root.
- Call hierarchy, via `textDocument/prepareCallHierarchy`,
  `callHierarchy/incomingCalls`, and `callHierarchy/outgoingCalls` (advertised
  as `callHierarchyProvider: true`). `prepareCallHierarchy` returns a single
  `CallHierarchyItem` for the identifier under the cursor when it resolves to
  a workspace `function`, `method`, or `class`, and `null` otherwise; the
  item's `range` covers the whole def block (including decorator lines if
  any), `selectionRange` is the bare-name span on the header line, and the
  item carries `data = {"path", "qualified_name"}` so subsequent
  incoming/outgoing requests do not need to re-resolve. `incomingCalls`
  groups `find_references(include_declaration=False)` results by their
  innermost enclosing workspace-known def/class (qualifier follows
  `module_symbol_table`'s ClassDef-only nesting); references inside nested
  function bodies bubble up to the next enclosing top-level function or
  class method, and module-top-level references are dropped because there
  is no caller item to attribute them to. `outgoingCalls` parses the
  declaring file, locates the `def` / `async def` / `class` matching the
  item's qualified name, and walks its body for `ast.Call` nodes without
  descending into nested `FunctionDef` / `AsyncFunctionDef` / `ClassDef` /
  `Lambda` scopes. Bare names and attribute chains resolve from the terminal
  identifier's source position through the shared lexical resolver. Proven
  workspace-module, class, `self` / `cls`, and directly annotated receivers
  can contribute callees; unproven or rebound chains do not. The
  consumer-layer entrypoints
  `WorkspaceSession.prepare_call_hierarchy(path, line, character)`,
  `WorkspaceSession.call_hierarchy_incoming_calls(path, qualified_name)`,
  and `WorkspaceSession.call_hierarchy_outgoing_calls(path, qualified_name)`
  return tuples of `CallHierarchyItem`, `CallHierarchyIncomingCall(caller,
  call_sites)`, and `CallHierarchyOutgoingCall(callee, call_sites)`
  dataclasses respectively. Items expose `range` and `selection_range`;
  `CallHierarchyCallSite` exposes `range`.
- Type hierarchy, via `textDocument/prepareTypeHierarchy`,
  `typeHierarchy/supertypes`, and `typeHierarchy/subtypes` (advertised as
  `typeHierarchyProvider: true`). `prepareTypeHierarchy` returns a single
  `TypeHierarchyItem` for the identifier under the cursor when it resolves to
  a workspace `class`, and `null` otherwise; the item's `range` covers the
  whole `class` block (including decorator lines if any), `selectionRange`
  is the bare-name span on the header line, and the item carries
  `data = {"path", "qualified_name"}` so subsequent
  supertypes/subtypes requests do not need to re-resolve. `supertypes`
  walks the matched `ClassDef`'s `bases` list once: `Subscript` bases
  (`Generic[T]`, `Base[T]`) are unwrapped to their `value` before
  resolution so generic base classes still navigate. Bare names and
  attribute chains resolve from their terminal source positions through the
  shared lexical resolver; a deep chain such as `pkg.subpkg.Foo` therefore
  works only when the lexical root and each module/class step are proven.
  Starred bases, call expressions, and unproven or rebound chains produce no
  entry. `subtypes` walks every Python file in the workspace via
  `workspace_analysis` and visits every `ClassDef` recursively (qualified
  names follow `module_symbol_table`'s `Outer.Inner` nesting convention),
  applying the same base-expression resolver to each candidate's bases;
  a candidate is a subtype iff at least one resolved base points at the
  target `(path, qualified_name)`. Only direct supertypes/subtypes are
  returned — LSP clients drill down by calling the endpoint recursively.
  The consumer-layer entrypoints
  `WorkspaceSession.prepare_type_hierarchy(path, line, character)`,
  `WorkspaceSession.type_hierarchy_supertypes(path, qualified_name)`, and
  `WorkspaceSession.type_hierarchy_subtypes(path, qualified_name)` return
  tuples of `TypeHierarchyItem` dataclasses (always
  `kind == "class"`) with `range` and `selection_range` fields.
- Rename, via `textDocument/prepareRename` and `textDocument/rename` (advertised
  as `renameProvider: {prepareProvider: true}`). `prepareRename` returns the
  identifier range and a placeholder when the cursor is on a workspace symbol;
  otherwise returns `null`. `rename` returns a `WorkspaceEdit` whose edits cover
  every reference returned by `find_references`, the `def`/`class`/`async def`
  exact declaration site returned by the shared scope graph, and every
  `from <defining_module> import <bare_old> [as <alias>]` line in the workspace
  (only the source-name portion is rewritten; any `as <alias>` clause is left
  untouched). Both absolute (`from a import foo`) and relative (`from .a import
  foo`, `from ..pkg.a import foo`) `from` lines are covered: each importer's
  relative module is resolved against its own package and matched against
  `target.defining_module`. Invalid identifiers and Python keywords yield a JSON-RPC
  `RequestFailed` error; renaming via an `import ... as` alias is refused with
  the same error code (the user is asked to rename the canonical name instead).
  Same-name and non-workspace targets return `null`.
- Inlay hints, via `textDocument/inlayHint` (advertised as
  `inlayHintProvider: {resolveProvider: false}`). The server parses the
  document (overlay or on-disk) once with `ast.parse` and walks every
  `ast.Call` whose call-function span starts inside the requested LSP
  range. For each call whose callee resolves to a workspace `function` or
  `class` (using the same position-based lexical resolver as
  `callHierarchy/outgoingCalls`), the server pairs each positional
  argument with the next positional parameter slot from the callee's
  `Signature.parameters` and emits an `InlayHint` with
  `label = "<paramname>:"`, `kind = "parameter"`, and
  `padding_right = True`. Class constructions surface
  `<Class>.__init__`'s parameters with the leading `self` / `cls`
  stripped, matching `signatureHelp`. Hints are suppressed when the
  argument is a bare `Name` whose identifier already equals the parameter
  name. Iteration stops at the first `*args` parameter (it absorbs the
  rest of the positional slots) or at the first `ast.Starred` argument in
  the call (its slot count is unknown). The consumer entrypoint
  `WorkspaceSession.inlay_hints_for_file(path, start_line=0,
  start_character=0, end_line=None, end_character=0)` returns a tuple of
  `InlayHint(position, label, kind, padding_left, padding_right)` dataclasses
  with `kind`
  typed as `Literal["parameter", "type"]`; omit `end_line` to scan the
  whole file.
- Semantic tokens, via `textDocument/semanticTokens/full` and
  `textDocument/semanticTokens/range` (advertised as
  `semanticTokensProvider: {legend: {tokenTypes, tokenModifiers}, full:
  true, range: true}`). The legend's `tokenTypes` are `["namespace",
  "class", "function", "method", "parameter", "variable"]` and
  `tokenModifiers` are `["declaration", "async"]`. The server parses
  the document (overlay or on-disk) once with `ast.parse` and emits one
  token per `def` / `async def` / `class` header — type `function` /
  `method` (when nested inside a `ClassDef` body) / `class`, modifier
  `declaration`, plus `async` for `async def` — one token per function
  parameter (posonly / positional / vararg / kwonly / kwarg slot, type
  `parameter`, modifier `declaration`), and one token per bare
  `ast.Name` load resolved through the shared lexical scope tree. Parameters,
  locals, and closure bindings retain their lexical kind when they shadow a
  module binding; module-level functions, classes, variables, and imports use
  the corresponding module-symbol classification. Attribute tokens and
  unproven cross-module targets are skipped. The walk recurses into decorator lists,
  default-value expressions, parameter / return annotations, and base /
  keyword-argument class headers, so workspace-resolved decorators,
  defaults, and base classes are all tokenised. The LSP layer encodes
  the tokens in delta form (five integers per token: `[deltaLine,
  deltaStart, length, tokenType, tokenModifiers]`), with
  `tokenModifiers` as a bitmask over the legend positions. The consumer
  entrypoint `WorkspaceSession.semantic_tokens_for_file(path)` returns
  a tuple of `SemanticToken(range, token_type, token_modifiers)` dataclasses;
  `token_type` is typed as `SemanticTokenType` and
  `token_modifiers` as `tuple[SemanticTokenModifier, ...]`. The
  range-scoped variant
  `WorkspaceSession.semantic_tokens_range_for_file(path, start_line=0,
  start_character=0, end_line=None, end_character=0)` returns the same
  tuple filtered to tokens whose start position falls in the half-open
  LSP range `[(start_line, start_character), (end_line, end_character))`;
  omit `end_line` to scan from the start position through end-of-file.
  Both the `full` and the `range` LSP handlers share the same delta
  encoder, and neither requires server-side per-document state.
- File-delete import cleanup, via `workspace/willDeleteFiles`
  (advertised as `workspace.fileOperations.willDelete` with a `**/*.py`
  filter, alongside the existing `willRename`). When the editor is about
  to delete one or more Python files, the server returns a
  `WorkspaceEdit` removing the `import` / `from` statements across the
  workspace that would become broken once those files are gone. Three
  deletion shapes are produced: (1) `import <deleted_module> [as alias]`
  → remove the whole statement (range covers the line including its
  trailing newline) when it's the only alias; otherwise remove only the
  dead alias plus its adjacent comma (`import a, b` with `a` deleted →
  `import b`); (2) `from <deleted_module> import …` → remove the whole
  statement (every imported name's source module is gone); (3)
  `from <pkg> import <leaf> [as alias]` where `<pkg>.<leaf> ==
  deleted_module` → remove the whole statement when it's the only
  imported name, else remove only the dead leaf plus its adjacent
  comma. Deletions are skipped when the path is outside the workspace,
  isn't a `.py` file, or is `__init__.py` (package deletes are a
  separate feature). Importers that are themselves part of the same
  delete batch are skipped — no point editing a file the client is
  about to remove. The consumer entrypoint
  `WorkspaceSession.import_edits_for_file_deletions(deletions)` accepts
  an iterable of paths and returns a tuple of
  `FileDeletionEdit(path, range, new_text)` dataclasses; `new_text` is always
  `""`. Sorted by `(path, range.start)`.
- File-rename import updates, via `workspace/willRenameFiles` (advertised
  as `workspace.fileOperations.willRename` with a `**/*.py` filter). When
  the editor is about to rename one or more Python files, the server
  returns a `WorkspaceEdit` updating every `import` / `from` statement
  across the workspace that references the renamed files' module names.
  Three rewrite shapes are produced: (1) `import <old_module> [as alias]`
  → rewrite the dotted-module span to `<new_module>` (the `as` clause is
  preserved); (2) `from <old_module> import …` → rewrite the dotted
  module portion, preserving the existing relative `level` when both old
  and new modules live under the same package anchor, falling back to an
  absolute `from <new_module> import …` (`level == 0`) otherwise; (3)
  `from <pkg> import <leaf>` where `<pkg>.<leaf> == old_module` →
  rewrite the leaf to the new leaf when `old_module` and `new_module`
  share the same parent package. Renames are skipped when either path is
  outside the workspace, isn't a `.py` file, is `__init__.py` (package
  renames are a separate feature), or yields an unchanged module name.
  The consumer entrypoint
  `WorkspaceSession.import_edits_for_file_renames(renames)` accepts an
  iterable of `(old_path, new_path)` pairs and returns a tuple of
  `FileRenameEdit(path, range, new_text)` dataclasses, sorted by
  `(path, range.start)`.

**Not supported:**

- `textDocument/formatting`.
- `completionItem/resolve` — a deliberate design decision, not a gap.
  `resolveProvider` is `false`: items are fully populated in the initial
  response (`label` / `kind` / `detail` / `sortText`), `detail` is a cheap
  already-decoded `Signature`, and the payload is capped at
  `_COMPLETION_LIMIT = 200`, so a lazy resolve round-trip would save nothing.
- `textDocument/completion` limitations:
  - Statement-context filtering is **deliberately deferred**. Completion
    offers the same declaration-driven candidate set regardless of position
    (e.g. it does not restrict to type names after a `:` annotation).
    Filtering by statement context would be a false-positive-prone mode that
    risks hiding valid candidates, so it is intentionally left out for now.
  - Members of stdlib / installed-package modules and classes are not
    completed; only workspace targets resolve to a member list. `os.<caret>`
    and `os.path.<caret>` yield nothing.
  - Instance-expression member completion beyond a directly annotated bare name
    is not modelled — the owner must be a workspace **module** / **class**, a
    `self`/`cls` receiver, or a bare name whose *declared* annotation names a
    workspace class. A chained owner whose type would have to be inferred
    (`obj.attr.<caret>`, `factory().<caret>`) yields nothing.
  - Only the whitelisted annotation shapes resolve for the annotated-name owner:
    a bare `Name`, a one-hop `mod.Foo`, or a whole-string forward reference.
    Subscripted, union, deep-dotted, and callable annotations (`list[Widget]`,
    `Widget | None`, `pkg.sub.Widget`, `Callable[[], Widget]`) resolve to
    nothing — the wrapped class is never half-inferred. A method's **return
    type** is likewise not propagated to its call sites.
  - The annotation is read only from the innermost enclosing function (or a
    module-level annotated variable); an outer function's parameter annotation
    does not apply inside a nested function, and a `self.x` captured in a
    **closure** or lambda over the receiver is not an instance attribute of the
    enclosing class.
  - The nearest lexical binding wins. A local annotation shadows a module-level
    import or declaration with the same name; same-scope rebindings make an
    attribute chain unresolvable rather than falling back speculatively.
  - Non-workspace base classes contribute no inherited members. A subclass of
    `collections.OrderedDict`, `enum.Enum`, an installed-package class, or a
    missing/ambiguous base sees only the members declared in the workspace part
    of its hierarchy; the base text is reported in `ClassModel.unresolved_bases`.
    An `AugAssign` (`self.x += 1`) does not *declare* an instance attribute —
    only plain / annotated `self.x = …` assignments do.
  - Dotted attribute owners are supported only for module and module-class
    chains: `pkg.sub.<caret>` (owner is a workspace module) and
    `pkg.sub.C.<caret>` / `M.C.<caret>` (owner is `<workspace-module>.<class>`)
    complete via exact `workspace_symbol_index` matches only when the lexical
    root proves the matching import. Merely having a same-named module in the
    workspace is insufficient. The dotted class case lists the class's **own**
    members only — inheritance flattening applies to the bare `Foo.` / `self.` /
    `cls.` / annotated-name paths, not this one. Anything requiring type
    inference on an intermediate component stays out.
  - Repair is caret-line-local: if a syntax error lies on a line other than
    the caret's, local and attribute completion return nothing for that file
    (import-context and workspace-module candidates still work, since they do
    not depend on the current file parsing).
  - Auto-import and snippet completions are out of scope.
- `textDocument/signatureHelp` limitations:
  - Bare-name and attribute calls are detected, including a proven deep
    module chain such as `pkg.sub.foo(`. Unproven or rebound receiver chains,
    runtime-inferred instance types, and subscripted calls (`factory[T](`)
    return no result. `self` / `cls` and directly annotated receivers work
    only when the shared resolver can prove the class at the call site.
  - Same-file calls whose enclosing `(` is still unclosed leave the file
    unparseable, so symbol extraction returns no signature. Cross-file
    calls keep working in this case because the *defining* file is
    independent of the consumer's parse status.
- Hover or goto-def on stdlib or installed-package symbols — resolution
  correctly classifies them as `stdlib` / `installed`, but the LSP does not
  synthesize a `Location` for out-of-workspace targets.
- Imports inside other conditional blocks (`if sys.version_info >= ...`, etc.) — the
  symbol walker treats these as a "conditional top-level binding" impurity and does
  not walk into them.
- Multi-hop `from X import *` chains where an intermediate uses only bare
  `from Y import *` without `__all__` or explicit re-exports. The intermediate's
  wildcard export surface is empty by design, so resolution returns `missing`.
  (See `test_symbol_at_wildcard_chain_is_bounded_by_intermediate_surface`.)
- Re-export chains deeper than `MAX_FOLLOW_DEPTH = 8`. Returns
  `resolution == "ambiguous"` and the LSP returns `[]`.
- Cyclic re-exports. Detected and returned as
  `resolution == "ambiguous"`; the LSP returns `[]`.
- `find_references` limitations:
  - A dotted module chain is followed only when a direct import proves every
    module step (for example, `import pkg.sub; pkg.sub.foo()`). Unimported
    chains and chains whose root is rebound in the active lexical scope return
    no result.
  - Forward-reference string annotations are scanned, but strings that span
    multiple lines, are triple-quoted, contain escape sequences, or use
    implicit string concatenation are skipped (offset reconstruction would
    be ambiguous in those cases).
- `linkedEditingRange` limitations:
  - In-file scope only. The mirrored ranges cover occurrences in the
    current document; cross-file references are deliberately omitted (use
    `textDocument/rename` for a workspace-wide edit). This matches
    `textDocument/documentHighlight`'s scoping, since the two share the
    same range set.
  - Inherits the `find_references` limitations above (unproven or rebound
    dotted chains and the multi-line / triple-quoted / escape-sequence /
    implicit-concatenation forward-reference-string caveats). A range that
    `find_references` cannot verify is not mirrored.
  - Only workspace symbols are mirrored. Stdlib / installed / ambiguous /
    missing targets return `null`, matching goto-definition's
    classification.
- `rename` limitations (in addition to the `find_references` limitations
  above, since rename is built on top of it):
  - Renaming via an `import ... as` alias is refused — e.g. clicking on
    `aliased` in `from a import foo as aliased` returns a `RequestFailed`
    error with the message *"Cannot rename ... via an `import ... as`
    alias; rename the original symbol instead."* The canonical-name rename
    of `foo` correctly preserves any `as <alias>` clauses across the
    workspace.
- `unused-import` / `codeAction` limitations:
  - Only workspace `from M import name` bindings are flagged. `import M`
    is never flagged (attribute usage like `M.foo()` is under-reported by
    the occurrence scan), and stdlib / installed targets are skipped
    (`find_references` cannot verify their usage). Star imports
    (`from M import *`) are never flagged.
  - `__init__.py` files are skipped entirely — they routinely aggregate and
    re-export submodule symbols that look locally unused.
  - Explicit re-exports are protected: `from y import z as z` (self-alias)
    is never flagged, a binding is left alone when *another* workspace
    module does `from <this_module> import <name>` (or `import *`) — removing
    it would break that importer — and a binding listed in this module's own
    static `__all__` is treated as an intentional public re-export and left
    alone. The cross-module guard reads the already-decoded workspace
    analysis; the `__all__` guard reads a literal `__all__ = [...]` / `(...)`
    / `{...}` of string constants (a dynamically built `__all__` cannot be
    inspected statically and offers no protection).
  - Usage detection inherits every `find_references` limitation above. In
    particular a binding used **only** inside a multi-line / triple-quoted /
    escaped / implicitly-concatenated forward-reference string annotation is
    not seen as used and may be flagged.
  - `codeAction` offers **quick fixes only** (no `refactor.*` / `source.*`
    kinds). The retarget action for `unresolved-symbol` is offered only for a
    single-name `from` statement and only when exactly one workspace module
    exposes a top-level `function` / `class` / `variable` of that name — an
    ambiguous or absent target yields just the removal action, and a
    multi-name statement is never retargeted (rewriting the from-module would
    break the sibling names that still resolve). Anchoring is line-granular:
    the request's character offsets do not further trim which diagnostics on a
    line contribute actions. Deferred: add-import for undefined bare names (no
    undefined-name diagnostic exists, and scope analysis would risk false
    positives against builtins / star-imports / locals) and `pyproject.toml`
    edits for `undeclared-import`.
- Call hierarchy limitations:
  - `prepareCallHierarchy` requires the cursor occurrence to resolve to a
    workspace function, method, or class through the shared lexical resolver.
    Variables and unresolved, ambiguous, stdlib, or installed targets produce
    no item.
  - `incomingCalls` inherits the `find_references` limitations listed above
    (unproven or rebound dotted chains and the multi-line / triple-quoted /
    escape-sequence / implicit-concatenation forward-reference-string
    caveats).
  - `outgoingCalls` resolves bare names and statically proven attribute
    chains, including directly imported deep module chains. Subscripted calls
    (`factory[T](...)`), lambda calls, unproven or rebound chains, and
    runtime-inferred instance attributes produce no callee.
  - Both directions only report workspace targets. Stdlib / installed /
    ambiguous / missing callees are omitted.
- Type hierarchy limitations:
  - `prepareTypeHierarchy` requires the cursor occurrence to resolve to a
    workspace class through the shared lexical resolver. Other symbol kinds
    and unresolved, ambiguous, stdlib, or installed targets produce no item.
  - Only workspace `class` targets contribute items. Stdlib /
    installed / ambiguous / missing classes produce no item; this
    means inheritance from `collections.OrderedDict`, `enum.Enum`,
    or third-party base classes is silently dropped from the
    supertypes view, and subclasses of such bases will not appear in
    the workspace-class subtypes view of the base.
  - Base-expression resolution accepts bare names and attribute chains, with
    `Subscript` unwrapped to its value. Every receiver step must be statically
    proven; unproven or rebound chains, `Starred` bases (`*bases`), and call
    expressions in the bases list produce no entry.
  - Only direct supertypes / subtypes are returned per call. LSP
    clients are expected to drill down by recursively calling the
    endpoint on each result.
  - Metaclass relationships (`class C(metaclass=Meta)`) are not
    reported. Metaclasses live in the `keywords` list, not `bases`,
    and the type-hierarchy view is class-inheritance only.
  - The subtypes walk iterates every `ClassDef` in every Python file
    via `workspace_analysis` and re-parses each candidate file's
    source on demand. The kernel memoises `workspace_analysis` and
    `module_symbol_table` across requests, so steady-state cost is
    bounded by the number of newly-changed files; cold runs on very
    large workspaces will scale linearly with file count.
- `textDocument/typeDefinition` limitations:
  - Annotated parameters and other lexical bindings resolve through the shared
    scope graph. Unannotated parameters have no declared type to navigate to.
  - Inferred types are out of scope. Only *declared* annotations contribute
    a type-definition location; an unannotated `x = Foo()` returns `[]`
    even when the right-hand side trivially names a workspace class.
  - Source-backed attribute annotations, including deep chains such as
    `pkg.subpkg.Foo`, resolve only when the shared lexical resolver proves the
    imported module/class chain. Unimported or rebound roots produce no
    location. Detached whole-string forward references retain the narrower
    bare-name or one-hop attribute fallback because they have no source
    position with which to prove a deeper receiver.
  - Partial string forward references (`x: "Foo" | None`,
    `x: list["Foo"]`) are not unwrapped: only the whole-annotation string
    form (`x: "Foo"`) is re-parsed. Names inside a partial-string position
    are silently dropped.
- `textDocument/semanticTokens` limitations:
  - Only the `full` and `range` request shapes are implemented.
    `semanticTokens/full/delta` is deliberately omitted — the delta form
    would require server-side state per document, and the full-document
    walk is fast enough that re-sending the whole token stream on every
    change beats the bookkeeping. The `range` handler reuses the
    full-document walk and then filters by token start position, so it
    is stateless on the server side too.
  - Use-site classification covers lexical bare-name bindings. Attribute
    access (`M.foo`, `self.method`) and cross-module re-export following are
    out of scope; the editor's default syntax highlighting still applies to
    those names.
  - `from_import_alias` and `wildcard_import_stub` entries are skipped
    at use sites (the alias's real symbol kind would need a cross-module
    resolve hop, which the first cut intentionally avoids). The
    declaration sites themselves are still tokenised when they appear as
    part of a `def` / `class` / parameter header, just not as bare-name
    uses.
- `workspace/willDeleteFiles` limitations:
  - `__init__.py` deletes (i.e. deleting a whole package directory by
    deleting its package marker) are not handled. Package deletes would
    need to remove every `import pkg.*` / `from pkg.* import …`
    statement transitively; supporting them cleanly is a separate
    feature.
  - Attribute-access usage sites are not rewritten. Deleting `helper.py`
    removes `import helper`, but any subsequent `helper.foo()` usage
    sites are left alone — the user is expected to clean them up
    separately. This mirrors the `workspace/willRenameFiles` limitation
    on the same shape.
  - Aliases inside a multi-name import whose `ast.alias` end positions
    are unavailable are skipped (the surviving statement still
    references the deleted module but at least the file is not
    mis-edited). On Python 3.11+ — the supported matrix — `ast.alias`
    nodes carry `end_lineno` / `end_col_offset`, so this is a defensive
    fallback rather than something users encounter in practice.
  - The edits assume the source compiles as Python: importers whose
    overlay / on-disk text fails `ast.parse` are skipped entirely.
- `workspace/willRenameFiles` limitations:
  - `__init__.py` renames (i.e. renaming a package directory by renaming
    its package marker) are not handled. Package renames change the
    module names of every file under the package; supporting them
    cleanly is a separate feature.
  - The rewrite of `import <old_module>` (with no `as` clause) preserves
    the leading binding (`a` in `import a.helper`) but does not update
    attribute-access usage sites (`a.helper.foo()` is not rewritten to
    `a.utils.foo()`). Likewise, `from <pkg> import <leaf>` rewrites
    when the parent stays the same but is intentionally skipped on
    cross-directory moves — both shapes would need usage-site rewrites
    that are outside the scope of a file-rename event. The user is
    expected to follow up with a symbol rename or a manual fix as
    needed.
  - The renamed file's own internal imports are not rewritten: a file
    moved to a new directory may need its relative imports updated by
    hand. Only *consumers* of the file's module are updated.
  - Multiple renames in one request are processed against the *current*
    workspace state — no chaining is attempted. A swap (A↔B) produces
    independent edits for each direction.
- `textDocument/inlayHint` limitations:
  - Only the `parameter` kind is emitted. Variable-type hints (`x = foo()`
    → `x: int = foo()`) and return-type hints are not synthesised in this
    release, even though the `InlayHintKind` literal reserves a `"type"`
    value for future use.
  - Resolves the same call shapes as `callHierarchy/outgoingCalls`: bare names
    and statically proven attribute chains. Subscripted calls
    (`factory[T](...)`), lambda calls, unproven or rebound chains, and
    runtime-inferred instance attributes produce no hints.
  - Stdlib / installed / ambiguous / missing callees are omitted, since
    the LSP does not navigate into out-of-workspace targets and a hint
    label needs an authoritative parameter name.
  - Keyword arguments and arguments past a `*spread` are not hinted
    (`f(a, *rest, c=3)` → only the first positional gets a hint).
  - The argument-vs-parameter pairing assumes the encoded
    `Signature.parameters` order matches Python's call semantics — that
    is, posonly → positional → vararg → kwonly → kwargs (see
    `_parameter_payloads_from_args` in `symbol_resolution`). Iteration
    stops at the first `*name` parameter entry, so positional arguments
    consumed by `*args` and kwonly parameters that follow it are not
    hinted.

## Troubleshooting

### "My edit isn't reflected."

The LSP applies full-text changes (`textDocumentSync.change == 1`). If your
client sends incremental `contentChanges` entries, the server only honors the
final entry's `text` field. If that field is missing, the overlay is not
updated — confirm your client is sending full-text sync, not incremental.

### "Goto-def returns empty."

Run `pyinc-tools analyze <root> --path <file>` and look at the `symbols` entry
for the name in question. The LSP identifier lookup is case-sensitive and
prefers an exact `qualified_name` match over a bare-name match. If the symbol
isn't there at all, it may fall under a known unsupported case (see the list
above) — most commonly a conditional-block import or a multi-hop wildcard.

Resolve the cursor with `session.symbol_at(path, SourcePosition(line,
character))`. A returned `SymbolId` contains the canonical workspace path and
declaration range and can be passed to `session.find_references(symbol_id)` or
`session.rename_symbol(symbol_id, new_name)`. `None` means the position is not a
conservatively resolvable workspace binding (for example, an ambiguous wildcard
provider, a cycle, a depth-limit miss, or an out-of-workspace symbol).

### "Dependency-check diagnostics don't match what I expect."

`pyinc-tools analyze` prints `dependency_check.statuses` and
`dependency_check.undeclared_imports` alongside the diagnostics. These are the
inputs the LSP uses to decide what to publish. If they look wrong, the issue
is in integration-layer behavior and not in the LSP wiring; inspect with
`pyinc-tools analyze` in isolation before filing against the LSP.

### Dependency-graph dumps for deeper debugging

`WorkspaceSession.db` exposes the full kernel introspection surface:

```python
from pyinc_tools.session import WorkspaceSession

with WorkspaceSession("/path/to/workspace") as session:
    session.analyze_workspace()
    print(session.db.statistics())
    print(session.db.query_profile())
    # Full machine-readable graph:
    graph = session.db.dependency_graph()
```

`Database.inspect(query, *args)` and `Database.explain(query, *args)` give
per-node provenance trees — see
[docs/kernel-contract.md](kernel-contract.md#additional-kernel-properties) for
their semantics.

## Architectural invariant

LSP wiring and push-based filesystem watchers stay in `pyinc_tools` and out of
`src/pyinc`. New editor-facing or watcher-facing features land in this layer on
top of stable `pyinc.integrations` entrypoints, not by widening the kernel
contract. See [docs/architecture.md](architecture.md) for the full v1 scope
boundary.
