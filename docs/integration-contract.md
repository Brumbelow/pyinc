# Integration Contract

`pyfoundinc` keeps integrations narrow on purpose. The core runtime contract stays in
`docs/kernel-contract.md`; this document defines what an integration may expose as public API.

## Stable Public API

For `pyfoundinc.integrations.python_source`, the stable public surface is:

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

`pyfoundinc.integrations` re-exports only that stable surface.

## Experimental Helpers

Low-level query nodes and payload helpers in `pyfoundinc.integrations.python_source` are retained for
debugging and targeted tests. Examples include `source_text`, `imports_for_file`,
`resolved_imports_for_file`, and the `*_payload` helpers.

Those names remain importable from the module itself, but they are experimental:

- they are not re-exported from `pyfoundinc.integrations`
- they do not carry the same compatibility promise as the stable dataclass views and entrypoints
- new integrations should not depend on them as a public contract

## Current Reference Integration Scope

The reference integration is intentionally narrow:

- workspace-local module discovery rooted at the supplied directory
- traversal is cycle-safe and constrained to real paths under the supplied root
- top-level imports and top-level definitions only
- syntax diagnostics only
- dependency invalidation based on resolved module export surfaces
- conservative import resolution with `workspace`, `external`, `missing`, and `ambiguous` outcomes

When a resolution case is unsupported or structurally ambiguous, the integration must prefer
`missing`/`ambiguous` or re-execution over optimistic dependency reuse.

## Out Of Scope

This contract does not include:

- full `sys.path` or installed-package resolution
- symbol or type resolution
- LSP wiring
- file watchers or schedulers
- widening the core runtime semantics to accommodate integration convenience
