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
  - composition query: `requirements_payload(db, path)` with
    `RequirementPayload`; stable in the `requirements_txt` submodule for other
    integrations, not re-exported at the package level
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
- shared source geometry
  - types: `SourcePosition`, `SourceRange`, `PositionEncoding`, `DocumentMap`
  - `DocumentMap` converts Python AST UTF-8 byte columns to public Unicode
    code-point positions and converts public positions to and from negotiated
    UTF-8, UTF-16, or UTF-32 protocol coordinates
- `symbol_resolution`
  - result types: `SymbolId`, `Scope`, `Binding`, `ScopeTree`, `Parameter`,
    `Signature`, `Symbol`,
    `ModuleSymbolTable`, `WorkspaceSymbolEntry`,
    `WorkspaceSymbolIndex`, `Reference`, `ReferenceQueryResult`, `ClassMember`,
    `ClassModel`
  - entrypoints: `scope_tree(db, path)`,
    `symbol_at(db, root, path, position)`,
    `module_symbol_table(db, root, path)`,
    `workspace_symbol_index(db, root)`,
    `find_references(db, root, symbol_id, *, include_declaration=True)`,
    `class_model(db, root, path, qualified_name)`
- `notebook` *(added in the v2 development cycle; resolves the v1 architectural non-goal "notebook integration")*
  - result types: `NotebookImport`, `NotebookDefinition`, `NotebookCell`,
    `NotebookDiagnostic`, `NotebookAnalysis`
  - entrypoints: `notebook_analysis(db, path)`,
    `workspace_notebook_analysis(db, root)`

## Experimental Helpers

Low-level query nodes, payload helpers, decode helpers, and module-local resource helpers in
the integration submodules are retained for debugging and targeted tests. Examples include
`source_text`, `imports_for_file`, `config_file_text`, `json_file_text`, and
most `*_payload` helpers. A payload query used for documented cross-integration
composition, such as `requirements_payload`, is listed in its defining
module's `__all__` but is not re-exported from `pyinc.integrations`.

Those names remain importable from their defining submodules, but they are experimental:

- they are not re-exported from `pyinc.integrations`
- they do not carry the same compatibility promise as the stable dataclass views and entrypoints
- new integrations should not depend on them as a public contract

## Python Source Integration Scope

`python_source` is intentionally narrow:

- workspace-local module discovery rooted at the supplied directory
- traversal is cycle-safe and constrained to real paths under the supplied root
- top-level imports, top-level definitions, and simple top-level assignments for export-surface tracking; imports inside `if TYPE_CHECKING:` and `try: … except ImportError/ModuleNotFoundError:` guard blocks at module top level are also collected
- syntax diagnostics only
- dependency invalidation based on resolved module export surfaces, including conservative static support for `from x import *`
- import resolution with `workspace`, `stdlib`, `installed`, `missing`, and `ambiguous` outcomes
- environment-aware resolution via composition with `installed_packages` (stdlib and installed package classification)
- Python source is decoded using BOM/PEP 263 rules
- public source coordinates use `SourcePosition` and `SourceRange`: zero-based,
  end-exclusive, Unicode-code-point columns. AST UTF-8 byte offsets are
  converted at the parser boundary.

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
- workspace-root discovery of `requirements.txt` via
  `workspace_requirements_analysis(db, root)`, including recursive `-r` files
  through `deep_requirements_analysis`
- PEP 508 specifier extraction: package name, extras, version constraints, environment markers
- package name normalization per PEP 503 (lowercase, hyphens/dots to underscores)
- file references (`-r`/`--requirement`, `-c`/`--constraint`)
- index directives (`--index-url`, `--extra-index-url`, `--find-links`)
- editable install detection (`-e`/`--editable`)
- URL-based requirements (`name @ url`)
- line continuation support (backslash-newline)
- diagnostics for unparseable lines
- `RequirementRef`, `FileReference`, and `IndexDirective` carry zero-based,
  end-exclusive `SourceRange` values for their logical source lines
- recursive `-r` traversal uses canonical paths, reports cycles, missing files,
  and project-root escapes, and merges included requirements before the
  including file so the including file wins duplicate names
- cutoff-based backdating (comment-only and whitespace-only edits are backdated)

Out of scope for this integration:

- marker expression evaluation
- version specifier satisfaction or resolution
- URL fetching or VCS cloning
- recursive `-c` constraint inclusion (constraint references are recorded but
  not followed)
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
- a tracked environment resource for `sys.path` and site-packages discovery
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
- PEP 440 version matching for `==`, `!=`, `>=`, `<=`, `>`, `<`, and `~=`,
  including epochs, pre/post/dev/local releases, compatible releases, and
  wildcard equality; unparseable specifiers and arbitrary equality (`===`)
  return `ambiguous`
- PEP 503 distribution name normalization
- result types: `DependencyStatus`, `UndeclaredImport`, `DependencyCheckAnalysis`

Cross-integration composition:

- imports `installed_distributions_index` from `installed_packages` at the query layer
  (creates an incremental dependency edge — package installs trigger revalidation)
- imports `environment_index` from `installed_packages` at the query layer
- composes with `python_source.workspace_analysis` at the entrypoint layer (not the
  query layer, since `workspace_analysis` is a non-query function)

Out of scope for this integration:

- PEP 440 forms outside the documented, differentially tested subset
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
- each `EnvEntry` carries the exact zero-based, end-exclusive `SourceRange` of
  its assignment line, measured in Unicode code points
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
- `requirement_evaluation` and `dependency_check` share the pure PEP 440 parser
  and satisfaction primitives through the dedicated internal `_pep440` module;
  neither imports private helpers from the other integration

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

The `symbol_resolution` integration exposes workspace-wide symbol tables,
lexical identities, and cross-module re-export resolution, intentionally
conservative and stdlib-only:

Scope:

- module-level and class-level symbol extraction: functions, methods, classes, class variables, top-level variables, import aliases, from-import aliases, wildcard-import stubs
- arbitrary class nesting depth; qualified name scheme is `foo` / `Foo.bar` / `Foo.Inner.bar`
- cross-module resolution that follows re-export chains (`from a import x` → `a.py: from b import x` → ...) bounded by `MAX_FOLLOW_DEPTH = 8` with cycle detection
- type-annotation *text* extraction via `ast.unparse` (no type evaluation, no type checking)
- installed-package lookups stop at the import boundary (`resolution="installed"`, `distribution_name`, `distribution_version`, `defining_path=None`)
- dynamic `__all__` on the wildcard-provider side → `"ambiguous"` with `db.report_untracked_read`
- `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` guard blocks at module top level: imports inside are collected as regular symbols; no impurity marker is set
- `try: … except ImportError:` / `except ModuleNotFoundError:` / `except (ImportError, ModuleNotFoundError):` guard blocks at module top level: imports inside are collected as regular symbols; no impurity marker is set
- other conditional top-level bindings (`if sys.version_info >= …`, top-level `For`, `While`, `Try` without recognised handler, `With`) → `impurity_reasons` includes `"conditional top-level binding"` and the binding produces no symbol
- one shared lexical graph for module, class, function, lambda, and
  comprehension scopes, including parameters, assignment/import/loop/with/
  exception/pattern targets, `global`, `nonlocal`, and walrus binding rules
- `scope_tree(db, path)` returns `ScopeTree` with stable `Scope`, `Binding`, and
  `SymbolId` values; `symbol_at` resolves a source position rather than a bare
  cursor name
- `find_references(db, root, symbol_id)` compares resolved identities. Local
  shadowing and comprehension bindings cannot leak into module references.
- every public source-bearing record exposes an end-exclusive, zero-based
  `range: SourceRange`; a diagnostic or resolution without a source site uses
  `range=None`. There are no one-based line or AST-byte-column aliases.
- attributes resolve only when every step is proven to be a workspace module,
  class, `self`/`cls`, or a directly annotated value; other chains return no
  target
- per-class member model via `class_model(db, root, path, qualified_name)` → `ClassModel(path, qualified_name, members, unresolved_bases)`, each `ClassMember` a `method` / `class_variable` / `instance_variable` carrying `range`, `defining_path`, and `defining_class`. Own members are: annotated class-body variables, assigned class-body variables, methods, then `self.NAME` instance attributes collected from every direct method whose first parameter is literally `self` (the earliest source range wins; descent stops at nested `def`/`class`/`lambda` scopes; `AugAssign` does not declare). Bases are encoded once (`("name", X)` / `("attr", L, A)` / `("text", raw)`, a single `Subscript` layer unwrapped so `Base[T]` follows `Base`)
- inheritance flattening in `class_model`: base classes that resolve to a workspace `class` are followed **depth-first, left-to-right, first-definition-wins** (a derived member shadows a base member of the same name — a single entry at the derived site), bounded by `MAX_BASE_DEPTH = 8` with a `(path, class_qname)` visited-set cycle guard, and base files queried one at a time via `class_models_for_file` (per-file invalidation). Bases that do not land on a workspace class (stdlib / installed / missing / ambiguous / `("text", …)`) contribute no members and are reported in `unresolved_bases`
- entrypoints: `scope_tree`, `symbol_at`, `module_symbol_table`,
  `workspace_symbol_index`, `find_references`, `class_model`
- result types include `SymbolId`, `Scope`, `Binding`, `ScopeTree`, `Reference`,
  and the declaration-oriented symbol and class-model views; every
  source-bearing record uses the shared `SourcePosition` / `SourceRange`
  geometry contract

Out of scope for this integration:

- speculative runtime attribute or type inference
- decorator-induced rebinding (`@functools.cache`, `@property`, `@classmethod`, etc.)
- full C3 MRO linearization (class models flatten depth-first, left-to-right, first-definition-wins; in a shared-grandparent diamond — `A(B, C)`, `B(D)`, `C(D)` — a member defined in both `C` and `D` resolves to `D`'s definition, reached depth-first through `B`, whereas C3 would pick `C`'s)
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
- AST-based extraction of module-level imports and top-level function/class
  definitions for code cells (each code cell parses as its own module), with a
  zero-based, end-exclusive `SourceRange` on each extracted record
- per-cell `syntax-error` diagnostics carrying the offending cell index and
  source range
- top-level `notebook-decode-error` and `notebook-shape-error` diagnostics for
  unparseable JSON or non-object cell entries; diagnostics without a cell source
  site use `range=None`
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
- `requirement_evaluation` → `installed_packages.installed_distributions_index`, `requirements_txt` payload helpers
- `deep_module_resolution` → `installed_packages.environment_index`
- `symbol_resolution` → `python_source.{source_text, module_binding_analysis_payload, resolved_imports_for_file, module_wildcard_export_surface, workspace_python_files}`

## Out Of Scope

This contract does not include:

- full `sys.path` / installed-package resolution beyond top-level module classification
- LSP wiring inside `src/pyinc`
- file watchers or schedulers inside `src/pyinc`
- widening the core runtime semantics to accommodate integration convenience
