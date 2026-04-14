# Integration Contract

`pyfoundinc` keeps integrations narrow on purpose. The core runtime contract stays in
`docs/kernel-contract.md`; this document defines what an integration may expose as public API.

## Stable Public API

`pyfoundinc.integrations` re-exports only the stable dataclass/result types and
high-level entrypoints from the shipped integrations below.

For `pyfoundinc.integrations.python_source` (the reference integration), the stable public
surface is:

- dataclass result types:
  - `ImportRef`
  - `DefinitionRef`
  - `Diagnostic`
  - `ResolvedImportRef`
  - `DependencySurface`
  - `PythonFileAnalysis`
  - `PythonModuleAnalysis`
  - `PythonWorkspaceAnalysis`
- high-level entrypoints:
  - `file_analysis(db, path)`
  - `directory_analysis(db, root)`
  - `module_analysis(db, root, path)`
  - `workspace_analysis(db, root)`

For `pyfoundinc.integrations.toml_config`, the stable public surface is:

- dataclass result types:
  - `ConfigKey`
  - `ConfigSection`
  - `ConfigAnalysis`
- high-level entrypoints:
  - `config_analysis(db, path)`
  - `workspace_config_analysis(db, root)`

For `pyfoundinc.integrations.requirements_txt`, the stable public surface is:

- dataclass result types:
  - `RequirementRef`
  - `FileReference`
  - `IndexDirective`
  - `RequirementsAnalysis`
- high-level entrypoints:
  - `requirements_analysis(db, path)`
  - `workspace_requirements_analysis(db, root)`

## Experimental Helpers

Low-level query nodes, payload helpers, decode helpers, and module-local resource helpers in
the integration submodules are retained for debugging and targeted tests. Examples include
`source_text`, `imports_for_file`, `config_file_text`, `requirements_payload`, and the
`*_payload` helpers.

Those names remain importable from their defining submodules, but they are experimental:

- they are not re-exported from `pyfoundinc.integrations`
- they do not carry the same compatibility promise as the stable dataclass views and entrypoints
- new integrations should not depend on them as a public contract

## Python Source Integration Scope

`python_source` is intentionally narrow:

- workspace-local module discovery rooted at the supplied directory
- traversal is cycle-safe and constrained to real paths under the supplied root
- top-level imports, top-level definitions, and simple top-level assignments for export-surface tracking
- syntax diagnostics only
- dependency invalidation based on resolved module export surfaces, including conservative static support for `from x import *`
- conservative import resolution with `workspace`, `external`, `missing`, and `ambiguous` outcomes

When a resolution case is unsupported or structurally ambiguous, the integration must prefer
`missing`/`ambiguous` or re-execution over optimistic dependency reuse.

Wildcard export handling is intentionally static:

- literal top-level `__all__ = [...]` / `(...)` / `{...}` assignments of string constants are honored
- otherwise wildcard exports fall back to statically known top-level bound names that do not start with `_`
- dynamic `__all__`, provider-side wildcard re-exports, and other unsupported top-level export shapes are treated conservatively via re-execution instead of optimistic reuse

## TOML Config Integration Scope

The TOML config integration is intentionally narrow:

- single-file TOML analysis via `config_analysis(db, path)`
- workspace-root discovery of `pyproject.toml` via `workspace_config_analysis(db, root)`
- section extraction by walking the TOML table hierarchy
- dependency extraction from `[project.dependencies]` and `[project.optional-dependencies]`
- tool config discovery under `[tool.*]`
- syntax diagnostics for malformed TOML
- cutoff-based backdating using parsed structure (comment-only edits are backdated)
- TOML datetime values are converted to ISO strings (no adapter required)

## Requirements.txt Integration Scope

The `requirements_txt` integration is intentionally narrow:

Scope:

- single-file requirements.txt parsing via `requirements_analysis(db, path)`
- workspace-root discovery of `requirements.txt` via `workspace_requirements_analysis(db, root)`
- PEP 508 specifier extraction: package name, extras, version constraints, environment markers
- package name normalization per PEP 503 (lowercase, hyphens/dots to underscores)
- file references (`-r`/`--requirement`, `-c`/`--constraint`)
- index directives (`--index-url`, `--extra-index-url`, `--find-links`)
- editable install detection (`-e`/`--editable`)
- URL-based requirements (`name @ url`)
- line continuation support (backslash-newline)
- diagnostics for unparseable lines
- cutoff-based backdating (comment-only and whitespace-only edits are backdated)

Out of scope for this integration:

- marker expression evaluation
- version specifier satisfaction or resolution
- URL fetching or VCS cloning
- recursive `-r` file inclusion (references are extracted but not followed)
- pip-specific options beyond index/find-links directives

## Out Of Scope

This contract does not include:

- full `sys.path` or installed-package resolution
- symbol or type resolution
- LSP wiring
- file watchers or schedulers
- widening the core runtime semantics to accommodate integration convenience
