# Integration Contract

`pyinc.integrations` contains stdlib-only analyzers built on the kernel. This
document defines the stable, user-facing surface: the names re-exported from
`pyinc.integrations`, the shapes each analyzer accepts, and its deliberate
limits.

All public result records are frozen dataclasses whose collection fields are
tuples. Public source positions use zero-based, end-exclusive `SourceRange`
values. High-level entrypoints decode cached tuple payloads into those records;
the payload queries and decoding helpers in individual modules are not part of
this contract.

## Shared source geometry

| Contract item | Stable surface |
|---|---|
| Purpose | Convert between source offsets and zero-based positions/ranges shared by source, symbol, notebook, requirements, environment, and tooling results. |
| Shared types | `DocumentMap`, `PositionEncoding`, `SourcePosition`, `SourceRange` |
| Supported shapes | Unicode-code-point positions in library APIs; UTF-8, UTF-16, and UTF-32 conversion through `DocumentMap`. |
| Key limits | Ranges are end-exclusive. Protocol-specific encoding conversion belongs at the consumer boundary. |

## Python source

| Contract item | Stable surface |
|---|---|
| Purpose | Discover Python modules and report imports, top-level definitions, exports, resolution status, and dependency surfaces. |
| Entrypoints | `file_analysis`, `directory_analysis`, `module_analysis`, `workspace_analysis` |
| Result types | `DefinitionRef`, `DependencySurface`, `Diagnostic`, `ImportRef`, `PythonFileAnalysis`, `PythonModuleAnalysis`, `PythonWorkspaceAnalysis`, `ResolvedImportRef` |
| Supported shapes | `.py` files under a workspace; absolute and relative imports; static exports; guarded imports used for type checking or import fallbacks; workspace, stdlib, installed, missing, and ambiguous resolution. |
| Key limits | It does not execute imports or infer dynamic exports. Conditional or dynamically constructed bindings are reported conservatively, and ambiguous module names remain ambiguous. |

## Installed packages

| Contract item | Stable surface |
|---|---|
| Purpose | Snapshot installed distributions and classify top-level import names. |
| Entrypoints | `installed_packages_analysis`, `resolve_import_name` |
| Result types | `ImportNameResolution`, `InstalledPackageRef`, `InstalledPackagesAnalysis` |
| Supported shapes | `.dist-info` metadata, `top_level.txt`, distribution name fallback, `Requires-Dist`, and the running interpreter's stdlib module names. |
| Key limits | Legacy egg formats, package installation, marker evaluation, and import-loader execution are out of scope. Namespace layout is handled by deep module resolution, not distribution metadata. |

## Deep module resolution

| Contract item | Stable surface |
|---|---|
| Purpose | Resolve a dotted import name to a regular module, regular package, namespace package, stdlib classification, or missing result. |
| Entrypoints | `deep_module_resolution_analysis`, `resolve_module_path` |
| Result types | `DeepModuleResolutionAnalysis`, `ModulePathEntry`, `NamespacePackage`, `PthDirective`, `ResolvedModuleLocation` |
| Supported shapes | Existing directory entries from the live `sys.path`; direct `.pth` files in those entries; simple path lines; `.py` modules; packages with `__init__.py`; and PEP 420 namespace directories. |
| Key limits | The live mutable `sys.path` is declared untracked and scanned again rather than treated as durable state. Empty, non-string, missing, and duplicate entries are ignored. `.pth` import lines are recorded and diagnosed but never executed. Zip imports, extension modules, legacy eggs, editable-install pointer formats, path hooks, and meta-path finders are not resolved. |

## Dependency checking

| Contract item | Stable surface |
|---|---|
| Purpose | Compare declared requirements with installed versions and optionally identify undeclared imports in a workspace. |
| Entrypoints | `dependency_check_analysis`, `workspace_dependency_check` |
| Result types | `DependencyCheckAnalysis`, `DependencyStatus`, `UndeclaredImport` |
| Supported shapes | Normalized distribution names and common PEP 440 comparisons, including compatible and wildcard equality forms. |
| Key limits | This is not a resolver: it does not install packages, traverse transitive dependencies, evaluate markers, or compare lock files. Unsupported or unparseable constraints are ambiguous rather than guessed. |

## TOML configuration

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect TOML sections and summarize Python project dependencies and tool tables. |
| Entrypoints | `config_analysis`, `workspace_config_analysis` |
| Result types | `ConfigAnalysis`, `ConfigKey`, `ConfigSection` |
| Supported shapes | Any single TOML file; workspace discovery of `pyproject.toml`; nested sections; project dependencies, optional dependency groups, tool names, parse and project-shape diagnostics. |
| Key limits | Values are summarized as stable strings, with date/time values rendered in ISO form. No build-backend execution, schema validation, dependency resolution, or file mutation occurs. |

## JSON configuration

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect keys and nested sections in a JSON object. |
| Entrypoints | `json_analysis`, `workspace_json_analysis` |
| Result types | `JsonAnalysis`, `JsonKey`, `JsonSection` |
| Supported shapes | Standard JSON; workspace discovery defaults to `package.json`; objects become sections and nested subsections; scalar, array, object, boolean, and null value kinds are reported. |
| Key limits | A non-object top level has no sections. JSONC, JSON5, schema validation, JSON Pointer/Path, and `$ref` resolution are out of scope. Duplicate keys and non-finite numeric constants are rejected rather than silently normalized. |

## Requirements files

| Contract item | Stable surface |
|---|---|
| Purpose | Parse requirements files and optionally follow their requirement-file includes. |
| Entrypoints | `requirements_analysis`, `deep_requirements_analysis`, `workspace_requirements_analysis` |
| Result types | `FileReference`, `IndexDirective`, `RequirementRef`, `RequirementsAnalysis` |
| Supported shapes | Names, extras, version text, markers, editable/direct URL lines, continuations, index/find-links directives, `-r` requirement references, and `-c` constraint references. Deep analysis follows in-root `-r` files with cycle and missing-file diagnostics. |
| Key limits | Marker evaluation is separate. No URL/VCS fetch, version solving, or recursive constraint application occurs; constraint references are recorded but not followed. Project-root escapes are diagnosed. |

## Requirement evaluation

| Contract item | Stable surface |
|---|---|
| Purpose | Evaluate version specifiers and environment markers, then combine requirements with the installed environment. |
| Entrypoints | `evaluate_version_specifier`, `evaluate_markers`, `applicable_requirements`, `workspace_applicable_requirements` |
| Result types | `ApplicableRequirement`, `ApplicableRequirementsAnalysis`, `MarkerEvaluation`, `PythonEnvironmentSnapshot`, `VersionSpecifierEvaluation` |
| Supported shapes | PEP 440 epochs, prerelease/post/dev/local labels, wildcards and compatible releases; PEP 508 boolean marker expressions against the running Python environment. |
| Key limits | Evaluation targets the current process environment only. Extras are not modeled, noisy or unknown marker variables produce diagnostics, and this API does not resolve or install dependencies. |

## Environment files

| Contract item | Stable surface |
|---|---|
| Purpose | Parse `.env`-style assignments without applying them to the process environment. |
| Entrypoints | `env_analysis`, `workspace_env_analysis` |
| Result types | `EnvEntry`, `EnvFileAnalysis` |
| Supported shapes | Single-line `KEY=VALUE`, optional `export`, quoted and unquoted values, comments, and braced `${NAME}` interpolation detection. Workspace discovery defaults to `.env`. |
| Key limits | Interpolation is diagnosed but not evaluated. Bare `$NAME`, command substitution, multiline dotenv variants, shell execution, and writes are out of scope. |

## XML configuration

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect XML elements, attributes, text, child tags, and dot-separated element paths. |
| Entrypoints | `xml_analysis`, `workspace_xml_analysis` |
| Result types | `XmlAnalysis`, `XmlAttribute`, `XmlElement` |
| Supported shapes | Well-formed XML documents; namespace-qualified element and attribute names are exposed by local name; workspace discovery defaults to `pom.xml`. Formatting-only changes can backdate parsed results. |
| Key limits | Every XML `DOCTYPE` and entity declaration is rejected with an `xml-parse-error` diagnostic. DTD/XSD validation, external entities, XInclude, streaming APIs, and general XPath are not supported. Dot paths identify hierarchy but do not index repeated siblings. |

## CSV data

| Contract item | Stable surface |
|---|---|
| Purpose | Summarize delimited table structure and inconsistent row widths. |
| Entrypoints | `csv_analysis`, `workspace_csv_analysis` |
| Result types | `CsvAnalysis`, `CsvColumn` |
| Supported shapes | CSV/TSV text handled by the stdlib CSV parser, delimiter/header sniffing, columns, row counts, and inconsistent-column diagnostics. Workspace discovery defaults to `data.csv`. |
| Key limits | The complete file is read. There is no schema/type inference, streaming result API, or guarantee for dialects the stdlib sniffer cannot identify. |

## Lexical scope

| Contract item | Stable surface |
|---|---|
| Purpose | Represent lexical scopes and resolve a source position to a stable workspace symbol identity. |
| Entrypoints | `scope_tree`, `symbol_at` |
| Result types | `Binding`, `Scope`, `ScopeTree`, `SymbolId` |
| Supported shapes | Module, class, function, lambda, and comprehension scopes; parameters and ordinary Python binding forms; `global`, `nonlocal`, and assignment-expression behavior. |
| Key limits | Resolution is static and conservative. A position that is ambiguous, dynamic, or outside a resolvable workspace binding returns no symbol instead of a speculative target. |

## Symbol resolution

| Contract item | Stable surface |
|---|---|
| Purpose | Build module/workspace symbol indexes, follow static re-exports, find identity-based references, and model workspace classes. |
| Entrypoints | `module_symbol_table`, `workspace_symbol_index`, `find_references`, `class_model` |
| Result types | `ClassMember`, `ClassModel`, `ModuleSymbolTable`, `Parameter`, `Reference`, `ReferenceQueryResult`, `Signature`, `Symbol`, `WorkspaceSymbolEntry`, `WorkspaceSymbolIndex` |
| Supported shapes | Functions, methods, classes, variables, imports/re-exports, annotations as source text, lexical references, workspace inheritance, and `self` attributes assigned directly in methods. |
| Key limits | No runtime attribute inference, type evaluation/checking, decorator semantics, installed-source navigation, or complete Python method-resolution-order model. Re-export and inheritance cycles or ambiguous chains produce conservative results. |

## Notebooks

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect Jupyter notebook metadata and source-bearing cells without executing them. |
| Entrypoints | `notebook_analysis`, `workspace_notebook_analysis` |
| Result types | `NotebookAnalysis`, `NotebookCell`, `NotebookDefinition`, `NotebookDiagnostic`, `NotebookImport` |
| Supported shapes | JSON `.ipynb` files; code, markdown, raw, and unknown cells; markdown headings; top-level imports/definitions and syntax diagnostics per code cell; kernel/language metadata. Workspace discovery scans `.ipynb` files directly in the requested root. |
| Key limits | Workspace discovery is not recursive. Outputs and execution counts are ignored for cutoff purposes. Cells are analyzed independently; there is no execution, magic/shell handling, cross-cell binding resolution, MIME rendering, attachment extraction, or nbformat schema dependency. |

## Composition and experimental helpers

Integrations call one another at the cached query layer where composition is
needed. Python import analysis uses installed-package and deep-module results;
dependency checking combines installed metadata with workspace imports;
requirement evaluation combines parsed requirements, environment markers, and
installed versions; scope and symbol analysis build on shared Python source.
Those calls become ordinary dependency edges and require no user wiring.

Individual integration modules also expose payload queries and helper names for
in-repository composition. They are intentionally absent from
`pyinc.integrations.__all__` and are not covered by this stable contract. Import
only the names listed in the `Entrypoints`, `Result types`, and `Shared types`
rows above when relying on semver compatibility.

LSP protocol behavior, filesystem watchers, scheduling, and code generation
belong to consumer packages and do not widen this integration surface.
