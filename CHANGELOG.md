# Changelog

All notable changes to this project will be documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Items in this section are queued for the next v2.x release.

### Added

- **`textDocument/rename` (and `textDocument/prepareRename`) in `pyinc-tools`
  LSP.** The server now advertises
  `renameProvider: {prepareProvider: true}`. `prepareRename` returns the range
  of the identifier under the cursor and a placeholder when the symbol resolves
  to a workspace target (otherwise `null`). `rename` returns a `WorkspaceEdit`
  with `changes` keyed by document URI. Edits cover (a) every `Name` /
  `Attribute` occurrence already produced by
  `symbol_resolution.find_references`; (b) the `def`/`class`/`async def`
  declaration site (the `find_references` synthetic placeholder is repaired by
  locating the actual identifier offset in the source line); and (c) every
  `from <defining_module> import <bare_old> [as <alias>]` line in the
  workspace, with only the source-name part rewritten so any `as <alias>`
  clause is preserved. Invalid identifiers (`"1bad"`, `""`) and Python
  keywords (`"class"`, `"return"`) yield a JSON-RPC `RequestFailed` (-32803)
  error with a human-readable message; renaming a symbol via an
  `import ... as` alias (e.g. clicking on `aliased` in
  `from a import foo as aliased`) is refused with a `RequestFailed` error
  directing the user to rename the canonical name instead. Same-name and
  non-workspace targets return `null`. The consumer-layer entrypoint
  `WorkspaceSession.rename_symbol(path, qualified_name, new_name)` returns a
  structured `RenameResult(target, edits, status)` carrying the target's
  `ResolvedSymbol`, a tuple of `RenameEdit(path, lineno, col_offset,
  end_col_offset, new_text)`, and one of the statuses `"ok"`,
  `"non_workspace_target"`, `"invalid_identifier"`, `"keyword_identifier"`,
  `"same_name"`, or `"alias_rename_unsupported"`. New public names re-exported
  from `pyinc_tools`: `RenameEdit`, `RenameResult`, `RenameStatus`. Lives
  entirely on top of the stable `pyinc.integrations` surface — no kernel
  contract change.
- **Forward-reference string annotations are now scanned for references.**
  `symbol_resolution.find_references` (and the LSP `textDocument/references`
  it backs) now detects names inside forward-reference strings such as
  `def g(a: 'Foo')`, `x: 'list[Foo]'`, `x: 'pkg.Foo'`, and `'Foo | None'`.
  Internally, `name_occurrences_for_file` performs a second pass over the
  annotation slots `AnnAssign.annotation`, `arg.annotation`, and
  `FunctionDef`/`AsyncFunctionDef.returns`, re-parses string-valued
  `ast.Constant` nodes via `ast.parse(value, mode="eval")`, and emits the
  inner `Name`/`Attribute` references with offsets translated back to file
  coordinates. Each new occurrence flows through the same
  `resolve_symbol_payload` verification used for bare `Name`/`Attribute`
  references, so workspace-only filtering, `MAX_FOLLOW_DEPTH`, and
  `if TYPE_CHECKING:` / `try: except ImportError:` guard handling all
  carry through unchanged. String annotations that span multiple lines,
  are triple-quoted, contain escape sequences, or use implicit string
  concatenation are skipped (offset reconstruction would be ambiguous);
  malformed annotation strings are silently ignored. No payload shape or
  public surface change.

## [2.0.0]

This is the v2.0.0 release (still in development; not yet tagged). v1.2.1 was
the last v1 release. Items previously listed under "Version 1 did not include"
in `docs/architecture.md` are resolved here, except for the still-deferred
*schedulers or worker pools*.

### Added

- **`notebook` integration (Jupyter `.ipynb`).** New stable integration
  `pyinc.integrations.notebook` exposes `notebook_analysis(db, path)` and
  `workspace_notebook_analysis(db, root)` plus the dataclasses
  `NotebookAnalysis`, `NotebookCell`, and `NotebookDiagnostic`. Code cells'
  Python source is concatenated and parsed via `ast` to surface module-level
  imports and definitions per cell, with cutoff-based backdating on the
  parsed structure (whitespace-only / output-only edits are backdated and
  do not invalidate downstream consumers). Markdown and raw cells are
  preserved with their first-line heading (markdown) or kind tag.
  Stdlib-only — uses `json` to decode the notebook envelope; no `nbformat`
  dependency. Resolves the v1 architectural non-goal "notebook integration".
- **Push observers in the kernel.** New `Database.observe(callback, query,
  *args, **kwargs) -> Subscription` registers a callback that fires when the
  identified query node's stored value changes (decision `"executed"`).
  Backdated and reused decisions do not fire — the stored value did not move.
  Events are delivered as `QueryChangeEvent` frozen dataclasses carrying
  `query_id`, `args_digest`, `decision`, `changed_at`, and `verified_at`.
  Dispatch runs after the outermost request scope completes and the kernel
  lock is released, so a callback may safely call back into the database;
  callback-level exceptions are routed to an optional
  `Database(observer_error_hook=...)` hook (default: a one-line stderr log)
  and do not suppress sibling callbacks. `Subscription.unsubscribe()` detaches
  a callback and is idempotent. New public names re-exported from `pyinc`:
  `QueryChangeEvent`, `Subscription`, `ObserverCallback`, `ObserverErrorHook`.
  Resolves the v1 architectural non-goal "push observers in the kernel".
- **Mutable object graphs across cached boundaries.** `freeze` / `thaw` now
  memoize shared object identity and reconstruct cyclic structures via the
  new `FrozenGraph(nodes, root)` envelope and `FrozenRef(index)` pointer
  snapshot variants. Previously the boundary raised
  `UnsupportedValueError("Cyclic values cannot cross cached boundaries.")`
  and silently dropped shared identity. Pure-tree inputs continue to produce
  the v1 flat snapshot shape (zero overhead in the common case); only inputs
  with actual sharing or cycles are wrapped in `FrozenGraph`. `thaw` runs a
  two-pass allocate-then-fill so a list-with-itself round-trips to an actual
  self-referential list and shared sub-objects retain identity. Resolves the
  v1 architectural non-goal "arbitrary mutable object graphs across cached
  boundaries". New public names re-exported from `pyinc`: `FrozenGraph`,
  `FrozenRef`.
- **Content-addressed artifact storage.** New `ArtifactStore` Protocol and
  two shipped implementations: `InMemoryArtifactStore` (dict-backed) and
  `FileSystemArtifactStore` (git-style two-character fan-out under
  `<root>/objects/<digest[:2]>/<digest[2:]>` with atomic `tempfile`+`os.replace`
  writes). `Database(store=...)` writes the serialized snapshot bytes for
  every value crossing the membrane, keyed by the `fingerprint_snapshot`
  digest. New `serialize_snapshot(snapshot)` and `deserialize_snapshot(payload)`
  helpers expose the byte form to external callers; both round-trip the full
  snapshot grammar including `FrozenGraph` / `FrozenRef`. Cross-run cache
  reuse is delivered via the durable checkpoint API:
  `Database.save_checkpoint(store=None) -> str` serialises all current query
  and resource node records (plus their dependency edges and snapshot bytes)
  to an `ArtifactStore` and returns a content-addressed checkpoint key
  prefixed with `"ck"`. A subsequent `Database.load_checkpoint(key, store=None)`
  in a fresh process reads the manifest back, verifies that all declared
  input digests and resource probe hints still match, and pre-warms the node
  record cache so that the next `db.get(query)` reuses the stored result
  without re-executing the query function. If any dependency is stale the
  affected query is silently re-executed and the new result is compared
  against the stored snapshot for backdating (from-scratch consistency is
  maintained). Both methods accept an optional `store=` kwarg for call-site
  store injection; `save_checkpoint` also writes all referenced snapshot
  bytes to the store, making it self-contained. The checkpoint key is
  content-addressed: identical database state always produces the same key.
  New public names re-exported from `pyinc`: `ArtifactStore`,
  `InMemoryArtifactStore`, `FileSystemArtifactStore`, `serialize_snapshot`,
  `deserialize_snapshot`. Resolves the v1 architectural non-goal
  "content-addressed artifact storage".
- **`try/except ImportError` import support.** `symbol_resolution` now
  recognises `try: … except ImportError:` and `try: … except
  ModuleNotFoundError:` (and the tuple form `except (ImportError,
  ModuleNotFoundError):`) guard blocks at the module top level and walks
  their bodies for `import` and `from … import` statements. The collected
  symbols appear in `ModuleSymbolTable.symbols` with the existing
  `import_alias` / `from_import_alias` kinds, exactly as if the imports
  were unconditional. The "conditional top-level binding" impurity marker
  is no longer recorded for files whose only conditional blocks are
  recognised import-error guards. `python_source` likewise collects import
  statements and bound names from such blocks, so that
  `import_statements_for_file` and the module binding analysis agree with
  the symbol table. Bare `except:` handlers (and handlers for other
  exception types) still set the impurity marker.
- **Kernel digest format bump (`K2;`).** The `fingerprint_snapshot` encoder
  prefixes its byte form with `K2;` so older `K1;` / unprefixed payloads in
  any external durable cache cannot be silently accepted. In-memory state
  across a process restart is unaffected. This is the standard
  encoder-change-requires-identity-bump path documented in
  `docs/kernel-contract.md`.

### Changed

- **Value boundary preserves shared identity.** When the same mutable
  container appears at two slots of an input value, both reads from the
  thawed copy in `checked` / `fast` mode now refer to the same Python object
  rather than two independent copies. This is consistent with the new mutable
  graph support. The kernel's stored snapshot remains immutable and safe; the
  mode table (strict / checked / fast) is unchanged. Tests that previously
  asserted v1's silent identity-drop behavior have been split: the
  v1-shaped *independent inputs* test continues to verify that two separately
  constructed dicts thaw independently, and a new companion test exercises
  the v2 *shared input* case explicitly.
- **`docs/kernel-contract.md` limitation #4 amended** to describe the
  outbound `ArtifactStore` and the durable `save_checkpoint` /
  `load_checkpoint` flow.
- **`pyinc-tools` LSP `serverInfo.version`** bumped from `"1.2.0"` to
  `"2.0.0"` to align with the kernel.

### Documentation

- Updated `docs/integration-authoring.md` line citations into
  `python_source.py` to the current line numbers.
- Removed the phantom v1.3.0 reference in `docs/pyinc-tools-guide.md`; the
  features described there shipped across v1.2.0 and v1.2.1 and continue in
  v2.0.0.
- Fixed `docs/pyinc-tools-guide.md` to list `try: … except ImportError:` guard
  blocks under "Supported" (they were added to the `symbol_resolution` walker in
  this release) and removed them from the "Not supported" conditional-blocks
  bullet, which now correctly names only `if sys.version_info >= …` style guards.
- Updated `docs/architecture.md` "Scope" section to replace the crossed-out
  development-cycle tracking list with a clean summary of what v2.0.0 resolved.
- Added `examples/checkpoint_demo.py` showing `save_checkpoint` /
  `load_checkpoint` cross-run cache reuse with `FileSystemArtifactStore`: three
  simulated runs demonstrating cold execution, full checkpoint reuse, and partial
  reuse when one input changes.

## [1.2.1] — 2026-04-24

### Added

- **`if TYPE_CHECKING:` import support.** `symbol_resolution` now recognises
  `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:` guard blocks at the
  module top level and walks their bodies for `import` and `from … import`
  statements. The collected symbols appear in `ModuleSymbolTable.symbols` with
  the existing `import_alias` / `from_import_alias` kinds, exactly as if the
  imports were unconditional. As a result, LSP hover and goto-definition work
  for names that are referenced as bare identifiers (e.g. `x: Foo`) even when
  the binding lives under a `TYPE_CHECKING` guard. The "conditional top-level
  binding" impurity marker is no longer recorded for files whose only
  conditional blocks are `TYPE_CHECKING` guards; other conditional blocks (e.g.
  `if sys.version_info >= …`) still set the marker. Non-import statements inside
  a `TYPE_CHECKING` block (unusual) are silently skipped rather than being
  promoted to the symbol table.

### Notes

- Kernel contract (`src/pyinc`) unchanged. Minor version bump reflects new
  behaviour in the `symbol_resolution` integration, which is part of the stable
  `pyinc.integrations` public surface.
- Remaining `find_references` limitation: forward-reference strings (`'Foo'` in
  annotations) are not scanned during the AST name-occurrence walk, so
  string-annotation usages are not included in reference results.

## [1.2.0] — 2026-04-22

### Added

- **`textDocument/references`.** `pyinc-tools lsp` now advertises
  `referencesProvider` and honors `context.includeDeclaration`. References
  are returned with per-occurrence character ranges (`col_offset` /
  `end_col_offset` from the AST, not the line-0 placeholder used for some
  other requests), so editors can highlight every match precisely.
- **`pyinc.integrations.find_references`.** New stable entrypoint +
  `Reference` and `ReferenceQueryResult` dataclasses. Backed by two new
  composition-layer `@query` functions in `symbol_resolution`:
  `name_occurrences_for_file` (full-AST `Name`/`Attribute` walk) and
  `workspace_name_occurrence_index`. Candidate filtering is bounded by a
  bare-name pre-filter; each surviving candidate is verified through
  `resolve_symbol_payload`, so results respect the existing
  `MAX_FOLLOW_DEPTH = 8` cross-module re-export semantics. Only
  workspace-resolved targets are indexed; `stdlib`/`installed`/`ambiguous`
  targets return an empty tuple with the `ResolvedSymbol` carried on the
  result.
- **`WorkspaceSession.find_references`.** Mirror-path aware wrapper around
  the integration entrypoint; paths in the returned `Reference` tuples are
  remapped to the real workspace root.
- **Threaded live polling.** `PollingWorkspaceWatcher.start(on_change, *,
  interval_s, on_error)` spawns a daemon thread that delivers debounced
  change batches to a caller-supplied callback; `stop(timeout=5.0)` joins
  the thread cleanly. Context-manager support (`with watcher: ...`)
  guarantees `stop()` on exit. Exceptions from `on_change` are forwarded to
  the optional `on_error` hook, or logged to stderr by default, without
  killing the watcher thread. `poll()` remains available for synchronous
  use but raises `RuntimeError` while the thread is running (one driver at
  a time).
- **LSP live polling.** `pyinc-tools lsp` starts a threaded
  `PollingWorkspaceWatcher` in `initialize` by default so external file
  changes (e.g. `git pull`, formatter scripts) publish fresh diagnostics
  without requiring `workspace/didChangeWatchedFiles` from the editor.
  Opt-out via `initializationOptions.pyinc.watcher.enabled=false`; tune via
  `pyinc.watcher.debounceMs` and `pyinc.watcher.intervalMs`. Repeated
  `publishDiagnostics` for an unchanged URI are suppressed via a
  diagnostic-tuple signature cache.
- **CLI `--poll-interval-ms` flag.** Explicit control over watcher poll
  cadence. `pyinc-tools analyze --watch` now drives its loop through the
  threaded watcher API; behavior is unchanged.

### Changed

- **`WorkspaceSession` is thread-safe for its own public surface.** A
  session-level `threading.RLock` now guards `set_overlay`, `clear_overlay`,
  `refresh_paths`, `analyze_file`, `analyze_workspace`,
  `resolve_symbol_reference`, and `find_references`; mutators raise
  `RuntimeError` once `close()` has been called so the watcher thread
  exits cleanly when the session shuts down. The kernel's existing
  `Database` `RLock` is unchanged.

### Notes

- Kernel contract (`src/pyinc`) unchanged. Minor version bump reflects new
  public consumer-layer API surface only. Watcher loops and LSP wiring
  remain architectural non-goals for the kernel itself; all new code lives
  in `pyinc_tools` on top of stable `pyinc.integrations` entrypoints.
- Known limitations for `find_references` in v1.2.0:
  - References via attribute access to a module-level symbol only
    imported as a module (`import a; a.foo()`) are not counted because
    the resolver is name-local. Use `from a import foo` to opt in.
  - Forward-reference strings (`'Foo'` in annotations) are not scanned.
  - Function-local shadowing is not modeled: a local `foo = 1` inside a
    function is still reported as a reference to a module-level `foo`.
    `symbol_resolution` is module/class-scope only per
    `docs/integration-contract.md`.

## [1.1.1] — 2026-04-22

### Added

- **`docs/pyinc-tools-guide.md`.** Consumer-facing guide covering install,
  `pyinc-tools analyze` (one-shot + `--watch`), `pyinc-tools lsp` (stdio +
  advertised capabilities), editor wiring (Neovim, Emacs/eglot, VS Code note),
  the `WorkspaceSession` overlay model, a supported-vs.-not-yet table, and
  troubleshooting. Cross-linked from `README.md`.
- **LSP hardening tests.** Added coverage for single-level wildcard goto-def,
  the `MAX_FOLLOW_DEPTH = 8` boundary, cyclic re-exports returning
  `ambiguous`, ambiguous wildcard lookups, the full eight-kind
  `documentSymbol` surface, and the current `if TYPE_CHECKING:` limitation.

### Notes

- Kernel contract (`src/pyinc`) unchanged. Patch-level release: docs and test
  coverage only.

## [1.1.0] — 2026-04-21

### Added

- **LSP hover and goto-definition.** `pyinc-tools lsp` now advertises
  `hoverProvider` and `definitionProvider`. Hover returns a markdown signature
  for the symbol under the cursor (functions with parameters and return
  annotation, classes, annotated variables, re-exported aliases);
  goto-definition follows cross-module re-exports via
  `symbol_resolution.resolve_symbol` and returns a `Location` in the defining
  module.
- **WorkspaceSession API.** `resolve_symbol_reference(path, qualified_name)`
  wraps `resolve_symbol` with mirror-root → real-root path remapping.
  `source_text(path)` returns the active overlay or on-disk contents for a
  tracked file.

### Notes

- Kernel contract (`src/pyinc`) is unchanged; the minor version bump reflects
  new public API on the `pyinc_tools` consumer layer. LSP wiring and
  push-based watchers remain architectural non-goals for the kernel itself;
  they live in `pyinc_tools` on top of stable `pyinc.integrations`
  entrypoints.

## [1.0.1] — 2026-04-21

### Added

- **Consumer tooling.** New `pyinc_tools` layer with a mirror-workspace
  `WorkspaceSession`, polling/debounce watcher support, `pyinc-tools analyze`,
  and `pyinc-tools lsp`, all kept outside `src/pyinc` so the kernel contract
  stays stable.
- **Examples.** Focused diagnostics/escape-hatch examples for
  `inspect_fresh(...)`, `explain_query_captures(...)`, and
  `report_untracked_read(...)`.

### Changed

- **Docs.** Reconciled the stable v1.x release story across `AGENTS.md`,
  `README.md`, `docs/architecture.md`, and `docs/integration-contract.md`.
- **Runtime diagnostics.** Unsupported ambient-capture failures now point users
  to `pyinc.explain_query_captures(...)` for preflight inspection.

## [1.0.0] — 2026-04-18

The first stable v1 release.

### Added

- **Kernel.** Pull-based red-green verification, backdating (early cutoff),
  `strict` / `checked` / `fast` value-membrane modes, LRU eviction, cycle
  detection, untracked-read guards, `Database.set_many(...)` batch
  invalidation, `Database.dependency_graph(...)` export,
  `Database.inspect(...)` / `Database.explain(...)` provenance, and
  `Database.statistics()` / `Database.query_profile()` observability.
- **Built-in resources.** `FileResource`, `FileStatResource`, `EnvResource`,
  `DirectoryResource`.
- **Twelve shipped integrations** under `pyinc.integrations`:
  `python_source`, `toml_config`, `requirements_txt` (including
  `deep_requirements_analysis` for recursive `-r` following),
  `installed_packages`, `json_config`, `dependency_check`, `env_file`,
  `xml_config`, `csv_data`, `deep_module_resolution`, `requirement_evaluation`
  (PEP 440 specifier satisfaction + PEP 508 marker evaluation), and
  `symbol_resolution` (module- and class-level symbol tables with bounded
  cross-module re-export resolution).
- **Typing.** Inline `py.typed` marker; `mypy --strict` clean.
- **Docs.** `kernel-contract.md`, `integration-contract.md`,
  `integration-authoring.md`, `architecture.md`.

### Notes

- Zero runtime dependencies; pure-Python, stdlib-only.
- Tested on CPython 3.11, 3.12, and 3.13.
- LSP wiring and push-based filesystem watchers are architectural non-goals
  for v1; see `docs/architecture.md` for scope boundary.

[1.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.0.0
[1.0.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.0.1
[1.1.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.1.0
[1.1.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.1.1
[1.2.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.2.0
[1.2.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.2.1
[2.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.0.0
