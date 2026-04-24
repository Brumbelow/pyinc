# Changelog

All notable changes to this project will be documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
