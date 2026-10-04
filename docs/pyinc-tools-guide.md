# `pyinc-tools` Guide

`pyinc-tools` is the command-line, watcher, and editor-facing package included
in the `pyinc` distribution. It consumes the stable `pyinc.integrations` API.
LSP and filesystem-watcher behavior lives here, outside the kernel.

`pyinc_tools` is **unstable** and outside the semantic-versioning promise.
[SECURITY.md](../SECURITY.md) states what that promise covers.

## Install and verify

```console
python -m pip install pyinc
pyinc-tools --help
pyinc-tools --version
```

The help output begins with:

```text
usage: pyinc-tools [-h] [--version] {analyze,lsp} ...
```

The version command prints `pyinc-tools <installed-version>`. The equivalent
module form works everywhere the package is importable:

```console
python -m pyinc_tools --help
python -m pyinc_tools --version
```

Exit statuses:

- `0`: success.
- `1`: analysis or workspace failure. The analyzer could not run.
- `2`: invalid command-line usage.
- `3`: the `--fail-on` diagnostic gate tripped. The analyzer ran and found
  something.

## Analyze a workspace

```console
pyinc-tools analyze /path/to/workspace
pyinc-tools analyze /path/to/workspace --path src/app.py
```

The first command prints one JSON `WorkspaceAnalysisResult`. The second prints
one `FileAnalysisResult`. Output includes Python module and import analysis,
the workspace symbol index, dependency status, and deduplicated diagnostics.
`--indent` takes a non-negative integer: `0` gives minimal indentation, and
larger values give more readable JSON.

`--path` must resolve inside the workspace. Invalid roots, escaping paths, and
unsafe filesystem links fail, and analysis stays inside the workspace.

### Report diagnostics and gate a CI job

The full JSON result embeds the workspace symbol index, which is large. For
reporting, print only the diagnostics, as text lines or as a JSON array:

```console
pyinc-tools analyze /path/to/workspace --format text
pyinc-tools analyze /path/to/workspace --diagnostics-only
```

Text lines are `path:line:col: severity code message`. Line and column are
1-based for display, converted from the zero-based source geometry. A
diagnostic without a range, such as one for a file that cannot be decoded,
keeps its `path:` prefix and omits the position. Diagnostics are sorted by
location, with rangeless ones first in each file, so output is stable across
runs. A workspace with no diagnostics prints nothing.

`--fail-on` turns the run into a gate. It exits `3` when any diagnostic is at
or above the given severity. The threshold is inclusive, so
`--fail-on warning` also fails on errors:

```console
pyinc-tools analyze /path/to/workspace --format text --fail-on error
```

The report prints before the exit status is decided, so a failing gate still
shows what failed. The default is `--fail-on none`, which never gates, so an
upgrade of `pyinc-tools` keeps a green pipeline green until you opt in.
Combining `--fail-on` with `--watch` is a usage error, because watch mode runs
until it is interrupted.

### Watch mode

```console
pyinc-tools analyze /path/to/workspace --watch
pyinc-tools analyze /path/to/workspace --watch --debounce-ms 300 --poll-interval-ms 150
```

Watch mode prints the initial analysis. After changed files settle for the
debounce window, it prints a JSON object with `changed_paths` and a new
`analysis`. The watcher polls in a daemon thread and exits cleanly on Ctrl-C.
Polling is the only built-in backend. It uses only the standard library and
is portable.

With `--format text`, each batch starts with a `# changed: <paths>` header,
followed by that run's diagnostic lines. `grep -v '^#'` filters out the
headers. With `--diagnostics-only`, the `analysis` key holds the diagnostics
array, and the event object keeps its shape.

To embed a watcher, use the public classes:

```python
from pyinc_tools import PollingWorkspaceWatcher, WorkspaceSession


def changed(paths: tuple[str, ...]) -> None:
    print(paths)


with WorkspaceSession("/path/to/workspace") as session:
    with PollingWorkspaceWatcher(session, debounce_ms=200) as watcher:
        watcher.start(changed, interval_s=0.1)
        # Keep the application alive while the watcher is needed.
```

Callbacks run on the watcher thread. Keep them short or hand work to a queue.
You can also call `refresh_paths(...)` from an existing platform watcher. Use
one of these two paths at a time for a given watcher.

`start()`, `stop()`, `poll()` and the session's `close()` may be called from
different threads. Each waits for the others, so a stop or a close that lands
while the watcher starts stops the thread it starts. `poll()` refuses while
the watcher is running.

## Start the LSP server

```console
pyinc-tools lsp
pyinc-tools lsp --root /fallback/workspace
```

The server speaks JSON-RPC over stdio. It picks the workspace root in this
order: the client's `rootUri`, its first workspace folder, its legacy root
path, then `--root`, then the current directory.

The server negotiates UTF-8, UTF-16, or UTF-32 positions and uses full-text
document synchronization. It publishes diagnostics after editor changes and
external filesystem refreshes. The [LSP reference](lsp-reference.md) has the
complete method matrix and user-visible limitations.

### Initialization options

Pass these keys under the LSP `initializationOptions` object:

| Key | Type | Default | Effect |
|---|---|---|---|
| `pyinc.watcher.enabled` | boolean | `true` | Starts the built-in polling watcher so external edits refresh diagnostics. |
| `pyinc.watcher.debounceMs` | integer | `200` | Waits this many milliseconds for a change to settle. |
| `pyinc.watcher.intervalMs` | number | derived from the debounce | Sets the polling interval in milliseconds. |
| `pyinc.workspace.exclude` | string array | `[]` | Omits matching workspace-relative glob patterns from the mirror and watcher. |

If the editor reliably sends `workspace/didChangeWatchedFiles`, disable the
built-in watcher to avoid duplicate scanning. Identical diagnostic
publications are deduplicated either way.

## Editor setup

Any client that can launch a stdio language server can use `pyinc-tools lsp`.
Configure it for Python files and choose the workspace root that should define
module names.

### Neovim

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

### Emacs with Eglot

```elisp
(with-eval-after-load 'eglot
  (add-to-list 'eglot-server-programs
               '(python-mode . ("pyinc-tools" "lsp"))))
```

### VS Code

VS Code requires an extension or generic bridge that launches a stdio server.
Configure that bridge to run:

```text
pyinc-tools lsp
```

This release ships without a first-party VS Code extension. `pyinc-tools` can
run beside a type checker. It covers incremental workspace symbols,
navigation, and dependency diagnostics, and leaves full static typing to the
type checker.

## Workspace mirror and overlays

`WorkspaceSession` analyzes a temporary mirror of the workspace, so editor
text stays out of the source tree.

1. Construction copies supported Python, configuration, notebook, and root
   requirements files under the workspace into a temporary directory.
2. `set_overlay(path, text)` replaces the mirror copy only. LSP open and change
   notifications use it.
3. `refresh_paths(paths)` syncs saved disk changes into files that have no
   active overlay. Polling and watched-file notifications use it.
4. Results are mapped back to real workspace paths before they reach callers,
   including paths embedded in diagnostic message text.
5. `close()` stops mutation and removes the temporary mirror.

The mirror also holds the root requirements file's in-workspace include
closure, even when included files use nonstandard names. The walk skips
default ignored directory names and configured exclusion globs. File links
are rejected. Directory links and Windows junctions are skipped. Avoid
renaming a workspace root while a session is synchronizing it.

All public source ranges are zero-based and end-exclusive. Library positions
count Unicode code points. The LSP boundary converts them to the encoding
negotiated with the editor.

## Public surface

`pyinc_tools` exports the names in this table and no others. The groups
describe what each name is for. A later release may regroup them without any
change a caller can observe. The kind aliases are `Literal[...]` string
aliases, so their values are plain strings.

| Group | Names |
|---|---|
| Entrypoints | `WorkspaceSession`, `PollingWorkspaceWatcher` |
| Analysis results | `AnalysisDiagnostic`, `FileAnalysisResult`, `WorkspaceAnalysisResult` |
| Navigation results | `CallHierarchyCallSite`, `CallHierarchyIncomingCall`, `CallHierarchyItem`, `CallHierarchyOutgoingCall`, `DeclarationLocation`, `DocumentHighlight`, `DocumentLink`, `SelectionRange`, `TypeDefinitionLocation`, `TypeHierarchyItem` |
| Editing results | `CodeAction`, `CodeActionEdit`, `CodeLens`, `CompletionItem`, `FileDeletionEdit`, `FileRenameEdit`, `FoldingRange`, `InlayHint`, `LinkedEditingRange`, `RenameEdit`, `RenameResult`, `SemanticToken`, `SignatureHelp`, `SignatureParameterInfo` |
| Kind aliases | `CallHierarchyItemKind`, `CodeActionKind`, `CompletionItemKind`, `DocumentHighlightKind`, `FoldingRangeKind`, `InlayHintKind`, `RenameStatus`, `SemanticTokenModifier`, `SemanticTokenType`, `TypeHierarchyItemKind` |

## Common operations from Python

```python
from pyinc.integrations import SourcePosition
from pyinc_tools import WorkspaceSession

with WorkspaceSession("/path/to/workspace") as session:
    workspace = session.analyze_workspace()
    one_file = session.analyze_file("src/app.py")

    target = session.symbol_at("src/app.py", SourcePosition(4, 8))
    if target is not None:
        references = session.find_references(target)

    print(workspace.diagnostics)
    print(one_file.diagnostics)
```

Pass the position-resolved `SymbolId` from `symbol_at` to references, rename,
and hierarchy operations. It carries the lexical scope and shadowing
information those operations need, which a bare name lacks.

`WorkspaceSession` holds one kernel request span per public method. A method
such as `analyze_workspace` fans out to several kernel gets, and they all
share that one request. So each resource the analysis walks is validated at
most once per call. Session methods that rewrite the mirror mid-call declare
it with `request_inputs_changed()`. That call rolls the held span onto a
fresh request, so later reads in the same call see the edit.

## Troubleshooting

### The command is not found

Run the module form from the same interpreter used to install the package:

```console
python -m pyinc_tools --version
python -m pip show pyinc
```

If that works, the interpreter's scripts directory is missing from `PATH`.
Add it.

### An editor change is not reflected

The server requests full-text synchronization. Confirm the client sends a
`text` field containing the complete document in `didChange`. Saving
refreshes from disk. Closing discards the overlay.

For external edits, keep the built-in watcher enabled or configure the client
to send `workspace/didChangeWatchedFiles`. Exclusion globs apply to both the
mirror and watcher.

### Navigation or completion returns nothing

Run the analyzer on the same file and inspect its `symbols`,
`resolved_imports`, and diagnostics:

```console
pyinc-tools analyze /path/to/workspace --path src/app.py
```

By design, resolution returns no target when a binding is ambiguous, dynamic,
shadowed, outside the workspace, or needs runtime type inference. The
[LSP reference](lsp-reference.md#analysis-boundary) lists the common limits.

### Dependency diagnostics look wrong

Inspect `dependency_check.statuses` and
`dependency_check.undeclared_imports` in analyzer output. They are the same
integration results the LSP publishes. Check that the selected root contains
the expected `pyproject.toml` or `requirements.txt`, and that your source
files sit outside the excluded paths.

### Inspect incremental work

The session exposes its `Database` for read-only diagnostics:

```python
from pyinc_tools import WorkspaceSession

with WorkspaceSession("/path/to/workspace") as session:
    session.analyze_workspace()
    print(session.db.statistics())
    print(session.db.query_profile())
    print(session.db.dependency_graph())
```

These calls report work and timing the shared kernel has already recorded.
They leave editor files and the source workspace unchanged.
