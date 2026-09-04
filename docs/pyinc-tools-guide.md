# `pyinc-tools` Guide

`pyinc-tools` is the command-line, watcher, and editor-facing package included
in the `pyinc` distribution. It consumes the stable `pyinc.integrations` API;
the kernel itself does not contain LSP or filesystem-watcher behavior.

**Stability.** `pyinc_tools` is **unstable** and outside the semantic-versioning
promise; [SECURITY.md](../SECURITY.md) states what that promise covers.

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
module form is available everywhere the package is importable:

```console
python -m pyinc_tools --help
python -m pyinc_tools --version
```

Exit status `0` means success, `1` means an analysis/workspace failure, `2`
means invalid command-line usage, and `3` means the `--fail-on` diagnostic gate
tripped. `1` and `3` are deliberately distinct: `1` says the analyzer could not
run, `3` says it ran and found something.

## Analyze a workspace

```console
pyinc-tools analyze /path/to/workspace
pyinc-tools analyze /path/to/workspace --path src/app.py
```

The first command prints one JSON `WorkspaceAnalysisResult`; the second prints
one `FileAnalysisResult`. Output includes Python module/import analysis, the
workspace symbol index, dependency status, and deduplicated diagnostics. Use
`--indent 0` for minimal indentation or another non-negative integer for more
readable JSON.

`--path` must resolve inside the workspace. Invalid roots, escaping paths, and
unsafe filesystem links fail without analyzing an outside target.

### Report diagnostics and gate a CI job

The full JSON result embeds the workspace symbol index, which is large. For
reporting, ask for just the diagnostics — as one line each, or as a JSON array:

```console
pyinc-tools analyze /path/to/workspace --format text
pyinc-tools analyze /path/to/workspace --diagnostics-only
```

Text lines are `path:line:col: severity code message`. Line and column are
1-based for display, converted from the zero-based source geometry. A
diagnostic with no range — a file that cannot be decoded, for example — keeps
its `path:` prefix and omits the position instead of pointing at an unrelated
line. Diagnostics are sorted by location, with rangeless ones first per file,
so output is stable across runs. A workspace with no diagnostics prints
nothing.

`--fail-on` turns the run into a gate. It exits `3` when any diagnostic is at or
above the given severity, and the threshold is inclusive, so `--fail-on warning`
also fails on errors:

```console
pyinc-tools analyze /path/to/workspace --format text --fail-on error
```

The report is always printed before the exit status is decided, so a failing
gate still tells you what failed. The default is `--fail-on none`, which never
gates — upgrading `pyinc-tools` cannot turn a green pipeline red until you opt
in. `--fail-on` cannot be combined with `--watch`, which never terminates
normally; that combination is rejected as a usage error.

### Watch mode

```console
pyinc-tools analyze /path/to/workspace --watch
pyinc-tools analyze /path/to/workspace --watch --debounce-ms 300 --poll-interval-ms 150
```

Watch mode emits the initial analysis, then a JSON object containing
`changed_paths` and a new `analysis` after changed files have settled for the
debounce window. The watcher polls in a daemon thread and exits cleanly on
Ctrl-C. Polling is stdlib-only and portable; platform-specific push watcher
backends are not included.

With `--format text`, each batch is introduced by a `# changed: <paths>` header
followed by that run's diagnostic lines, so the headers can be filtered out with
`grep -v '^#'`. With `--diagnostics-only`, the `analysis` key holds the
diagnostics array rather than the full result, keeping the event wrapper shape
unchanged.

For an embedded watcher, use the public classes directly:

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
Calling `refresh_paths(...)` from an existing platform watcher is also
supported; do not drive one watcher concurrently from both polling paths.

## Start the LSP server

```console
pyinc-tools lsp
pyinc-tools lsp --root /fallback/workspace
```

The server speaks JSON-RPC over stdio. The client-provided `rootUri` wins,
followed by its first workspace folder and legacy root path. `--root` is only a
fallback; the current directory is used when neither side supplies a root.

The server negotiates UTF-8, UTF-16, or UTF-32 positions, uses full-text
document synchronization, and publishes diagnostics after editor changes or
external filesystem refreshes. See the [LSP reference](lsp-reference.md) for
the complete method matrix and user-visible limitations.

### Initialization options

Pass these keys under the LSP `initializationOptions` object:

| Key | Type | Default | Effect |
|---|---|---|---|
| `pyinc.watcher.enabled` | boolean | `true` | Starts the built-in polling watcher so external edits refresh diagnostics. |
| `pyinc.watcher.debounceMs` | integer | `200` | Waits this many milliseconds for a change to settle. |
| `pyinc.watcher.intervalMs` | number | derived from the debounce | Sets the polling interval in milliseconds. |
| `pyinc.workspace.exclude` | string array | `[]` | Omits matching workspace-relative glob patterns from the mirror and watcher. |

If the editor reliably sends `workspace/didChangeWatchedFiles`, disabling the
built-in watcher avoids duplicate scanning. Identical diagnostic publications
are deduplicated either way.

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

No first-party VS Code extension ships with this release. `pyinc-tools` can run
beside a type checker: it focuses on incremental workspace symbols, navigation,
and dependency diagnostics rather than full static typing.

## Workspace mirror and overlays

`WorkspaceSession` analyzes a temporary mirror rather than writing editor text
to the source tree.

1. Construction copies supported Python, configuration, notebook, and root
   requirements files under the workspace into a temporary directory.
2. `set_overlay(path, text)`—used by LSP open/change notifications—replaces the
   mirror copy only.
3. `refresh_paths(paths)`—used by polling and watched-file notifications—syncs
   saved disk changes into files without an active overlay.
4. Results are mapped back to real workspace paths before they reach callers,
   including paths embedded in diagnostic message text.
5. `close()` stops mutation and removes the temporary mirror.

The root requirements file's in-workspace include closure is mirrored even
when included files use nonstandard names. Default ignored directory names and
configured exclusion globs are not traversed. File links are rejected;
directory links and Windows junctions are not followed. Workspace roots should
not be concurrently renamed while a session is synchronizing them.

All public source ranges are zero-based and end-exclusive. Library positions
count Unicode code points; the LSP boundary converts them to the encoding
negotiated with the editor.

## Public surface

`pyinc_tools` exports exactly the names below. The groups are editorial: they
say what a name is for, and a later release may regroup them without changing
anything a caller can observe. What the table states is the union — these rows
together are exactly what the package exports. The kind aliases are
`Literal[...]` string aliases rather than enumerations, so each one's values are
plain strings.

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

Use the position-resolved `SymbolId` returned by `symbol_at` for references,
rename, and hierarchy operations. Passing a bare name would lose the lexical
scope and shadowing information those operations need.

`WorkspaceSession` holds one kernel request span per public method: the
several kernel gets a method such as `analyze_workspace` fans out to share
one request, so every resource the analysis walks is validated at most once
per call. Session methods that rewrite the mirror mid-call already declare it
via `request_inputs_changed()`, which rolls the held span onto a fresh
request so later reads in the same call see the edit.

## Troubleshooting

### The command is not found

Run the module form from the same interpreter used to install the package:

```console
python -m pyinc_tools --version
python -m pip show pyinc
```

If that works, the interpreter's scripts directory is missing from `PATH`.

### An editor change is not reflected

The server requests full-text synchronization. Confirm the client sends a
`text` field containing the complete document in `didChange`. Saving refreshes
from disk; closing discards the overlay.

For external edits, keep the built-in watcher enabled or configure the client
to send `workspace/didChangeWatchedFiles`. Exclusion globs apply to both the
mirror and watcher.

### Navigation or completion returns nothing

Run the analyzer on the same file and inspect its `symbols`,
`resolved_imports`, and diagnostics:

```console
pyinc-tools analyze /path/to/workspace --path src/app.py
```

Resolution intentionally returns no target when a binding is ambiguous,
dynamic, shadowed, outside the workspace, or requires runtime type inference.
The [LSP reference](lsp-reference.md#analysis-boundary) lists the common limits.

### Dependency diagnostics look wrong

Inspect `dependency_check.statuses` and
`dependency_check.undeclared_imports` in analyzer output. They are the same
integration results the LSP publishes. Verify the selected root contains the
expected `pyproject.toml` or `requirements.txt` and that excluded paths are not
hiding source files.

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

These calls report work and timing already recorded by the shared kernel. They
do not change editor files or the source workspace.
