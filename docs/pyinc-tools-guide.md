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
    "referencesProvider": true
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
| `textDocument/references` | `Location[]` via `find_references`; honors `context.includeDeclaration`; per-occurrence `col_offset` / `end_col_offset` ranges so editors can highlight each match. Only workspace-resolved targets are indexed — stdlib / installed / ambiguous targets return `[]`. |
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
- Find references, via `symbol_resolution.find_references`. Bare-name and
  rightmost-attribute `Name` / `Attribute` occurrences are verified through the
  same resolver as goto-definition. Per-occurrence character ranges are
  returned, so editors can highlight each match precisely. Honors
  `context.includeDeclaration`.
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

**Not supported:**

- `textDocument/completion` (needs statement-context analysis).
- `textDocument/rename`, `textDocument/codeAction`, `textDocument/formatting`.
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
- `find_references` limitations (v1.2.0):
  - Attribute access to a module-level symbol only imported as a module
    (`import a; a.foo()`) is not counted because the resolver is name-local.
    Use `from a import foo` to opt in.
  - Function-local shadowing is not modeled: a local `foo = 1` inside a
    function is still reported as a reference to a module-level `foo`.
    `symbol_resolution` is module/class-scope only.

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
