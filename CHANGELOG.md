# Changelog

All notable changes to this project will be documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
