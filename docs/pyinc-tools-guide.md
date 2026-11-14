# `pyinc-tools` Guide

`pyinc-tools` is the command-line, watcher, and editor-facing package included
in the `pyinc` distribution. It consumes the stable `pyinc.integrations` API.
LSP and filesystem-watcher behavior lives here, outside the kernel.

`pyinc_tools` is **feature-frozen**: its behavior changes only to fix a defect.
It is outside the semantic-versioning promise.
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
- `1`: the command did not finish. The root or `--path` is missing or unsafe,
  the workspace holds too many files, analysis failed, or the reader closed
  the output pipe. A closed pipe ends the command without a message.
- `2`: invalid command-line usage.
- `3`: the `--fail-on` diagnostic gate tripped. The analyzer ran and found
  something.

`pyinc-tools lsp` exits `0` after `shutdown` and `exit` or at the end of its
input, `1` after an `exit` without `shutdown` or on input it cannot split into
messages, and `143` when SIGTERM ends it.

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
`--path` limits the output to one file. The whole workspace is still mirrored
and indexed, because import resolution and the dependency check read every
module. A `--path` that is not an existing file exits `1`. A `--path` that
names a file the mirror does not copy, such as a non-Python file other than
the root manifests or a file under a skipped directory, prints a result with a
null `module`, null `symbols`, and no diagnostics.

A workspace may hold at most 1,000 Python and manifest files, and a larger one
exits `1`. `--max-files N` raises the limit. It takes an integer of at least 1,
and the refusal message names it.

The root sets both the module names and where `pyproject.toml` and
`requirements.txt` are read. Module names are paths relative to the root, and
the two manifests are read only at the root. In a src layout, the project root
reads the manifests but names the package `src.mypkg`, so `import mypkg` does
not resolve to the workspace. The `src` directory as root names it `mypkg` but
holds neither manifest, so no dependency diagnostics appear, and files outside
`src` are not analyzed. Installed imports resolve against the interpreter that
runs `pyinc-tools`: its site-packages directories and the user site directory.

### Report diagnostics and gate a CI job

The full JSON result embeds the workspace symbol index, which is large. For
reporting, print only the diagnostics, as text lines or as a JSON array:

```console
pyinc-tools analyze /path/to/workspace --format text
pyinc-tools analyze /path/to/workspace --diagnostics-only
```

Text lines are `path:line:col: severity code message`, and the path is
absolute. Line and column are 1-based for display, converted from the
zero-based source geometry. A diagnostic without a range, such as one for a
file that cannot be decoded, keeps its `path:` prefix and omits the position.
Diagnostics are sorted by location, with rangeless ones first in each file, so
output is stable across runs. A workspace with no diagnostics prints nothing.

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

### Diagnostic codes

The analyzer reports these codes, and the LSP server publishes them with the
same `code` and `source`.

| Code | Severity | Source | Meaning |
|---|---|---|---|
| `syntax-error` | error | `pyinc.python_source` | The file does not parse, or nests too deeply to parse. |
| `source-decode-error` | error | `pyinc.python_source` | The file does not decode with its BOM or encoding declaration. |
| `missing-import` | error | `pyinc.python_source` | The import names no workspace module, standard-library module, or installed package. |
| `ambiguous-import` | warning | `pyinc.python_source` | More than one workspace file could provide the module. |
| `unresolved-symbol` | error | `pyinc.symbol_resolution` | A `from` import names something its workspace module does not define. |
| `ambiguous-symbol` | warning | `pyinc.symbol_resolution` | A `from` import names something with more than one possible definition. |
| `unused-import` | hint | `pyinc.symbol_resolution` | A workspace `from ... import ...` binding is never read in its module. |
| `undeclared-import` | warning | `pyinc.dependency_check` | The import comes from an installed distribution the manifests do not declare. |
| `dependency-missing` | warning | `pyinc.dependency_check` | A declared dependency is not installed. |
| `dependency-version_mismatch` | warning | `pyinc.dependency_check` | The installed version does not satisfy the declared constraint. |
| `dependency-ambiguous` | warning | `pyinc.dependency_check` | The declared constraint cannot be evaluated. |
| `unparseable-line` | error | `pyinc.requirements_txt` | A requirements line is not a requirement, include, or option the parser reads. |
| `requirements-decode-error` | error | `pyinc.requirements_txt` | A requirements file is not UTF-8. |
| `missing-requirements-file` | error | `pyinc.requirements_txt` | An `-r` include names a file that does not exist. |
| `cycle` | error | `pyinc.requirements_txt` | `-r` includes form a loop. |
| `error` | error | `pyinc.requirements_txt` | An `-r` include resolves outside the workspace root. |

No code reports `information`. `undeclared-import` is reported only when the
root holds a `requirements.txt` or a `pyproject.toml` with a `[project]`
table. A `[project]` table with `dynamic = ["dependencies"]` and no
`requirements.txt` lists no dependencies, so each installed import gets the
diagnostic unless an optional dependency group names it. `unused-import`
checks only `from` imports of workspace modules, and never in an `__init__.py`
file. It skips a file that does not parse, a star import, a
`from m import x as x` re-export, a name in the module's `__all__`, and a name
another workspace module imports from it.

### Watch mode

```console
pyinc-tools analyze /path/to/workspace --watch
pyinc-tools analyze /path/to/workspace --watch --debounce-ms 300 --poll-interval-ms 150
```

Watch mode prints the initial analysis. After changed files settle for the
debounce window, it prints a JSON object with `changed_paths` and a new
`analysis`. `--debounce-ms` takes an integer of at least 0 and defaults to
200. `--poll-interval-ms` takes an integer of at least 1 and defaults to half
the debounce window, but no less than 50. The watcher polls in a daemon
thread. Ctrl-C ends watch mode with status `0` once the initial analysis has
printed. Polling is the only built-in backend. It uses only the standard
library and is portable.

Each poll walks the workspace and reads a file again only when its size or
modification time changes, so an edit that keeps both is not seen. A path
whose content on disk equals what the mirror last copied from disk is not
reported, such as a file edited and then restored within the debounce window.

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

`start()` polls every 0.5 seconds when `interval_s` is omitted. Callbacks run
on the watcher thread. Keep them short or hand work to a queue. You can also
call `refresh_paths(...)` from an existing platform watcher. Use one of these
two paths at a time for a given watcher.

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

`initialize` fails with a message that says what to do when the root is the
home directory or a filesystem root, and when the workspace holds more than
1,000 Python and manifest files. Exclude directories with
`pyinc.workspace.exclude`, or raise the limit with `pyinc.workspace.maxFiles`.

The server pushes diagnostics. The [LSP reference](lsp-reference.md) has the
position and document model, the complete method matrix, when diagnostics are
published, and the user-visible limitations.

### Initialization options

Pass these keys under the LSP `initializationOptions` object:

| Key | Type | Default | Effect |
|---|---|---|---|
| `pyinc.watcher.enabled` | boolean | `true` | Starts the built-in polling watcher so external edits refresh diagnostics. |
| `pyinc.watcher.debounceMs` | integer | `200` | Waits this many milliseconds for a change to settle. |
| `pyinc.watcher.intervalMs` | number | `500` | Sets the polling interval in milliseconds. |
| `pyinc.workspace.exclude` | string array | `[]` | Omits matching workspace-relative glob patterns from the mirror and watcher. |
| `pyinc.workspace.maxFiles` | integer | `1000` | Refuses a workspace with more Python and manifest files than this. |

A `pyinc.watcher.debounceMs` or `pyinc.watcher.intervalMs` of the wrong type
fails initialize with an Invalid params error. `pyinc.watcher.enabled` is read
as a truth value: `null`, `0` and an empty string turn the watcher off, and any
other value turns it on. A `pyinc.workspace.maxFiles` that is not an integer of
at least 1 keeps the default. A `pyinc.workspace.exclude` that is not an array
is ignored, and so are its entries that are not strings.

The built-in watcher and `workspace/didChangeWatchedFiles` can run together.
The watcher skips a path whose disk content equals what the mirror last copied
from disk, so a change the editor reports first is analyzed once. A file whose
editor text is in the mirror (after `didOpen` or `didChange`, until `didSave`
or `didClose`) is analyzed twice, because the editor's report leaves that text
in place and the watcher still sees the disk change. A
`workspace/didChangeWatchedFiles` notification always runs an analysis, so a
change the watcher reports first is analyzed twice.

## Editor setup

Neovim and Emacs with Eglot are the documented clients. Configure the client
for Python files, choose the root as
[Analyze a workspace](#analyze-a-workspace) describes, and launch the
`pyinc-tools` installed in the project's environment. `pyinc-tools` can run
beside a type checker. It covers incremental workspace symbols, navigation,
and dependency diagnostics, and leaves full static typing to the type checker.

### Neovim

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = "python",
  callback = function(args)
    local root = vim.fs.root(args.buf, { "pyproject.toml", ".git" })
    if root == nil then
      return
    end
    vim.lsp.start({
      name = "pyinc-tools",
      cmd = { "pyinc-tools", "lsp" },
      root_dir = root,
    })
  end,
})
```

A buffer with no `pyproject.toml` or `.git` above it starts no server, so the
server never falls back to the editor's working directory.

### Emacs with Eglot

```elisp
(with-eval-after-load 'eglot
  (add-to-list 'eglot-server-programs
               '(python-mode . ("pyinc-tools" "lsp"))))
```

### VS Code

VS Code is untested. It needs an extension or a generic bridge that launches a
stdio server, and pyinc ships none.

## Workspace mirror and overlays

`WorkspaceSession` analyzes a mirror of the workspace, so editor text stays
out of the source tree.

1. Construction copies the workspace's Python files (`.py`), the root
   `pyproject.toml`, the root `requirements.txt`, and the files that file
   includes with `-r` or `-c`, and nothing else. It raises `ValueError` when
   the workspace holds more than `max_files` of them, 1,000 by default.
2. `set_overlay(path, text)` replaces the mirror copy only. LSP open and change
   notifications use it.
3. `refresh_paths(paths)` syncs saved disk changes into files that have no
   active overlay. Polling and watched-file notifications use it.
4. Results are mapped back to real workspace paths before they reach callers,
   including paths embedded in diagnostic message text.
5. `close()` stops mutation and removes the mirror.

The mirror is a directory on disk under the system temporary directory
(`tempfile.gettempdir()`). `close()` removes it, and `pyinc-tools lsp` closes
its session when it ends, including on SIGTERM. A process killed with SIGKILL
leaves the directory behind, and so does `pyinc-tools analyze` ended by
SIGTERM.

Requirements includes are followed in every form pip accepts (`-r file`,
`-rfile`, `--requirement file`, `--requirement=file`, and the same four for
constraints), through included files too, when they resolve inside the root.
An included file can have any name. The walk skips version control, cache,
virtual environment, and installed-package directories, and `dist`. Among them
are `node_modules`, `.eggs`, `site-packages`, `__pypackages__`, `.tox`, `.nox`,
any directory that holds a `pyvenv.cfg`, and any name ending in `.egg-info`.
It also skips paths that match the exclusion globs. A link inside the root is
skipped wherever the walk meets one: a linked file, a linked directory, a
Windows junction, a dangling link such as the lock file Emacs writes beside a
modified buffer, and a linked requirements file. A link whose target lies
outside the root refuses the workspace. Avoid renaming a workspace root while
a session is synchronizing it.

All public source ranges are zero-based and end-exclusive. Library positions
count Unicode code points. The LSP boundary converts them to the encoding
negotiated with the editor.

## Public surface

`pyinc_tools` exports the names in this table and no others. The groups
describe what each name is for. The grouping is editorial: every name imports
from `pyinc_tools` itself, whatever its group. The kind aliases are
`Literal[...]` string aliases, so their values are plain strings.

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
`dependency_check.undeclared_imports` in analyzer output. `statuses` holds
each declared dependency, and each status other than `satisfied` is published
as a `dependency-*` diagnostic. `undeclared_imports` lists each installed
distribution that an import uses and no manifest declares, once, with one
import that uses it, even when the root holds no manifest. The
`undeclared-import` diagnostic needs a manifest, as
[Diagnostic codes](#diagnostic-codes) states. A requirements line that names
no distribution, such as a bare path or URL, is not checked against the
installed packages. Check that the selected root contains the expected
`pyproject.toml` or `requirements.txt`, and that your source files sit outside
the excluded paths.

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
