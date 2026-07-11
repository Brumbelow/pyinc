# `pyinc-tools` — Consumer Tooling Guide

`pyinc-tools` is the editor- and watcher-facing consumer layer that ships with
the `pyinc` distribution. It builds only on the stable `pyinc.integrations`
public surface and does not widen the kernel semver contract. LSP wiring and
push-based filesystem watchers are architectural non-goals for the kernel itself
(see [docs/architecture.md](architecture.md)); they live here.

The distribution installs a single console script, `pyinc-tools`, with two
subcommands (`python -m pyinc_tools` is equivalent):

- `pyinc-tools analyze` — one-shot workspace or file analysis, with an optional
  threaded `--watch` mode.
- `pyinc-tools lsp` — a stdio LSP adapter; the
  [LSP feature reference](#lsp-feature-reference) below documents every
  supported method, from symbols and diagnostics through completion,
  navigation, call and type hierarchy, and file-operation edits.

Both subcommands build on the same `WorkspaceSession` — a mirrored workspace
with editor overlays, so editor edits never touch the user's source tree.

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
`WorkspaceAnalysisResult` dataclass, with six top-level keys: `root`, `python`
(the Python module analysis), `symbols` (the workspace symbol index),
`dependency_check`, `files` (per-file results), and `diagnostics` (deduped
workspace diagnostics). For a minimal workspace containing `app.py` (which
imports `greet` from `helper.py`), with the aggregate keys elided:

```json
{
  "root": "/path/to/workspace",
  "python": { ... },
  "symbols": { ... },
  "dependency_check": { ... },
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

Starts a JSON-RPC-over-stdio LSP server. The workspace root is taken from the
client's `initialize` params: `rootUri` first, then the first
`workspaceFolders` entry, then the deprecated `rootPath`. `--root` is used only
when the client supplies none of these; without `--root` the server falls back
to the current directory. The protocol surface follows LSP 3.18. The server
selects the first supported entry in the client's
`general.positionEncodings` preference list (`utf-8`, `utf-16`, or `utf-32`)
and defaults to UTF-16. Internal integration positions remain zero-based
Unicode-code-point offsets. Inbound framing accepts at most 64 KiB of headers
and a 16 MiB message body; malformed, oversized, or excessively deep JSON is
rejected at the protocol boundary.

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

## LSP feature reference

The table below indexes every supported method. The subsections that follow
are the canonical description of each feature — behavior and limitations in
one place.

| Method | Behavior |
|---|---|
| `initialize` / `shutdown` / `exit` | Standard lifecycle with strict ordering checks. |
| `textDocument/didOpen` / `didChange` / `didSave` / `didClose` | Full-text overlay sync; save/close resync disk bytes. |
| `workspace/didChangeWatchedFiles` | Triggers `refresh_paths` for the listed URIs. |
| `textDocument/documentSymbol` | Per-file symbols from `module_symbol_table`. |
| `workspace/symbol` | Case-insensitive substring filter over `workspace_symbol_index`. |
| `textDocument/hover` | Markdown signature / kind line for workspace symbols. |
| `textDocument/completion` | Declaration-driven candidates; `.` trigger character. |
| `textDocument/signatureHelp` | Signature and active parameter for the enclosing call. |
| `textDocument/definition` | Single-entry `Location[]` at the resolved definition. |
| `textDocument/declaration` | Single-entry `Location[]` at the local binding statement. |
| `textDocument/typeDefinition` | `Location[]` at the declared type(s) of the symbol. |
| `textDocument/references` | Exact ranges for every proven workspace occurrence. |
| `textDocument/documentHighlight` | File-scoped occurrences with write/text kinds. |
| `textDocument/linkedEditingRange` | In-file simultaneous-edit ranges, or `null`. |
| `textDocument/prepareRename` / `rename` | Workspace-wide `WorkspaceEdit` over proven references. |
| `textDocument/foldingRange` | Def/class body folds plus coalesced import folds. |
| `textDocument/selectionRange` | Nested AST containment chain per requested position. |
| `textDocument/documentLink` | Links from import clauses to workspace files. |
| `textDocument/codeLens` | Reference-count lens per top-level def/class. |
| `textDocument/prepareCallHierarchy`, `callHierarchy/incomingCalls` / `outgoingCalls` | Caller / callee navigation for workspace functions, methods, classes. |
| `textDocument/prepareTypeHierarchy`, `typeHierarchy/supertypes` / `subtypes` | Direct base / derived navigation for workspace classes. |
| `textDocument/inlayHint` | Parameter-name hints at proven call sites. |
| `textDocument/semanticTokens/full` / `range` | Stateless AST-derived token stream. |
| `textDocument/diagnostic` | Pull-model single-document report. |
| `workspace/diagnostic` | Pull-model workspace report. |
| `textDocument/codeAction` | Diagnostics-anchored quickfixes. |
| `workspace/willRenameFiles` | Import-statement rewrites for renamed `.py` files. |
| `workspace/willDeleteFiles` | Import-statement removals for deleted `.py` files. |

### Resolution model

Every navigation and editing feature resolves the cursor through the same
position-based lexical resolver (`WorkspaceSession.symbol_at`) — the "shared
resolver" below. Resolution is conservative: targets are classified as
workspace, stdlib, installed, ambiguous, or missing (the declared kind literals
also reserve an `external` value that no code path currently produces), and
only workspace-resolved targets produce results. "Non-workspace" below always
means stdlib / installed / ambiguous / missing. Per-document providers parse the
document (overlay or on-disk) once with `ast.parse`; each subsection states
what an unparseable file returns. Shared properties:

- `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:` import blocks are
  recognized — the symbol walker walks their bodies for `import` /
  `from X import Y` statements.
- So are `try: … except ImportError:` / `except ModuleNotFoundError:` /
  `except (ImportError, ModuleNotFoundError):` guard blocks at module top
  level. Symbols bound inside these guards appear in hover and
  goto-definition exactly as unconditional imports do, and no "conditional
  top-level binding" impurity marker is recorded for files whose only
  conditional blocks are import-error guards.
- Hover and goto-definition work for any bare identifier that matches a symbol
  name, including identifiers inside string annotations (e.g. `x: "Foo"`),
  since the identifier-at-position parser operates on raw source characters.
- The resolver's depth and cycle bounds, and the conditional-import shapes it
  refuses to walk, are listed under [Not supported](#not-supported).

### Lifecycle and document sync

`initialize` / `shutdown` / `exit` follow the standard lifecycle: requests
before initialization or after shutdown are rejected, repeated initialization
is rejected, `exit` after `shutdown` exits with status 0, and `exit` without
`shutdown` exits with status 1.

`textDocument/didOpen` / `didChange` edits land in the session overlay
(full-text `change: 1`); `didSave` / `didClose` clear the overlay and resync
authoritative disk bytes. `workspace/didChangeWatchedFiles` triggers
`refresh_paths` for the listed URIs.

### Symbols and hover

`textDocument/documentSymbol` returns per-file symbols from
`module_symbol_table`, covering all eight symbol kinds: `function`, `method`,
`class`, `class_variable`, `variable`, `import_alias`, `from_import_alias`,
and `wildcard_import_stub`. `workspace/symbol` applies a case-insensitive
substring filter over `workspace_symbol_index`.

`textDocument/hover` renders Markdown for local symbols —
`def foo(x: int) -> int` / `class Foo` / `x: int` — plus a
`*re-exported from*` line for import aliases. The stdlib / installed hover gap
is listed under [Not supported](#not-supported).

### Completion

`textDocument/completion` returns `{isIncomplete: false, items}` for the
caret. Completion is declaration-driven: candidates come from bindings proven
by the shared lexical scope graph and conservative import resolution, never
from inferred runtime types. Because a mid-edit buffer is often unparseable at
the caret (a trailing `owner.`), the server repairs the caret line to `pass`
before analysis, keeping every top-level import and definition intact while
leaving the rest of the file untouched. Recognized contexts:

- **Bare-name prefix** (`wor|`): module-level symbols from the current file's
  `ModuleSymbolTable`, workspace top-level module names, and Python keywords.
- **Attribute** `M.<prefix>`, `M` a bare name: `M` is resolved via the shared
  resolver; a workspace module offers its module-level exports, a workspace
  class its class view (methods and class variables), flattened across
  workspace base classes (see *inheritance* below).
- **`self.` / `cls.` inside a method**: the caret's enclosing method is
  located in the repaired buffer, and the owner must be that method's literal
  first parameter. The enclosing class's members come from the `class_model`
  surface: `self.` offers the instance view (methods, class variables, and
  `self.x` instance attributes); `cls.` the class view (no instance
  attributes). A `self.` in a `cls`-method, at module level, or inside a
  nested function / lambda that rebinds the first parameter offers nothing —
  the owner is not the innermost callable's first parameter.
- **Annotated bare name** (`w.<prefix>` where `w` is neither `self` / `cls`
  nor resolvable as a module/class): the name's declared annotation is
  followed to a workspace class and that class's instance view is offered.
  The declaration is resolved in priority order: the enclosing function's
  parameter annotation, then the nearest preceding `w: T` local re-annotation
  in that function, then a module-level annotated `variable`. Only a
  whitelist of annotation shapes resolves: a bare `Name` (`Widget`), a
  one-hop attribute of a bare name (`helpers.Widget`), or a whole-string
  forward reference (`"Widget"`, `"helpers.Widget"`) unwrapped exactly once.
  Subscripted, union, deep-dotted, and callable shapes (`list[Widget]`,
  `Widget | None`, `pkg.sub.Widget`, `Callable[[], Widget]`) resolve to
  nothing rather than half-inferring the wrapped class. When the same bare
  name already resolves as a module or class via the attribute path *and that
  class-object view yields any items*, it wins and the annotation is not
  consulted; when the view is empty (an empty class, or no member matches the
  prefix), the annotation is consulted instead.
- **Dotted attribute owner** `pkg.sub.<prefix>` / `pkg.sub.C.<prefix>` /
  `M.C.<prefix>`: every step must be proven a workspace module or class, via
  exact `workspace_symbol_index` matches and only when the lexical root
  proves the matching import — a same-named module merely existing in the
  workspace is insufficient. A module offers its exports; a proven class
  lists its **own** members only (inheritance flattening applies to the bare
  `Foo.` / `self.` / `cls.` / annotated-name paths, not this one).
  Unimported, rebound, ambiguous, instance, and stdlib / installed chains
  yield nothing — no runtime-type inference.
- **Import context**: `from <pkg> import <prefix>` offers the workspace
  `pkg`'s module-level names (via `workspace_symbol_index`, so it works even
  while the current file is unparseable); `import <prefix>` / `from <prefix>`
  offer workspace module names.

**Inheritance.** The `class_model`-backed views (bare `Foo.`, `self.`, `cls.`,
annotated-name owners) are flattened over workspace base classes: depth-first,
left-to-right, first definition wins, so a derived member shadows a same-named
base member (one entry, at the derived declaration site) — deliberately not C3
MRO. Only bases resolving to a workspace class contribute: a subclass of
`collections.OrderedDict`, `enum.Enum`, an installed-package class, or a
missing / ambiguous base sees only the members declared in the workspace part
of its hierarchy (`class D(OrderedDict)` gains no `dict` members), and the
unresolved base text is reported in `ClassModel.unresolved_bases`. A
subscripted base (`Base[int]`) is followed through to `Base`; a starred base
(`*mixins`) is never followed. Cycles are cut by a visited set and the walk is
bounded at `MAX_BASE_DEPTH = 8` derivations.

**Items.** Each item carries `label`, `kind` (mapped from the symbol kind:
function / method / class / field / variable / module / keyword), `detail`
(the signature label for callables, reused from signature help, or the
declared annotation for variables), and `sortText`. `resolveProvider` is
`false` (see [Not supported](#not-supported)). A caret inside a string or line
comment, or an owner that does not resolve to the workspace, returns no items.
Consumer entrypoint: `WorkspaceSession.completions_at(path, line, character)`
→ tuple of `CompletionItem(label, kind, detail, sort_text)`.

Limitations:

- Statement-context filtering is deliberately deferred: the same
  declaration-driven candidate set is offered regardless of position (no
  restriction to type names after a `:` annotation), because such filtering
  would be a false-positive-prone mode that risks hiding valid candidates.
- Members of stdlib / installed-package modules and classes are not completed
  (`os.<caret>` and `os.path.<caret>` yield nothing); only workspace targets
  resolve to a member list.
- Instance-expression owners beyond a directly annotated bare name are not
  modelled: a chained owner whose type would need inference
  (`obj.attr.<caret>`, `factory().<caret>`) yields nothing, and a method's
  return type is not propagated to its call sites.
- The annotation is read only from the innermost enclosing function (or a
  module-level annotated variable): an outer function's parameter annotation
  does not apply inside a nested function, and a `self.x` captured in a
  closure or lambda over the receiver is not an instance attribute of the
  enclosing class.
- The nearest lexical binding wins: a local annotation shadows a same-named
  module-level import or declaration, and same-scope rebindings make an
  attribute chain unresolvable rather than falling back speculatively.
- An `AugAssign` (`self.x += 1`) does not *declare* an instance attribute —
  only plain / annotated `self.x = …` assignments do.
- Repair is caret-line-local: a syntax error on any other line makes local and
  attribute completion return nothing for that file (import-context and
  workspace-module candidates still work — they do not depend on the current
  file parsing).
- Auto-import and snippet completions are out of scope.

### Signature help

`textDocument/signatureHelp` (trigger characters `(` and `,`; retrigger `,`)
returns the signature and active parameter for the call expression enclosing
the cursor. A forward source scanner skips comments and string literals
(single, double, and triple-quoted) and tracks a stack of open brackets; the
topmost open `(` whose preceding token is a usable identifier identifies the
function being called, and the count of top-level commas yields
`activeParameter`. The identifier resolves through the shared resolver, so
cross-module re-exports hop transparently; bare-name calls and proven dotted
module calls such as `M.foo(` and `pkg.sub.foo(` are supported.

Functions surface their declared signature; classes surface
`<Class>.__init__`'s signature with the leading `self` / `cls` stripped, or an
empty constructor signature when no `__init__` is defined. Parameter default
values are rendered into the label (`name: ann = default` / `name=default`),
read from the defining file's source since `Parameter` carries no default.
Parameters are reported with LSP `[start, end]` substring offsets into the
signature label so editors can highlight the active parameter precisely.
Consumer entrypoint: `WorkspaceSession.signature_help_at(path, line,
character)` → `SignatureHelp(label, parameters, active_parameter)`.

Limitations: non-workspace targets, unproven or rebound receiver chains,
runtime-inferred instance types, subscripted calls (`factory[T](`), and
`def` / `class` definition headers all return `null`. `self` / `cls` and
directly annotated receivers work only when the shared resolver can prove the
class at the call site. Same-file calls whose enclosing `(` is still unclosed
leave the file unparseable, so symbol extraction returns no signature;
cross-file calls keep working because the *defining* file is independent of
the consumer's parse status.

### Navigation: definition, declaration, type definition

**Definition.** `textDocument/definition` resolves the cursor with the shared
resolver and returns a single-entry `Location` array pointing at the resolved
definition. It follows `import` / `from X import Y` / single-level
`from X import *` chains, including conservative cross-module re-exports and
directly imported dotted module chains.

**Declaration.** `textDocument/declaration` also returns a single-entry
`Location[]`, but pointing at the *binding statement* in the current file —
distinct from definition, which follows import chains through to the imported
target's file. The cursor is resolved in its lexical scope and the returned
range is the binding's exact `SourceRange`. For workspace `function` /
`class` / `method` / `variable` / `class_variable` symbols, the declaration
coincides with the definition (the def/class/assignment line). For
`import_alias` and `from_import_alias` symbols, the declaration is the local
`import` / `from … import` binding, even when the imported target is outside
the workspace. Wildcard-import stubs have no name-specific local binding, so a
reference supplied only by `from M import *` has no declaration result.
Unknown identifiers, whitespace positions, and paths outside the workspace
return `[]`. Consumer entrypoint:
`WorkspaceSession.declaration_location_at(symbol_id)` →
`DeclarationLocation(path, range)` or `None`.

**Type definition.** `textDocument/typeDefinition` returns `Location[]`
pointing at the declared type of the symbol under the cursor. The server
resolves the symbol's exact `SymbolId`, reads its declared annotation
(including parameter / local / class-variable annotations and function or
method returns), parses it as a Python expression, and resolves each
source-backed type occurrence at its real lexical position. Bare names and
dotted attributes are accepted only when every module/class step is proven.
Generics (`list[Foo]`), unions (`Foo | Bar`), and qualified attribute types
(`pkg.sub.Foo`) each contribute one exact `SourceRange` per workspace-resolved
type, deduplicated by `(path, range)`. Whole-string forward references
(`x: "Foo"`, `def f() -> "Foo"`) are unwrapped exactly once. Classes return
their own definition location. Non-workspace type names are skipped via the
resolver's classification; unsupported or non-workspace targets return `[]`.
Consumer entrypoint: `WorkspaceSession.type_definitions_at(symbol_id)` →
tuple of `TypeDefinitionLocation(path, range)`.

Type-definition limitations:

- Only *declared* annotations contribute a location; inferred types are out of
  scope. An unannotated `x = Foo()` returns `[]` even when the right-hand side
  trivially names a workspace class, and unannotated parameters have no
  declared type to navigate to. (Annotated parameters and other lexical
  bindings resolve through the shared scope graph.)
- Deep attribute chains such as `pkg.subpkg.Foo` resolve only when the shared
  resolver proves the imported module/class chain; unimported or rebound roots
  produce no location. Detached whole-string forward references retain the
  narrower bare-name or one-hop attribute fallback because they have no source
  position with which to prove a deeper receiver.
- Partial string forward references (`x: "Foo" | None`, `x: list["Foo"]`) are
  not unwrapped: only the whole-annotation string form is re-parsed, and names
  inside a partial-string position are silently dropped.

### References and highlights

`textDocument/references` resolves the cursor with the shared resolver
followed by `find_references(SymbolId)`, honors `context.includeDeclaration`,
and emits each occurrence's exact `SourceRange` so editors can highlight every
match precisely. Only workspace-resolved targets are indexed. Bare-name and
proven attribute occurrences are verified through the same resolver as
goto-definition, and cross-module re-exports inside the imported module hop
through transparently.

**Module-attribute access.** Attribute access on an `import M` /
`import M as alias` binding (`M.foo()`, `alias.foo()`) is counted: the
occurrence walker carries the LHS `Name` as a hint, and the verifier resolves
the LHS through its `import_alias` to the target's defining module before
checking the attribute name. Only the rightmost-attribute span is reported
(the leading `M.` is left alone), so rename rewrites just the attribute
portion. A dotted module chain is followed only when a direct import proves
every module step (for example, `import pkg.sub; pkg.sub.foo()`); unimported
chains and chains whose root is rebound in the active lexical scope return no
result.

**Forward-reference strings.** Forward-reference string annotations (e.g.
`def g(a: 'Foo')`, `x: 'list[Foo]'`, `x: 'pkg.Foo'`, `'Foo | None'`) are also
scanned: the walker re-parses the string with `ast.parse(..., mode="eval")`
and emits `Name` / `Attribute` occurrences from the inner expression with
offsets translated back to the file. Strings spanning multiple lines,
triple-quoted strings, strings containing escape sequences, and
implicitly-concatenated string literals are skipped to keep offset
reconstruction unambiguous.

These two paragraphs are the canonical *find-references caveats*. Every
feature built on `find_references` — document highlights, linked editing,
rename, code lenses, incoming calls, and `unused-import` detection — inherits
them: an occurrence the verifier cannot prove is not reported.

**Document highlight.** `textDocument/documentHighlight` returns highlight
ranges for the symbol under the cursor, scoped to the current file, each with
its exact range. The declaration site is reported with `kind: 3` (Write);
other occurrences with `kind: 1` (Text). Cross-file references that
`find_references` would return are filtered out — workspace-wide highlighting
is `textDocument/references`'s job. Non-workspace targets return `[]`.
Consumer entrypoint:
`WorkspaceSession.find_document_highlights(path, symbol_id)` → tuple of
`DocumentHighlight(range, kind)`, with `kind` typed as
`Literal["text", "read", "write"]`.

### Rename and linked editing

**Linked editing.** `textDocument/linkedEditingRange` returns `{ranges}` or
`null` — the set of ranges in the current file that an editor should mirror
while the user types; every range has identical content, so editing one
updates them all live. The range set is exactly the file-scoped occurrences
that `textDocument/documentHighlight` reports for the symbol under the cursor
(the declaration plus every verified bare-name or proven-attribute reference),
so all spans cover the same bare identifier. The optional protocol
`wordPattern` is omitted so clients do not apply an ASCII-only restriction to
valid Unicode Python identifiers. Linked editing is in-file only and
intentionally lighter than rename: it does not touch other files, so
workspace-wide edits still go through `textDocument/rename`. It inherits the
find-references caveats — a range that `find_references` cannot verify is not
mirrored. Unknown identifiers, whitespace cursor positions, non-workspace
targets, and files outside the workspace return `null`. Consumer entrypoint:
`WorkspaceSession.linked_editing_ranges_at(path, symbol_id)` → tuple of
`LinkedEditingRange(range)`. The feature lives entirely on top of the stable
`pyinc.integrations` public surface (via `find_references`) — no kernel
contract change and no new integration-layer surface.

**Rename.** `textDocument/prepareRename` resolves the cursor to a workspace
`SymbolId` and returns its exact identifier range and placeholder, or `null`
when the target cannot be renamed safely. `textDocument/rename` resolves the
same cursor target and returns a `WorkspaceEdit` whose edits cover every
reference returned by `find_references`, the `def` / `class` / `async def`
exact declaration site returned by the shared scope graph, and every
`from <defining_module> import <bare_old> [as <alias>]` line in the workspace
(only the source-name portion is rewritten; any `as <alias>` clause is left
untouched). Both absolute (`from a import foo`) and relative
(`from .a import foo`, `from ..pkg.a import foo`) `from` lines are covered:
each importer's relative module is resolved against its own package and
matched against `target.defining_module`.

Invalid identifiers and Python keywords yield a JSON-RPC `RequestFailed`
error. Renaming via an `import ... as` alias is refused with the same error
code — e.g. clicking on `aliased` in `from a import foo as aliased` returns
*"Cannot rename ... via an `import ... as` alias; rename the original symbol
instead."* — while the canonical-name rename of `foo` correctly preserves any
`as <alias>` clauses across the workspace. Same-name and non-workspace targets
return `null`. Rename is built on `find_references` and inherits its caveats.

### Folding, selection, links, lenses

**Folding ranges.** `textDocument/foldingRange` emits a fold for every `def` /
`async def` / `class` block: a generic-region fold (no `kind` field) starting
at the header line — or the first decorator line if any decorators are
attached — and ending at the AST range, with the header kept visible. Class
bodies recurse, so methods fold independently of their enclosing class.
Consecutive top-level `import` / `from … import` statements are coalesced into
a single `kind: "imports"` fold spanning the first to the last line of the
run; multi-line parenthesised imports collapse on their own. Single-line
definitions and single-line single imports emit no fold. Every entry includes
`startLine`, `startCharacter`, `endLine`, and `endCharacter`, with scalar
character fields converted to the negotiated position encoding. Unparseable
files return `[]`. Consumer entrypoint:
`WorkspaceSession.folding_ranges_for_file(path)` → tuple of
`FoldingRange(range, kind)`, with `kind` typed as
`Literal["imports", "comment", "region"]`.

**Selection ranges.** `textDocument/selectionRange` returns one
`SelectionRange` per requested position: a chain of nested ranges encoded via
the recursive `parent` field (innermost first, each parent strictly containing
its child) that powers the editor's "expand selection" / "shrink selection"
command. The chain is computed by normalizing AST byte columns to code points
and collecting every AST node whose
`(lineno, col_offset)`–`(end_lineno, end_col_offset)` span contains the
cursor; duplicate-span nodes are collapsed and the result is reduced to a
strict containment chain ordered by length. Unparseable files, positions
outside the source, and positions no AST node covers fall back to a single
zero-width range at the cursor, so the LSP result length always matches the
requested `params.positions` length. Consumer entrypoint:
`WorkspaceSession.selection_ranges_at(path, line, character)` → flat tuple of
`SelectionRange(range)` (empty when no chain can be computed); the LSP layer
threads the flat tuple into the recursive `parent` shape.

**Document links.** `textDocument/documentLink` walks the document's AST and
emits one link per `ast.alias` whose enclosing `Import` / `ImportFrom`
resolves to a workspace file. For `import M [as alias]` the link spans the
whole `M [as alias]` clause and targets `M`'s resolved file; for
`from M import a, b` each imported name is linked individually to its own
resolved path (a submodule import like `from pkg import child` jumps to
`child.py`, not `pkg/__init__.py`). Non-workspace targets and
`from M import *` emit no link, matching the server's scope of navigating only
to workspace-resolved targets. Imports inside `if TYPE_CHECKING:` and
`try: … except ImportError:` guard blocks are linked, since
`resolved_imports_for_file` walks into both. Unparseable files return `[]`.
Consumer entrypoint: `WorkspaceSession.document_links_for_file(path)` → tuple
of `DocumentLink(range, target_path)`; `target_path` is already remapped from
the mirror root back to the real workspace root.

**Code lenses.** `textDocument/codeLens` emits one lens above every top-level
`def` / `async def` / `class` in the requested document. The lens range spans
the bare-name identifier on the definition's header line (decorated
definitions still report on the `def` line, not the decorator line), and its
`command` is `{title: "<N> reference[s]", command: ""}`, where `N` counts
workspace references returned by `find_references(include_declaration=False)`.
The empty `command` string follows pylsp's convention, so the lens displays as
plain hint text without binding to an editor-specific action. Methods
(`kind: "method"`), nested classes (dotted qualified names), class variables,
and import aliases intentionally emit no lens — this view is limited to the
file's top-level API. Non-workspace targets, unparseable files, and files with
no qualifying symbols return `[]`. Consumer entrypoint:
`WorkspaceSession.code_lenses_for_file(path)` → tuple of
`CodeLens(range, title)`.

### Call hierarchy

`textDocument/prepareCallHierarchy` resolves the identifier under the cursor
through the shared resolver. If the target is a workspace `function`,
`method`, or `class`, it returns a single `CallHierarchyItem` describing the
declaring def/class; variables, import aliases, `from_import` aliases,
wildcard-import stubs, and non-workspace targets return `null`. The item's
`range` covers the whole def block (including decorator lines if any), its
`selectionRange` is the bare-name span on the header line, and its `data`
field carries `{"path": str, "qualified_name": str}`, which the server reads
back on the incoming/outgoing requests so they do not need to re-resolve.

`callHierarchy/incomingCalls` calls
`find_references(include_declaration=False)` on the item's target and groups
references by their innermost enclosing workspace-known def/class in the same
file. The qualifier follows `module_symbol_table`'s ClassDef-only nesting
scheme, so a reference inside `class C: def m(self): ...` is attributed to
`C.m`. References inside nested function bodies bubble up to the next
enclosing function or method that is in the symbol table; module-top-level
references are dropped because there is no caller item to attribute them to.
`fromRanges` are AST occurrence ranges, including the rightmost-attribute span
for `M.foo()`-style references. Incoming calls inherit the find-references
caveats.

`callHierarchy/outgoingCalls` parses the item's declaring file, locates the
matching `def` / `async def` / `class`, and walks its body for `ast.Call`
nodes without descending into nested `FunctionDef` / `AsyncFunctionDef` /
`ClassDef` / `Lambda` scopes. Bare calls and attribute calls whose complete
receiver chain is statically proven resolve from the terminal identifier's
source position through the shared resolver; proven workspace-module, class,
`self` / `cls`, and directly annotated receivers can contribute callees.
Workspace `function` / `method` / `class` targets contribute callees;
subscripted calls (`factory[T](...)`), lambda calls, unproven or rebound
chains, and runtime-inferred instance attributes produce no callee.
`fromRanges` are exact callee ranges.

Both directions report only workspace targets; non-workspace callees are
omitted. Consumer entrypoints:
`WorkspaceSession.prepare_call_hierarchy(path, line, character)`,
`WorkspaceSession.call_hierarchy_incoming_calls(path, qualified_name)`, and
`WorkspaceSession.call_hierarchy_outgoing_calls(path, qualified_name)` →
tuples of `CallHierarchyItem`,
`CallHierarchyIncomingCall(caller, call_sites)`, and
`CallHierarchyOutgoingCall(callee, call_sites)` respectively. Items expose
`range` and `selection_range`; `CallHierarchyCallSite` exposes `range`.

### Type hierarchy

`textDocument/prepareTypeHierarchy` resolves the identifier under the cursor
through the shared resolver. If the target is a workspace `class`, it returns
a single `TypeHierarchyItem` describing the declaring `ClassDef`; functions,
methods, variables, import aliases, `from_import` aliases, wildcard-import
stubs, and non-workspace targets return `null`. The item's `range` covers the
whole class block (including decorator lines if any), its `selectionRange` is
the bare-name span on the header line, and its `data` field carries
`{"path": str, "qualified_name": str}`, read back on the supertypes/subtypes
requests so they do not need to re-resolve.

`typeHierarchy/supertypes` parses the item's declaring file, locates the
matching `ClassDef`, and resolves each entry in its `bases` list through the
shared resolver. `Subscript` bases (`Generic[T]`, `Base[T]`) are unwrapped to
their value once, so generic base classes still navigate. Bare names and
attribute chains resolve from their terminal source positions; a deep chain
such as `pkg.subpkg.Foo` works only when the lexical root and each
module/class step are proven. Starred bases, call expressions, and unproven or
rebound chains produce no entry. Duplicates by `(path, qualified_name)` are
collapsed.

`typeHierarchy/subtypes` walks the workspace once via `workspace_analysis`,
visiting every `ClassDef` recursively (qualified names follow
`module_symbol_table`'s `Outer.Inner` nesting convention). Each candidate's
bases are unwrapped (subscript dropped) and resolved through the candidate's
module imports using the same rules as `supertypes`; a candidate is a subtype
iff at least one resolved base points at the target `(path, qualified_name)`.
The target itself is excluded, duplicates by `(path, qualified_name)` are
collapsed, and the output is sorted by `(path, qualified_name)`.

Shared semantics and limitations:

- Only workspace `class` targets contribute items; non-workspace bases are
  dropped. Inheritance from `collections.OrderedDict`, `enum.Enum`, or
  third-party base classes is silently absent from the supertypes view, and
  subclasses of such bases do not appear in the workspace-class subtypes view
  of the base.
- Only direct supertypes / subtypes are returned per call; LSP clients drill
  down by calling the endpoint recursively on each result.
- Metaclass relationships (`class C(metaclass=Meta)`) are not reported —
  metaclasses live in the `keywords` list, not `bases`, and the type-hierarchy
  view is class-inheritance only.
- The subtypes walk iterates every `ClassDef` in every Python file via
  `workspace_analysis` and re-parses each candidate file's source on demand.
  The kernel memoises `workspace_analysis` and `module_symbol_table` across
  requests, so steady-state cost is bounded by the number of newly-changed
  files; cold runs on very large workspaces scale linearly with file count.

Consumer entrypoints:
`WorkspaceSession.prepare_type_hierarchy(path, line, character)`,
`WorkspaceSession.type_hierarchy_supertypes(path, qualified_name)`, and
`WorkspaceSession.type_hierarchy_subtypes(path, qualified_name)` → tuples of
`TypeHierarchyItem` (always `kind == "class"`) with `range` and
`selection_range` fields.

### Inlay hints

`textDocument/inlayHint` emits parameter-name hints at call sites inside the
requested range: the server walks every `ast.Call` whose call-function span
starts inside the requested LSP range. For each call whose callee resolves
through the shared resolver to a workspace `function` or `class` (the same
call shapes as `callHierarchy/outgoingCalls`), it pairs each positional
argument with the next positional parameter slot from the callee's
`Signature.parameters` and emits an `InlayHint` with label `"<paramname>:"`,
`kind: 2` (Parameter), and `paddingRight: true`. Class constructions surface
`<Class>.__init__`'s parameters with the leading `self` / `cls` stripped,
matching signature help. Hints are suppressed when the argument is a bare
`Name` whose identifier already equals the parameter name. Unparseable files
produce no hints.

Limitations:

- Only the `parameter` kind is emitted. Variable-type hints (`x = foo()` →
  `x: int = foo()`) and return-type hints are not synthesised in this release,
  even though the `InlayHintKind` literal reserves a `"type"` value for future
  use.
- Subscripted calls (`factory[T](...)`), lambda calls, unproven or rebound
  chains, and runtime-inferred instance attributes produce no hints.
  Non-workspace callees are omitted, since the LSP does not navigate into
  out-of-workspace targets and a hint label needs an authoritative parameter
  name.
- Keyword arguments and arguments past a `*spread` are not hinted
  (`f(a, *rest, c=3)` → only the first positional gets a hint). Iteration
  stops at the first `*args` parameter (it absorbs the rest of the positional
  slots) or at the first `ast.Starred` argument in the call (its slot count is
  unknown), so positional arguments consumed by `*args` and kwonly parameters
  that follow it are not hinted.
- The argument-vs-parameter pairing assumes the encoded `Signature.parameters`
  order matches Python's call semantics — posonly → positional → vararg →
  kwonly → kwargs (see `_parameter_payloads_from_args` in
  `symbol_resolution`).

Consumer entrypoint: `WorkspaceSession.inlay_hints_for_file(path,
start_line=0, start_character=0, end_line=None, end_character=0)` → tuple of
`InlayHint(position, label, kind, padding_left, padding_right)`, with `kind`
typed as `Literal["parameter", "type"]`; omit `end_line` to scan the whole
file.

### Semantic tokens

`textDocument/semanticTokens/full` returns a `SemanticTokens` payload
(`{data: int[]}`) for the requested document. The legend appears verbatim in
the advertised capabilities above: `tokenTypes` `["namespace", "class",
"function", "method", "parameter", "variable"]` and `tokenModifiers`
`["declaration", "async"]`. The server emits:

- one token per `def` / `async def` / `class` header — type `function` /
  `method` (when nested inside a `ClassDef` body) / `class`, modifier
  `declaration`, plus `async` for `async def`;
- one token per function parameter (posonly / positional / vararg / kwonly /
  kwarg slot) — type `parameter`, modifier `declaration`; and
- one token per bare `ast.Name` load resolved through the shared lexical scope
  tree.

Use-site classification combines the lexical scope tree with the module symbol
table: parameters, locals, and closure bindings retain their lexical token
kind when they shadow a module binding, while module-level functions, classes,
variables, and imports use the corresponding module-symbol classification. The
walk recurses into decorator lists, default-value expressions, parameter /
return annotations, and base / keyword-argument class headers, so
workspace-resolved decorators, defaults, and base classes are all tokenised.
Tokens are emitted in `(line, character)` order and encoded into the LSP wire
format as five integers per token
`[deltaLine, deltaStart, length, tokenType, tokenModifiers]`, where
`tokenModifiers` is a bitmask over the legend positions. Unparseable files
return `{"data": []}`.

`textDocument/semanticTokens/range` returns the slice of the document covered
by the half-open LSP range `[params.range.start, params.range.end)`. It reuses
the same full-document walk and filters by token start position: a token at
`(line, character)` is included iff its start is `>= range.start` and
`< range.end`. The retained tokens are encoded on their own — the delta cursor
is reset, so the first emitted token's `deltaLine` / `deltaStart` are
absolute. Unparseable and missing files return `{"data": []}`. Both handlers
share the same delta encoder, and neither holds server-side per-document
state — every request is independent.

Limitations:

- Only the `full` and `range` request shapes are implemented.
  `semanticTokens/full/delta` is deliberately omitted — the delta form would
  require server-side state per document, and the full-document walk is fast
  enough that re-sending the whole token stream on every change beats the
  bookkeeping.
- Use-site classification covers lexical bare-name bindings. Attribute access
  (`M.foo`, `self.method`) and cross-module re-export following are out of
  scope; the editor's default syntax highlighting still applies to those
  names.
- `from_import_alias` and `wildcard_import_stub` entries are skipped at use
  sites (the alias's real symbol kind would need a cross-module resolve hop,
  which is intentionally avoided). The declaration sites themselves are still
  tokenised when they appear as part of a `def` / `class` / parameter header,
  just not as bare-name uses.

Consumer entrypoint: `WorkspaceSession.semantic_tokens_for_file(path)` →
tuple of `SemanticToken(range, token_type, token_modifiers)`; `token_type` is
typed as `SemanticTokenType` and `token_modifiers` as
`tuple[SemanticTokenModifier, ...]`. The range-scoped variant
`WorkspaceSession.semantic_tokens_range_for_file(path, start_line=0,
start_character=0, end_line=None, end_character=0)` returns the same tuple
filtered to tokens whose start position falls in the half-open LSP range
`[(start_line, start_character), (end_line, end_character))`; omit `end_line`
to scan from the start position through end-of-file.

### Diagnostics and code actions

**Diagnostic set.** Diagnostics cover syntax errors, unresolved imports,
undeclared dependencies from `dependency_check`, and unused workspace
`from … import` bindings. The codes are `missing-import`, `ambiguous-import`,
`undeclared-import`, `unresolved-symbol`, `ambiguous-symbol`, and
`unused-import`, plus `python_source` parse errors. `unused-import` items
carry `severity: 4` (Hint) and `tags: [1]` (Unnecessary) so editors fade the
binding.

**Channels.** Diagnostics are delivered over both the push channel
(`textDocument/publishDiagnostics`, fed by document sync and the optional
watcher — see [Initialization options](#initialization-options)) and the LSP
3.18 pull channel (`textDocument/diagnostic` + `workspace/diagnostic`,
advertised via `diagnosticProvider`).

**Single-document pull.** `textDocument/diagnostic` runs `analyze_file` on the
requested document and returns a `RelatedFullDocumentDiagnosticReport`
(`{kind: "full", resultId, items}`) whose `items` are the same `Diagnostic`
objects the push channel emits for that file. A clean file returns a full
report with `items: []`; a pull for a URI outside the workspace returns an
empty full report rather than failing.

**Workspace pull.** `workspace/diagnostic` runs `analyze_workspace` once and
returns `{items: [...]}` with one report per analyzed `.py` file (plus any
config / requirements file that carries dependency or requirements-parse
diagnostics), sorted by path. Recursive requirements failures such as
`missing-requirements-file` and `cycle` are reported against the real root
`requirements.txt`. Each report is a `WorkspaceFullDocumentDiagnosticReport`
(`{kind: "full", uri, version: null, resultId, items}`); files that are now
clean still get an empty-`items` report so the client can clear stale
problems. `version` is always `null` — the session tracks overlays, not LSP
document versions.

**`resultId` mechanism.** The pull channel is stateless: each report's
`resultId` is a SHA-256 over the diagnostic signatures — including `tags`, so
a diagnostic gaining or losing a tag re-issues. When the client echoes a
matching `previousResultId` (or, for the workspace request,
`previousResultIds` as `[{uri, value}]`), the server replies
`{kind: "unchanged", resultId}` — workspace form:
`{kind: "unchanged", uri, version: null, resultId}` — instead of resending.
Because `resultId`s are pure functions of the current diagnostics, the pull
channel coexists with the push channel without extra bookkeeping.

**`unused-import` flagging rules.** Only workspace `from M import name`
bindings are flagged. `import M` is never flagged (attribute usage like
`M.foo()` is under-reported by the occurrence scan), stdlib / installed
targets are skipped (`find_references` cannot verify their usage), and star
imports (`from M import *`) are never flagged. `__init__.py` files are skipped
entirely — they routinely aggregate and re-export submodule symbols that look
locally unused. Explicit re-exports are protected: `from y import z as z`
(self-alias) is never flagged; a binding is left alone when *another*
workspace module does `from <this_module> import <name>` (or `import *`),
since removing it would break that importer; and a binding listed in this
module's own static `__all__` is treated as an intentional public re-export
and left alone. The cross-module guard reads the already-decoded workspace
analysis; the `__all__` guard reads a literal `__all__ = [...]` / `(...)` /
`{...}` of string constants (a dynamically built `__all__` cannot be inspected
statically and offers no protection). Usage detection inherits the
find-references caveats: a binding used only inside a skipped
forward-reference string annotation is not seen as used and may be flagged.

**Code actions.** `textDocument/codeAction` returns `CodeAction[]` (all
`kind: "quickfix"`), or `[]`. The server recomputes diagnostics for the
document (stateless, pull-diagnostics style), keeps those whose line falls
inside the request `range` — anchoring is line-granular; the request's
character offsets do not further trim which diagnostics on a line contribute —
and turns each into a fix. Each returned action echoes its anchor `Diagnostic`
in `diagnostics` and carries a `WorkspaceEdit` under `edit`
(`{"changes": {uri: [TextEdit]}}`). Three anchors are handled:

- `unused-import` → *"Remove unused import 'name'"* (removes the alias, or the
  whole statement when it is the sole name);
- `missing-import` → *"Remove unresolvable import"* (same deletion machinery);
- `unresolved-symbol` → *"Remove import of 'name'"*, plus *"Import 'name' from
  '<module>'"* (rewrites the from-module span) — offered **only** when the
  statement imports just that one name and exactly one workspace module
  exposes a top-level `function` / `class` / `variable` of that name in
  `workspace_symbol_index`. An ambiguous or absent target yields just the
  removal action, and a multi-name statement is never retargeted (rewriting
  the from-module would break the sibling names that still resolve).

`context.only` is honored — a request that does not admit `quickfix` gets
`[]`. Unparseable files yield `[]` (every fix needs the AST). Quick fixes are
the whole surface: no refactorings, no `refactor.*` / `source.*` kinds.
Deferred: add-import for undefined bare names (no undefined-name diagnostic
exists, and scope analysis would risk false positives against builtins /
star-imports / locals) and `pyproject.toml` edits for `undeclared-import`.
Consumer entrypoint: `WorkspaceSession.code_actions_for_range(path,
start_line, start_character, end_line, end_character)` → tuple of
`CodeAction(title, kind, diagnostic, edits)`, where each
`CodeActionEdit(path, range, new_text)` uses the public `SourceRange`
contract.

### File rename and delete import edits

Both operations are advertised under `workspace.fileOperations` with a
`**/*.py` filter (see the capabilities block above), and both return a
`WorkspaceEdit`, or `null` when no edits are needed. Multiple entries in one
request are batched against the *current* workspace state — no chaining is
attempted, so a swap (A↔B) produces independent edits for each direction.

#### `workspace/willRenameFiles`

For each `{oldUri, newUri}` pair the server walks every Python file in the
workspace and emits text edits that update the `import` and `from` statements
referencing the renamed file's module name. Three rewrite shapes:

1. `import <old_module> [as alias]` — the dotted-module span becomes
   `<new_module>`; the `as` clause is preserved.
2. `from <old_module> import …` — the dotted-module span (including any
   leading dots) is rewritten. The existing relative `level` is preserved when
   both old and new modules live under the same package anchor; otherwise the
   statement is rewritten to absolute form (`from <new_module> import …`,
   `level == 0`).
3. `from <pkg> import <leaf> [as alias]` where `<pkg>.<leaf> == old_module`
   and `old_module` / `new_module` share the same parent package — the leaf is
   rewritten to `new_module`'s leaf; the `as` clause is preserved.

Renames are silently skipped when either path is outside the workspace, is not
a `.py` file, is `__init__.py` (package renames change the module names of
every file under the package; supporting them cleanly is a separate feature),
or yields an unchanged module name.

Limitations: the rewrite of `import <old_module>` (with no `as` clause)
preserves the leading binding (`a` in `import a.helper`) but does not update
attribute-access usage sites (`a.helper.foo()` is not rewritten to
`a.utils.foo()`); likewise, `from <pkg> import <leaf>` rewrites when the
parent stays the same but is intentionally skipped on cross-directory moves —
both shapes would need usage-site rewrites that are outside the scope of a
file-rename event, so the user is expected to follow up with a symbol rename
or a manual fix. The renamed file's own internal imports are not rewritten
either: a file moved to a new directory may need its relative imports updated
by hand — only *consumers* of the file's module are updated.

Consumer entrypoint:
`WorkspaceSession.import_edits_for_file_renames(renames)` accepts an iterable
of `(old_path, new_path)` pairs → tuple of
`FileRenameEdit(path, range, new_text)`, sorted by `(path, range.start)`.

#### `workspace/willDeleteFiles`

For each `{uri}` entry the server walks every Python file in the workspace and
emits text edits that remove the `import` and `from` statements which would
become broken once the file is gone. Three deletion shapes:

1. `import <deleted_module> [as alias]` — the whole statement is removed
   (range spans the line including its trailing newline) when it is the only
   alias; otherwise only the dead alias plus its adjacent comma is removed
   (`import a, b` with `a` deleted → `import b`).
2. `from <deleted_module> import …` — the whole statement is removed (every
   imported name's source module is gone).
3. `from <pkg> import <leaf> [as alias]` where
   `<pkg>.<leaf> == deleted_module` — the whole statement is removed when it
   is the only imported name, else only the dead leaf plus its adjacent comma.

Deletions are silently skipped when the path is outside the workspace, is not
a `.py` file, or is `__init__.py` (package deletes would need to remove every
`import pkg.*` / `from pkg.* import …` statement transitively; supporting them
cleanly is a separate feature). Importers that are themselves part of the same
delete batch are skipped — there is no point editing a file the client is
about to remove.

Limitations: attribute-access usage sites are not rewritten — deleting
`helper.py` removes `import helper`, but any subsequent `helper.foo()` usage
sites are left for the user to clean up, mirroring the rename limitation on
the same shape. Aliases inside a multi-name import whose `ast.alias` end
positions are unavailable are skipped (the surviving statement still
references the deleted module, but at least the file is not mis-edited); on
Python 3.11+ — the supported matrix — `ast.alias` nodes carry `end_lineno` /
`end_col_offset`, so this is a defensive fallback rather than something users
encounter in practice. The edits assume the source compiles as Python:
importers whose overlay / on-disk text fails `ast.parse` are skipped entirely.

Consumer entrypoint:
`WorkspaceSession.import_edits_for_file_deletions(deletions)` accepts an
iterable of paths → tuple of `FileDeletionEdit(path, range, new_text)`, sorted
by `(path, range.start)`; `new_text` is always `""`.

## Not supported

- `textDocument/formatting`.
- `completionItem/resolve` — a deliberate design decision, not a gap.
  `resolveProvider` is `false`: items are fully populated in the initial
  response (`label` / `kind` / `detail` / `sortText`), `detail` is a cheap
  already-decoded `Signature`, and the payload is capped at
  `_COMPLETION_LIMIT = 200`, so a lazy resolve round-trip would save nothing.
- Hover or goto-definition on stdlib or installed-package symbols — resolution
  correctly classifies them as `stdlib` / `installed`, but the LSP does not
  synthesize a `Location` for out-of-workspace targets.
- Imports inside conditional blocks other than the recognized guards
  (`if sys.version_info >= ...`, etc.) — the symbol walker treats these as a
  "conditional top-level binding" impurity and does not walk into them. The
  recognized guards are described in [Resolution model](#resolution-model).
- Multi-hop `from X import *` chains where an intermediate uses only bare
  `from Y import *` without `__all__` or explicit re-exports. The
  intermediate's wildcard export surface is empty by design, so resolution
  returns `missing`. (See
  `test_symbol_at_wildcard_chain_is_bounded_by_intermediate_surface`.)
- Re-export chains deeper than `MAX_FOLLOW_DEPTH = 8` — returns
  `resolution == "ambiguous"` and the LSP returns `[]`.
- Cyclic re-exports — detected and returned as `resolution == "ambiguous"`;
  the LSP returns `[]`.

Per-feature limitations — what each provider deliberately does not do — live
in the feature subsections above.

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
isn't there at all, it may fall under a known unsupported case (see
[Not supported](#not-supported)) — most commonly a conditional-block import or
a multi-hop wildcard.

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
contract. See [docs/architecture.md](architecture.md) for the v3 scope
boundary.
