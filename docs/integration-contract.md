# Integration Contract

`pyinc` keeps integrations narrow on purpose. The core runtime contract stays in
`docs/kernel-contract.md`; this document defines what an integration may expose as public API.

## Stable Public API

`pyinc.integrations` re-exports only the stable dataclass/result types and
high-level entrypoints from the shipped integrations below.

Stable public surface by module:

- `python_source`
  - result types: `ImportRef`, `DefinitionRef`, `Diagnostic`,
    `ResolvedImportRef`, `DependencySurface`, `PythonFileAnalysis`,
    `PythonModuleAnalysis`, `PythonWorkspaceAnalysis`
  - entrypoints: `file_analysis(db, path)`, `directory_analysis(db, root)`,
    `module_analysis(db, root, path)`, `workspace_analysis(db, root)`
- `toml_config`
  - result types: `ConfigKey`, `ConfigSection`, `ConfigAnalysis`
  - entrypoints: `config_analysis(db, path)`,
    `workspace_config_analysis(db, root)`
- `json_config`
  - result types: `JsonKey`, `JsonSection`, `JsonAnalysis`
  - entrypoints: `json_analysis(db, path)`,
    `workspace_json_analysis(db, root, filename)`
- `requirements_txt`
  - result types: `RequirementRef`, `FileReference`, `IndexDirective`,
    `RequirementsAnalysis`
  - entrypoints: `requirements_analysis(db, path)`,
    `workspace_requirements_analysis(db, root)`,
    `deep_requirements_analysis(db, path)`
- `installed_packages`
  - result types: `InstalledPackageRef`, `ImportNameResolution`,
    `InstalledPackagesAnalysis`
  - entrypoints: `installed_packages_analysis(db)`,
    `resolve_import_name(db, import_name)`
- `dependency_check`
  - result types: `DependencyStatus`, `UndeclaredImport`,
    `DependencyCheckAnalysis`
  - entrypoints: `dependency_check_analysis(db, declared_deps)`,
    `workspace_dependency_check(db, root, declared_deps)`
- `env_file`
  - result types: `EnvEntry`, `EnvFileAnalysis`
  - entrypoints: `env_analysis(db, path)`, `workspace_env_analysis(db, root)`
- `xml_config`
  - result types: `XmlAttribute`, `XmlElement`, `XmlAnalysis`
  - entrypoints: `xml_analysis(db, path)`,
    `workspace_xml_analysis(db, root, filename)`
- `csv_data`
  - result types: `CsvColumn`, `CsvAnalysis`
  - entrypoints: `csv_analysis(db, path)`, `workspace_csv_analysis(db, root)`
- `deep_module_resolution`
  - result types: `ResolvedModuleLocation`, `NamespacePackage`,
    `PthDirective`, `ModulePathEntry`, `DeepModuleResolutionAnalysis`
  - entrypoints: `resolve_module_path(db, dotted_name)`,
    `deep_module_resolution_analysis(db)`
- `requirement_evaluation`
  - result types: `MarkerEvaluation`, `VersionSpecifierEvaluation`,
    `ApplicableRequirement`, `ApplicableRequirementsAnalysis`,
    `PythonEnvironmentSnapshot`
  - entrypoints: `evaluate_markers(db, marker)`,
    `evaluate_version_specifier(db, specifier, version)`,
    `applicable_requirements(db, path)`,
    `workspace_applicable_requirements(db, root)`
- `symbol_resolution`
  - result types: `Parameter`, `Signature`, `Symbol`, `ModuleSymbolTable`,
    `ResolvedSymbol`, `WorkspaceSymbolEntry`, `WorkspaceSymbolIndex`,
    `Reference`, `ReferenceQueryResult`
  - entrypoints: `module_symbol_table(db, root, path)`,
    `resolve_symbol(db, root, path, qualified_name)`,
    `workspace_symbol_index(db, root)`,
    `find_references(db, root, path, qualified_name, *, include_declaration=True)`
- `notebook` *(added in the v2 development cycle; resolves the v1 architectural non-goal "notebook integration")*
  - result types: `NotebookImport`, `NotebookDefinition`, `NotebookCell`,
    `NotebookDiagnostic`, `NotebookAnalysis`
  - entrypoints: `notebook_analysis(db, path)`,
    `workspace_notebook_analysis(db, root)`

## Experimental Helpers

Low-level query nodes, payload helpers, decode helpers, and module-local resource helpers in
the integration submodules are retained for debugging and targeted tests. Examples include
`source_text`, `imports_for_file`, `config_file_text`, `json_file_text`,
`requirements_payload`, and the `*_payload` helpers.

Those names remain importable from their defining submodules, but they are experimental:

- they are not re-exported from `pyinc.integrations`
- they do not carry the same compatibility promise as the stable dataclass views and entrypoints
- new integrations should not depend on them as a public contract

## Python Source Integration Scope

`python_source` is intentionally narrow:

- workspace-local module discovery rooted at the supplied directory
- traversal is cycle-safe and constrained to real paths under the supplied root
- top-level imports, top-level definitions, and simple top-level assignments for export-surface tracking
- syntax diagnostics only
- dependency invalidation based on resolved module export surfaces, including conservative static support for `from x import *`
- import resolution with `workspace`, `stdlib`, `installed`, `missing`, and `ambiguous` outcomes
- environment-aware resolution via composition with `installed_packages` (stdlib and installed package classification)

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

## JSON Config Integration Scope

The `json_config` integration is intentionally narrow:

Scope:

- single-file JSON analysis via `json_analysis(db, path)`
- workspace-root discovery via `workspace_json_analysis(db, root, filename)` (default filename: `package.json`)
- section extraction by walking the JSON object hierarchy (nested objects become subsections)
- value type classification: string, number, boolean, null, array, object
- syntax diagnostics for malformed JSON
- cutoff-based backdating using parsed structure (whitespace and formatting changes are backdated)
- standard JSON only (stdlib `json` module)
- non-object top-level values (arrays, primitives) produce no sections

Out of scope for this integration:

- JSONC (comments) or JSON5 (trailing commas, unquoted keys)
- schema validation or JSON Schema
- JSON Pointer or JSON Path queries
- recursive `$ref` resolution
- schema-specific field extraction (dependencies, scripts, etc. -- belongs in downstream consumers)

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

## Installed Packages Integration Scope

The `installed_packages` integration is intentionally narrow:

Scope:

- installed package discovery via `.dist-info` directory scanning in site-packages
- `METADATA` file parsing for distribution name, version, summary, and `Requires-Dist`
- `top_level.txt` reading for import-name-to-package mapping (falls back to normalized distribution name)
- stdlib module identification via `sys.stdlib_module_names` (Python 3.10+)
- import name resolution: `stdlib` / `installed` / `unknown`
- resource-tracked site-packages directory listings and metadata files
- `db.report_untracked_read()` for `sys.path` discovery (runtime list, not interceptable)
- cutoff-based backdating on metadata parsing (field-only comparison, whitespace changes backdate)
- `installed_packages_analysis(db)` for full environment analysis
- `resolve_import_name(db, import_name)` for single import resolution
- `environment_index(db)` composition query for cross-integration import resolution (exported in the module's `__all__` but intentionally not re-exported from `pyinc.integrations` — exists for query-layer composition with other integrations, currently used by `python_source`)

Out of scope for this integration:

- `.egg-info` or `.egg` format packages
- namespace package detection
- marker expression evaluation or version satisfaction
- `sys.path` manipulation or `.pth` file processing

## Cross-Integration Composition

Integrations can compose at the query layer by importing `@query` functions from other
integration modules. The kernel's dependency tracking extends automatically across
integration boundaries — if an upstream integration's query result changes, downstream
queries that called it are re-verified and re-executed as needed.

The canonical list of current composition edges is maintained below under
*Cross-Integration Composition Edges*.

Composition queries (e.g. `environment_index`, `installed_distributions_index`,
`resolve_module_location`, the `python_source` helpers consumed by `symbol_resolution`) are
public `@query` functions listed in their defining module's `__all__`, but they are
intentionally **not** re-exported from `pyinc.integrations`. They exist for cross-integration
use at the query layer, not as user-facing entrypoints.

## Dependency Check Integration Scope

The `dependency_check` integration cross-references declared dependencies against the
installed environment:

Scope:

- `dependency_check_analysis(db, declared_deps)` checks declared dependencies (as PEP 508
  specifier strings) against installed packages for missing, satisfied, version-mismatch,
  or ambiguous outcomes
- `workspace_dependency_check(db, root, declared_deps)` extends the base check with
  undeclared import detection by composing with `python_source.workspace_analysis`
  at the entrypoint layer
- PEP 440 version matching for common operators (`==`, `!=`, `>=`, `<=`, `>`, `<`, `~=`)
  with dotted numeric versions; unparseable specifiers return `ambiguous`
- PEP 503 distribution name normalization
- result types: `DependencyStatus`, `UndeclaredImport`, `DependencyCheckAnalysis`

Cross-integration composition:

- imports `installed_distributions_index` from `installed_packages` at the query layer
  (creates an incremental dependency edge — package installs trigger revalidation)
- imports `environment_index` from `installed_packages` at the query layer
- composes with `python_source.workspace_analysis` at the entrypoint layer (not the
  query layer, since `workspace_analysis` is a non-query function)

Out of scope for this integration:

- full PEP 440 version matching (pre-release, post-release, local, wildcard specifiers)
- marker expression evaluation
- transitive dependency resolution
- lock file comparison

## CSV Data Integration Scope

The `csv_data` integration is intentionally narrow:

Scope:

- single-file CSV/TSV structural analysis via `csv_analysis(db, path)`
- workspace-root discovery via `workspace_csv_analysis(db, root)`
- stdlib `csv` module only — no third-party parsers
- header detection and column discovery
- delimiter sniffing via `csv.Sniffer`
- row counting
- inconsistent-column diagnostics (rows with a column count that does not match the header)
- cutoff-based backdating on structural parse

Out of scope for this integration:

- schema validation or type inference
- multi-line field handling beyond what `csv` defaults support
- streaming / iterator APIs (analysis reads the whole file)
- alternate dialects that `csv.Sniffer` cannot detect

## Deep Module Resolution Integration Scope

The `deep_module_resolution` integration resolves dotted module names to physical file paths by walking `sys.path`:

Scope:

- `sys.path` entry walking (the live list, reported via `db.report_untracked_read` because it is runtime-mutable and not resource-tracked)
- `.pth` file processing with whitespace-tolerant / comment-tolerant backdating
- PEP 420 namespace package collection
- dotted-name → file resolution via `resolve_module_path(db, dotted_name)`
- workspace-wide snapshot via `deep_module_resolution_analysis(db)`
- result types: `ResolvedModuleLocation`, `NamespacePackage`, `PthDirective`, `ModulePathEntry`, `DeepModuleResolutionAnalysis`
- `resolve_module_location` composition query (exported in `__all__` but not re-exported from `pyinc.integrations`)

Out of scope for this integration:

- editable-install pointer files beyond simple path-line `.pth` contents
- loader customizations (`sys.path_hooks`, meta path finders)
- `importlib.util.find_spec`-style fully dynamic resolution
- `.egg` / `.egg-info` legacy layouts

## Env File Integration Scope

The `env_file` integration parses `.env`-style key/value files:

Scope:

- single-file `.env` analysis via `env_analysis(db, path)`
- workspace-root discovery via `workspace_env_analysis(db, root)`
- key/value extraction with support for quoted (single or double) vs unquoted values
- `export` prefix handling
- interpolation reference detection (the parser records `$VAR` / `${VAR}` references; it does not evaluate them)
- diagnostics for malformed lines
- cutoff-based backdating on structural parse

Out of scope for this integration:

- interpolation evaluation or variable substitution
- dotenv command-substitution syntax (`$(...)`, backticks)
- multi-line values beyond the single-line `.env` convention
- writing / mutating `.env` files

## Requirement Evaluation Integration Scope

The `requirement_evaluation` integration implements PEP 440 version-specifier satisfaction and PEP 508 marker evaluation:

Scope:

- PEP 440 version parsing and comparison: epochs, pre-release (`a`/`b`/`rc`), post-release, dev, local version labels, wildcard `==x.*`, compatible release `~=`
- PEP 508 marker tokenizer and recursive-descent parser
- marker evaluation against the live Python environment via a single-point private function `_current_python_env()` (tests monkeypatch this one symbol; do not patch `sys` / `platform` attributes directly)
- `PythonEnvironmentResource` captures the environment snapshot for change-triggered re-evaluation
- entrypoints: `evaluate_markers`, `evaluate_version_specifier`, `applicable_requirements`, `workspace_applicable_requirements`
- result types: `MarkerEvaluation`, `VersionSpecifierEvaluation`, `ApplicableRequirement`, `ApplicableRequirementsAnalysis`, `PythonEnvironmentSnapshot`

Cross-integration composition:

- composes with `requirements_txt` payload helpers to read declared requirements
- composes with `installed_packages.installed_distributions_index` to report satisfaction
- shared private helpers `_parse_specifier_set` and `_satisfies` are reused by `dependency_check` (documented exception to the "public `@query` only" rule)

Out of scope for this integration:

- VCS URL / direct URL fetching
- lock-file comparison or resolution
- transitive dependency graph construction
- build-time marker evaluation against non-current Python environments (marker evaluation uses the live `_current_python_env()` only)

## XML Config Integration Scope

The `xml_config` integration parses XML configuration files via the stdlib `xml.etree.ElementTree`:

Scope:

- single-file XML analysis via `xml_analysis(db, path)`
- workspace-root discovery via `workspace_xml_analysis(db, root, filename)`
- element traversal with namespace-aware tag normalization
- dot-path queries into the parsed tree
- attribute extraction for each element
- diagnostics for malformed XML
- cutoff-based backdating on structural parse

Out of scope for this integration:

- DTD validation
- XSD / schema validation
- XInclude / entity resolution beyond defaults
- streaming / pull-parser APIs
- XPath queries beyond the dot-path convention

## Symbol Resolution Integration Scope

The `symbol_resolution` integration exposes workspace-wide symbol tables and cross-module re-export resolution, intentionally narrow and stdlib-only:

Scope:

- module-level and class-level symbol extraction: functions, methods, classes, class variables, top-level variables, import aliases, from-import aliases, wildcard-import stubs
- arbitrary class nesting depth; qualified name scheme is `foo` / `Foo.bar` / `Foo.Inner.bar`
- cross-module resolution that follows re-export chains (`from a import x` → `a.py: from b import x` → ...) bounded by `MAX_FOLLOW_DEPTH = 8` with cycle detection
- type-annotation *text* extraction via `ast.unparse` (no type evaluation, no type checking)
- installed-package lookups stop at the import boundary (`resolution="installed"`, `distribution_name`, `distribution_version`, `defining_path=None`)
- dynamic `__all__` on the wildcard-provider side → `"ambiguous"` with `db.report_untracked_read`
- conditional top-level binding (`if TYPE_CHECKING:`, top-level `For`, `While`, `Try`, `With`) → `impurity_reasons` includes `"conditional top-level binding"` and the binding produces no symbol
- workspace-wide reference index composed over `name_occurrences_for_file` (full-AST `Name`/`Attribute` walk) and verified through `resolve_symbol_payload`; only workspace-resolved targets are indexed (stdlib / installed / ambiguous return empty with `ResolvedSymbol` carried)
- entrypoints: `module_symbol_table`, `resolve_symbol`, `workspace_symbol_index`, `find_references`
- result types: `Parameter`, `Signature`, `Symbol`, `ModuleSymbolTable`, `ResolvedSymbol`, `WorkspaceSymbolEntry`, `WorkspaceSymbolIndex`, `Reference`, `ReferenceQueryResult`

Out of scope for this integration:

- function-local symbols (`find_references` therefore reports a local rebinding like `foo = 1` inside a function as a reference to the module-level `foo` of the same name)
- attribute-chain reference resolution — `import a; a.foo()` is not counted as a reference to `a.foo` because the resolver is name-local at the call site
- forward-reference strings in annotations (e.g. `'Foo'` in `def g(a: 'Foo')`)
- decorator-induced rebinding (`@functools.cache`, `@property`, `@classmethod`, etc.)
- MRO / class-member override resolution
- following into installed third-party source files (v2 concern)
- type evaluation or static type checking

## Notebook Integration Scope

The `notebook` integration parses Jupyter `.ipynb` notebook files via the
stdlib `json` module. It is intentionally narrow and stdlib-only — no
`nbformat` dependency.

Scope:

- single-file notebook analysis via `notebook_analysis(db, path)`
- workspace-root discovery of `*.ipynb` files via `workspace_notebook_analysis(db, root)`
- per-cell extraction of: cell index, `cell_type` (`"code"`/`"markdown"`/`"raw"`/`"unknown"`), the concatenated source text, and (for markdown cells) the first heading line with leading `#` characters stripped
- AST-based extraction of module-level imports and top-level function/class definitions for code cells (each code cell parses as its own module)
- per-cell `syntax-error` diagnostics carrying the offending cell index
- top-level `notebook-decode-error` and `notebook-shape-error` diagnostics for unparseable JSON or non-object cell entries
- kernel name and language extracted from `metadata.kernelspec` (with `metadata.language_info.name` as a fallback for the language)
- cutoff-based backdating that ignores `outputs` and `execution_count`: re-running a notebook that leaves cell sources unchanged backdates analysis nodes and never invalidates downstream consumers

Out of scope for this integration:

- evaluation of cell sources, magic commands, or shell escapes (`!cmd`, `%magic`)
- output rendering, MIME bundle parsing, or attachment extraction
- cross-cell name resolution (`from_import` chasing across cells, or shadowing semantics)
- nbformat schema validation
- reading or writing alternate notebook formats (`.py` percent-format, `.Rmd`, etc.)
- following imports inside code cells to workspace files (notebook cells have no on-disk file to attribute imports to; consumers wishing to chain into `python_source` should write a thin payload-cell-as-source layer themselves)

## Cross-Integration Composition Edges

These are the current concrete composition edges between shipped integrations. Each edge is tracked by the kernel as an ordinary dependency and contributes to incremental re-verification.

- `python_source` → `installed_packages.environment_index` (classifies non-workspace imports as `stdlib` / `installed` / `missing`)
- `python_source` → `deep_module_resolution.resolve_module_location` (populates `resolved_path` for installed imports)
- `dependency_check` → `installed_packages.installed_distributions_index`, `installed_packages.environment_index`
- `dependency_check.workspace_dependency_check` → `python_source.workspace_analysis` (entrypoint-layer composition for undeclared-import detection)
- `dependency_check` → `requirement_evaluation._parse_specifier_set` / `_satisfies` (shared private helpers; documented exception to the "public `@query` only" rule)
- `requirement_evaluation` → `installed_packages.installed_distributions_index`, `requirements_txt` payload helpers
- `deep_module_resolution` → `installed_packages.environment_index`
- `symbol_resolution` → `python_source.{source_text, module_binding_analysis_payload, resolved_imports_for_file, module_wildcard_export_surface, workspace_python_files}`

## Out Of Scope

This contract does not include:

- full `sys.path` / installed-package resolution beyond top-level module classification
- LSP wiring inside `src/pyinc`
- file watchers or schedulers inside `src/pyinc`
- widening the core runtime semantics to accommodate integration convenience
