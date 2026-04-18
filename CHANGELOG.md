# Changelog

All notable changes to this project will be documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-04-18

First stable release.

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
