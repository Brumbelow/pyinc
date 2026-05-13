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
# usage: pyinc-tools [-h] {analyze,lsp} ...
```

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
        { "qualified_name": "greet", "kind": "from_import_alias", "lineno": 1,
          "import_source_module": "helper", "import_source_name": "greet" },
        { "qualified_name": "main", "kind": "function", "lineno": 4,
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

### Advertised capabilities

```json
{
  "capabilities": {
    "textDocumentSync": { "openClose": true, "change": 1,
                          "save": { "includeText": true } },
    "documentSymbolProvider": true,
    "workspaceSymbolProvider": true,
    "hoverProvider": true,
    "definitionProvider": true,
    "typeDefinitionProvider": true,
    "referencesProvider": true,
    "documentHighlightProvider": true,
    "signatureHelpProvider": {
      "triggerCharacters": ["(", ","],
      "retriggerCharacters": [","]
    },
    "foldingRangeProvider": true,
    "selectionRangeProvider": true,
    "documentLinkProvider": { "resolveProvider": false },
    "codeLensProvider": { "resolveProvider": false }
  },
  "serverInfo": { "name": "pyinc-tools", "version": "2.0.0" }
}
```

### Initialization options

The server honors the following keys under `params.initializationOptions`:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `pyinc.watcher.enabled` | bool | `true` | Start a threaded polling watcher on `initialize` and publish diagnostics when it detects filesystem changes outside the editor (e.g. `git pull`, formatter scripts). |
| `pyinc.watcher.debounceMs` | int | `200` | How long a change must quiesce before the watcher acts on it. |
| `pyinc.watcher.intervalMs` | int | `max(debounceMs / 2, 50)` | How often the watcher re-scans the workspace. |

If your editor already emits `workspace/didChangeWatchedFiles` reliably, set
`pyinc.watcher.enabled: false` to avoid the redundant thread. The server
deduplicates identical `publishDiagnostics` payloads per URI, so enabling both
channels does not produce duplicate messages.

### Supported LSP methods

| Method | Behavior |
|---|---|
| `initialize` / `shutdown` / `exit` | Standard lifecycle. |
| `textDocument/didOpen` / `didChange` / `didSave` / `didClose` | Edits land in the session overlay (full-text `change: 1`). |
| `workspace/didChangeWatchedFiles` | Triggers `refresh_paths` for the listed URIs. |
| `textDocument/documentSymbol` | Per-file symbols from `module_symbol_table`. |
| `workspace/symbol` | Case-insensitive substring filter over `workspace_symbol_index`. |
| `textDocument/hover` | Markdown `def foo(x: int) -> int` / `class Foo` / `x: int`, plus a `*re-exported from*` line for import aliases. |
| `textDocument/definition` | Single `Location` via `resolve_symbol`; follows cross-module re-exports bounded by `MAX_FOLLOW_DEPTH = 8`. |
| `textDocument/typeDefinition` | `Location[]` pointing at the declared type of the symbol under the cursor. The cursor's identifier is resolved through the file's imports to its declaring `Symbol`; the symbol's annotation (variable / class-variable `annotation`, or function / method `signature.return_annotation`) is parsed as a Python expression and walked for `Name` and `Attribute(value=Name(...))` nodes, with each name re-resolved against the declaring module. Generics (`list[Foo]`), unions (`Foo \| Bar`), and qualified attribute types (`pkg.Foo`) all contribute one location per workspace-resolved type, deduplicated by `(path, lineno)`. Whole-string forward references (`x: "Foo"`) are unwrapped exactly once. Classes return their own definition location. Stdlib / installed / ambiguous type names (`int`, `list`, `typing.Optional`, etc.), import aliases, wildcard-import stubs, unannotated variables / functions, and non-workspace targets return `[]`. Attribute chains whose LHS is not a bare `Name` (`pkg.subpkg.Foo`) are skipped, matching `find_references`'s LHS-bare-Name limitation. |
| `textDocument/references` | `Location[]` via `find_references`; honors `context.includeDeclaration`; per-occurrence `col_offset` / `end_col_offset` ranges so editors can highlight each match. Only workspace-resolved targets are indexed — stdlib / installed / ambiguous targets return `[]`. |
| `textDocument/documentHighlight` | `DocumentHighlight[]` for the symbol under the cursor, scoped to the current file. The declaration site is reported with `kind: 3` (Write); other occurrences with `kind: 1` (Text). The synthetic `find_references` placeholder for `def`/`class` declarations is repaired to the real identifier offset, so editors highlight the actual name and not the line's first character. Cross-file references returned by `find_references` are filtered out — workspace-wide highlighting is `textDocument/references`'s job. Stdlib / installed / ambiguous targets return `[]`. |
| `textDocument/foldingRange` | `FoldingRange[]` for the requested document. AST-walked: `def`/`async def`/`class` blocks emit a generic-region fold (no `kind` field) starting at the header line — or the first decorator line if any decorators are attached — and ending at the AST `end_lineno`; class bodies recurse so methods fold independently. Consecutive top-level `import` / `from … import` statements are coalesced into one `kind: "imports"` fold; multi-line parenthesised imports collapse on their own. Single-line definitions and single-line single imports emit no fold. Files that fail to parse return `[]`. |
| `textDocument/selectionRange` | `SelectionRange[]` (one entry per requested position). Each entry is a chain of nested ranges encoded via the recursive `parent` field: innermost first, each parent strictly contains its child. The chain is computed by parsing the document (overlay or on-disk) once with `ast.parse` and collecting every AST node whose `(lineno, col_offset)`–`(end_lineno, end_col_offset)` span contains the cursor; duplicates are collapsed and the result is filtered to a strict containment chain ordered by length. Files that fail to parse, positions outside the source, or positions that no AST node covers fall back to a single zero-width range at the cursor so the LSP result length always matches `params.positions` length. |
| `textDocument/documentLink` | `DocumentLink[]` for the requested document. The server walks the document's AST and emits one link per `ast.alias` whose enclosing `Import` / `ImportFrom` resolves to a workspace file. For `import M [as alias]` the link spans the whole `M [as alias]` clause and points at `M`'s resolved file; for `from M import a, b` each imported name is linked individually to its own resolved path (a submodule import like `from pkg import child` resolves to `child.py`, not `pkg/__init__.py`). Stdlib / installed / missing / ambiguous targets and `from M import *` emit no link. Files that fail to parse return `[]`. |
| `textDocument/codeLens` | `CodeLens[]` for the requested document. One lens is emitted above every top-level `def` / `async def` / `class` in the file; the range spans the bare-name identifier on the definition's header line (decorated definitions still report on the `def` line, not the decorator line). The lens's `command` is `{title: "<N> reference[s]", command: ""}`, where `N` is the count returned by `find_references` with `include_declaration=False` restricted to workspace targets. Methods (`kind: "method"`), nested classes (dotted qualified names), class variables, and import aliases emit no lens — `find_references` does not reliably resolve attribute calls on instances, so a method lens would always read 0. Non-workspace targets, unparseable files, and files with no qualifying symbols return `[]`. The empty `command` string follows pylsp's convention so the lens displays as plain hint text without binding to an editor-specific action. |
| `textDocument/signatureHelp` | `SignatureHelp` for the call expression enclosing the cursor. A forward source scanner finds the topmost open `(` whose preceding token is a usable identifier, counts top-level commas to derive `activeParameter`, and resolves the identifier through `symbol_resolution.resolve_symbol`. Functions surface their declared signature; classes surface `<Class>.__init__` with a leading `self` / `cls` stripped, or an empty constructor signature when no `__init__` is defined. Stdlib / installed / ambiguous targets, attribute calls (`obj.method(`), subscripted calls (`factory[T](`), and `def`/`class` definition headers all return `null`. Parameters use LSP `[start, end]` substring offsets into the signature label. |
| `textDocument/publishDiagnostics` | Server-pushed after every state change or watcher tick; scoped to paths currently or previously reported. Duplicate payloads for an unchanged URI are suppressed. |

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

`WorkspaceSession` copies the user's workspace into a temporary mirror root
once on construction. Editor buffer edits arrive via `set_overlay(path, text)`
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
  else the on-disk contents.
- Symbol resolution ran via `resolve_symbol_reference(path, qualified_name)`
  resolves through the mirror and remaps `defining_path` back to the real root
  before returning.

## Supported vs. not yet supported

**Supported (current as of v2.0.0):**

- Document symbols (all eight kinds: `function`, `method`, `class`,
  `class_variable`, `variable`, `import_alias`, `from_import_alias`,
  `wildcard_import_stub`).
- Workspace symbols with case-insensitive substring filter.
- Diagnostics: syntax errors, unresolved imports, undeclared dependencies from
  `dependency_check`.
- Hover on local symbols.
- Goto-definition, following `import` / `from X import Y` / single-level
  `from X import *` chains through `symbol_resolution.resolve_symbol`.
- Type definition, via `textDocument/typeDefinition` (advertised as
  `typeDefinitionProvider: true`). For the symbol under the cursor, the
  server resolves the identifier to its declaring `Symbol`, reads the
  declared annotation (variable / class-variable annotation, or function /
  method return annotation), parses it as a Python expression, and walks
  for `Name` / `Attribute(value=Name(...), attr=...)` nodes. Each name is
  resolved against the declaring module — bare `Name` references through
  that module's imports, and `lhs.attr` references by first resolving
  `lhs` to a workspace module and then resolving `attr` inside that
  module. Generics (`list[Foo]`), unions (`Foo | Bar`), and qualified
  attribute types (`pkg.Foo`) all yield one location per workspace-resolved
  type, deduplicated by `(path, lineno)`. Whole-string forward references
  (`x: "Foo"`, `def f() -> "Foo"`) are unwrapped exactly once; partial
  string annotations (`x: "Foo" | None`) skip the string portion. Classes
  return their own definition location. Stdlib / installed / ambiguous
  type names are skipped via the resolver's classification. The consumer
  entrypoint `WorkspaceSession.type_definitions_at(path, qualified_name)`
  returns a tuple of `TypeDefinitionLocation(path, lineno, col_offset,
  end_col_offset)` dataclasses.
- Find references, via `symbol_resolution.find_references`. Bare-name and
  rightmost-attribute `Name` / `Attribute` occurrences are verified through the
  same resolver as goto-definition. Per-occurrence character ranges are
  returned, so editors can highlight each match precisely. Honors
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
  `DocumentHighlightKind.Text` (1). The synthetic placeholder that
  `find_references` emits for `def` / `class` declarations is repaired to the
  real identifier offset so the editor highlights the actual name rather than
  the first character of the line. Cross-file references that
  `find_references` would return are filtered out — full workspace-wide
  results are still available via `textDocument/references`. The consumer
  entrypoint `WorkspaceSession.find_document_highlights(path, qualified_name)`
  returns a tuple of `DocumentHighlight(lineno, col_offset, end_col_offset,
  kind)` dataclasses with `kind` typed as `Literal["text", "read", "write"]`.
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
  comma count yields `activeParameter`. The identifier is resolved through
  `symbol_resolution.resolve_symbol`, so cross-module re-exports hop through
  transparently. Functions surface their declared signature; classes surface
  `<Class>.__init__`'s signature with a leading `self` / `cls` stripped, or
  an empty constructor signature when no `__init__` is defined. Parameters
  are reported with LSP `[start, end]` substring offsets into the signature
  label so editors can highlight the active parameter precisely. The
  consumer entrypoint `WorkspaceSession.signature_help_at(path, line,
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
  `FoldingRange(start_line, end_line, kind)` dataclasses with `kind` typed as
  `Literal["imports", "comment", "region"]` (`start_line` / `end_line` are
  1-based AST linenos; the LSP layer subtracts 1 for the LSP 0-based shape).
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
  flat tuple of `SelectionRange(start_line, start_character, end_line,
  end_character)` dataclasses with all four fields 0-based (LSP-style), or
  an empty tuple when no chain can be computed; the LSP layer threads the
  flat tuple into the recursive `parent` shape.
- Code lenses, via `textDocument/codeLens` (advertised as
  `codeLensProvider: {resolveProvider: false}`). One lens per top-level
  `def` / `async def` / `class` in the requested document; the lens range
  spans the bare-name identifier on the definition's header line and its
  `command` is `{title: "<N> reference[s]", command: ""}` where `N` counts
  workspace references returned by `find_references(include_declaration=
  False)`. Methods, nested classes (dotted qualified names), class
  variables, and import aliases emit no lens — references on those are
  not reliably resolvable through the workspace resolver, so a method
  lens would always read 0. Non-workspace targets, unparseable files, and
  files with no qualifying symbols return `[]`. The consumer entrypoint
  `WorkspaceSession.code_lenses_for_file(path)` returns a tuple of
  `CodeLens(start_line, start_character, end_line, end_character, title)`
  dataclasses with all four position fields 0-based (LSP-style).
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
  `DocumentLink(start_line, start_character, end_line, end_character,
  target_path)` dataclasses with all four position fields 0-based
  (LSP-style); `target_path` is already remapped from the mirror root back
  to the real workspace root.
- Rename, via `textDocument/prepareRename` and `textDocument/rename` (advertised
  as `renameProvider: {prepareProvider: true}`). `prepareRename` returns the
  identifier range and a placeholder when the cursor is on a workspace symbol;
  otherwise returns `null`. `rename` returns a `WorkspaceEdit` whose edits cover
  every reference returned by `find_references`, the `def`/`class`/`async def`
  declaration site (the synthetic placeholder is repaired by locating the
  identifier offset in the source line), and every
  `from <defining_module> import <bare_old> [as <alias>]` line in the workspace
  (only the source-name portion is rewritten; any `as <alias>` clause is left
  untouched). Both absolute (`from a import foo`) and relative (`from .a import
  foo`, `from ..pkg.a import foo`) `from` lines are covered: each importer's
  relative module is resolved against its own package and matched against
  `target.defining_module`. Invalid identifiers and Python keywords yield a JSON-RPC
  `RequestFailed` error; renaming via an `import ... as` alias is refused with
  the same error code (the user is asked to rename the canonical name instead).
  Same-name and non-workspace targets return `null`.

**Not supported:**

- `textDocument/completion` (needs statement-context analysis).
- `textDocument/codeAction`, `textDocument/formatting`.
- `textDocument/signatureHelp` limitations:
  - Attribute calls (`obj.method(`) and subscripted calls
    (`factory[T](`) are not detected — the call-site scanner only looks
    for a bare identifier immediately before `(`.
  - Same-file calls whose enclosing `(` is still unclosed leave the file
    unparseable, so symbol extraction returns no signature. Cross-file
    calls keep working in this case because the *defining* file is
    independent of the consumer's parse status.
  - Default values are not part of the signature label
    (`symbol_resolution.Parameter` carries only `name` and `annotation`).
- Hover or goto-def on stdlib or installed-package symbols — resolution
  correctly classifies them as `stdlib` / `installed`, but the LSP does not
  synthesize a `Location` for out-of-workspace targets.
- Imports inside other conditional blocks (`if sys.version_info >= ...`, etc.) — the
  symbol walker treats these as a "conditional top-level binding" impurity and does
  not walk into them.
- Multi-hop `from X import *` chains where an intermediate uses only bare
  `from Y import *` without `__all__` or explicit re-exports. The intermediate's
  wildcard export surface is empty by design, so resolution returns `missing`.
  (See `test_resolve_symbol_reference_wildcard_chain_is_bounded_by_intermediate_surface`.)
- Re-export chains deeper than `MAX_FOLLOW_DEPTH = 8`. Returns
  `resolution == "ambiguous"` and the LSP returns `[]`.
- Cyclic re-exports. Detected and returned as
  `resolution == "ambiguous"`; the LSP returns `[]`.
- `find_references` limitations:
  - Attribute access whose LHS is itself an attribute chain
    (`import pkg.subpkg; pkg.subpkg.foo()`) is not counted: the occurrence
    walker only emits a verification hint when `Attribute.value` is a bare
    `Name`. Use `from pkg import subpkg; subpkg.foo()` (or
    `from pkg.subpkg import foo`) to opt in.
  - Function-local shadowing is not modeled: a local `foo = 1` inside a
    function is still reported as a reference to a module-level `foo`.
    `symbol_resolution` is module/class-scope only.
  - Forward-reference string annotations are scanned, but strings that span
    multiple lines, are triple-quoted, contain escape sequences, or use
    implicit string concatenation are skipped (offset reconstruction would
    be ambiguous in those cases).
- `rename` limitations (in addition to the `find_references` limitations
  above, since rename is built on top of it):
  - Renaming via an `import ... as` alias is refused — e.g. clicking on
    `aliased` in `from a import foo as aliased` returns a `RequestFailed`
    error with the message *"Cannot rename ... via an `import ... as`
    alias; rename the original symbol instead."* The canonical-name rename
    of `foo` correctly preserves any `as <alias>` clauses across the
    workspace.
- `textDocument/typeDefinition` limitations:
  - Function-parameter type definitions are not surfaced. The
    `symbol_resolution` integration tracks parameters only as fields on
    a function's `Signature`, not as standalone symbols, so the cursor
    must be on the symbol whose declared annotation is the type — not on
    a parameter name inside a function body.
  - Inferred types are out of scope. Only *declared* annotations contribute
    a type-definition location; an unannotated `x = Foo()` returns `[]`
    even when the right-hand side trivially names a workspace class.
  - Attribute chains whose LHS is not a bare `Name` (`pkg.subpkg.Foo`) are
    skipped, matching `find_references`'s LHS-bare-Name limitation. Use
    `from pkg import subpkg` so the annotation reads `subpkg.Foo` to opt
    in.
  - Partial string forward references (`x: "Foo" | None`,
    `x: list["Foo"]`) are not unwrapped: only the whole-annotation string
    form (`x: "Foo"`) is re-parsed. Names inside a partial-string position
    are silently dropped.

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

If the symbol is there but `defining_path` is `null`, call
`resolve_symbol` directly (or construct a `WorkspaceSession` and
`session.resolve_symbol_reference(path, name)`) and inspect the returned
`ResolvedSymbol`:

- `resolution == "ambiguous"` — too many wildcard providers, a dynamic
  `__all__`, a cycle, or depth beyond `MAX_FOLLOW_DEPTH`.
- `resolution == "missing"` — the symbol is neither a workspace binding nor
  reachable via a wildcard provider's export surface.
- `resolution in {"stdlib", "installed", "external"}` — correctly classified
  as out-of-workspace; the LSP does not navigate into these.

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
