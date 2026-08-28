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
| Supported shapes | Normalized distribution names and common PEP 440 comparisons, including compatible, wildcard, and arbitrary (`===`) equality forms. |
| Key limits | This is not a resolver: it does not install packages, traverse transitive dependencies, evaluate markers, or compare lock files. Unsupported or unparseable constraints are ambiguous rather than guessed. |

## Nesting caps

`toml_config`, `json_config`, and `xml_config` each cap how deeply a document
may nest, and each names its cap in the diagnostic that rejects it. The cap
bounds the *cache* rather than the parser: every section re-emits its ancestors'
dot path, so cached payloads grow with the square of the nesting depth. Each cap
keeps a document at the cap inside the same ~1 MiB payload budget and inside
what `freeze` will snapshot, so nothing these integrations accept can fail to
cache. None of the three raises `RecursionError`: when the interpreter's stack
is exhausted the diagnostic carries that integration's fixed text. Stack
exhaustion is a property of the caller's remaining stack, not of the file.

## TOML configuration

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect TOML sections and summarize Python project dependencies and tool tables. |
| Entrypoints | `config_analysis`, `workspace_config_analysis` |
| Result types | `ConfigAnalysis`, `ConfigKey`, `ConfigSection` |

**Semantics.** Any single TOML file; workspace discovery of `pyproject.toml`;
nested sections; project dependencies, optional dependency groups, tool names,
parse and project-shape diagnostics. Values are summarized as stable strings,
with date/time values rendered in ISO form.

**Limits.** No build-backend execution, schema validation, dependency
resolution, or file mutation occurs. Table or array nesting deeper than 200
levels is rejected with a `toml-decode-error` diagnostic that names the limit;
depth is measured on the parsed document and counts its implicit top-level
table as the first level, so `[a.b]` is three and `[[a]]` is three as well, an
array wrapping a table. On stack exhaustion that diagnostic carries the fixed
text `TOML parsing exhausted the interpreter stack`.

## JSON configuration

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect keys and nested sections in a JSON object. |
| Entrypoints | `json_analysis`, `workspace_json_analysis` |
| Result types | `JsonAnalysis`, `JsonKey`, `JsonSection` |

**Semantics.** Standard JSON; workspace discovery defaults to `package.json`;
objects become sections and nested subsections; scalar, array, object,
boolean, and null value kinds are reported.

**Limits.** A non-object top level has no sections. JSONC, JSON5, schema
validation, JSON Pointer/Path, and `$ref` resolution are out of scope.
Duplicate keys and non-finite numeric constants are rejected rather than
silently normalized, as is object or array nesting deeper than 200 levels —
the `json-decode-error` diagnostic names that limit, and depth is counted from
the file text before parsing, so the rejection is the same from every call
site. On stack exhaustion that diagnostic carries the fixed text `JSON parsing
exhausted the interpreter stack`.

## Requirements files

| Contract item | Stable surface |
|---|---|
| Purpose | Parse requirements files and optionally follow their requirement-file includes. |
| Entrypoints | `requirements_analysis`, `deep_requirements_analysis`, `workspace_requirements_analysis` |
| Result types | `FileReference`, `IndexDirective`, `RequirementRef`, `RequirementsAnalysis` |

**Semantics.** Names, extras, version text, markers, editable/direct URL
lines, continuations, index/find-links directives, `-r` requirement
references, and `-c` constraint references. Per-requirement options (for
example the `--hash=...` lines `pip-compile --generate-hashes` emits) are
split off the requirement rather than folded into its version text; the
options themselves are ignored, not verified. Deep analysis follows in-root
`-r` files with cycle and missing-file diagnostics.

**Limits.** Marker evaluation is separate. No URL/VCS fetch, version solving,
or recursive constraint application occurs; constraint references are recorded
but not followed. Project-root escapes are diagnosed.

## Requirement evaluation

| Contract item | Stable surface |
|---|---|
| Purpose | Evaluate version specifiers and environment markers, then combine requirements with the installed environment. |
| Entrypoints | `evaluate_version_specifier`, `evaluate_markers`, `applicable_requirements`, `workspace_applicable_requirements` |
| Result types | `ApplicableRequirement`, `ApplicableRequirementsAnalysis`, `MarkerEvaluation`, `PythonEnvironmentSnapshot`, `VersionSpecifierEvaluation` |

**Semantics.** PEP 440 epochs, prerelease/post/dev/local labels, wildcards,
compatible releases, and arbitrary equality (`===`); PEP 508 boolean marker
expressions against the running Python environment. An installed version is
checked with pre-releases allowed, matching dependency checking;
`evaluate_version_specifier` keeps resolver-style pre-release exclusion unless
the specifier opts in. `===` compares the version exactly as written — no
normalization, padding, or case folding — so it is decided without parsing and
is not subject to pre-release exclusion.

**Limits.** Evaluation targets the current process environment only. Extras
are not modeled, noisy or unknown marker variables produce diagnostics, and
this API does not resolve or install dependencies. Unsupported or unparseable
constraints are ambiguous rather than guessed.

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

**Semantics.** Well-formed XML documents; namespace-qualified element and
attribute names are exposed by local name; workspace discovery defaults to
`pom.xml`. Formatting-only changes can backdate parsed results.

**Limits.** Every XML `DOCTYPE` and entity declaration is rejected with an
`xml-parse-error` diagnostic, as is element nesting deeper than 256 levels —
the diagnostic names that limit, and depth counts the document's root element
as the first level. On stack exhaustion it carries the fixed text `XML parsing
exhausted the interpreter stack`. DTD/XSD validation, external entities,
XInclude, streaming APIs, and general XPath are not supported. Dot paths
identify hierarchy but do not index repeated siblings.

## CSV data

| Contract item | Stable surface |
|---|---|
| Purpose | Summarize delimited table structure and inconsistent row widths. |
| Entrypoints | `csv_analysis`, `workspace_csv_analysis` |
| Result types | `CsvAnalysis`, `CsvColumn` |
| Supported shapes | CSV/TSV text handled by the stdlib CSV parser, delimiter/header sniffing, columns, row counts, and inconsistent-column diagnostics. Workspace discovery defaults to `data.csv`. |
| Key limits | The complete file is read and parsed, but delimiter and header sniffing inspect only the first 8192 characters, so a file whose dialect or header shape becomes apparent only later may be misclassified. A sniffed delimiter that is a line terminator is refused and the text is read as comma-delimited instead; text the fallback dialect cannot read either is reported as an empty table. Each step down is recorded as a `csv-dialect-error` diagnostic. There is no schema/type inference, streaming result API, or guarantee for dialects the stdlib sniffer cannot identify. |

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

**Semantics.** Functions, methods, classes, variables, imports/re-exports,
annotations as source text, lexical references, workspace inheritance, and
`self` attributes assigned directly in methods.

Inheritance is flattened depth-first, left-to-right,
nearest-definition-wins: a member name is claimed by the definition at the
shortest inheritance distance from the starting class, ties at equal distance
going to the earlier depth-first left-to-right arrival. A class reached again
at a strictly shallower distance is walked again and its members reclaimed, so
every flattened `ClassMember` — its `defining_path`, `defining_class`,
`range`, `annotation` and `signature`, not only its name — is fixed by the
inheritance graph, its base declaration order, and the depth cap below, never
by the order in which the walk happens to reach a class.

**Limits.** No runtime attribute inference, type evaluation/checking,
decorator semantics, installed-source navigation, or complete Python
method-resolution-order model — the nearest-definition rule above is not C3,
so it can pick a different winner than the interpreter for a name defined at
several points in a diamond. Re-export and inheritance cycles or ambiguous
chains produce conservative results.

Both walks stop at depth 8: re-export following reports an `ambiguous` result
observable through `follow_depth`/`trail`, and base-class following names
every base the cap stopped it from walking in `ClassModel.truncated_bases`,
so members inherited eight or more levels above a class are omitted but never
silently. Both base tuples hold base source text as written at the stopped
edge — an aliased base is reported under its alias — deduplicated in
first-encounter order, and report different facts: `truncated_bases` is a
base that resolved to a workspace class but sat past the cap,
`unresolved_bases` a base that never resolved to a workspace class at all. A
base under neither was followed.

## Notebooks

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect Jupyter notebook metadata and source-bearing cells without executing them. |
| Entrypoints | `notebook_analysis`, `workspace_notebook_analysis` |
| Result types | `NotebookAnalysis`, `NotebookCell`, `NotebookDefinition`, `NotebookDiagnostic`, `NotebookImport` |

**Semantics.** JSON `.ipynb` files; code, markdown, raw, and unknown cells;
markdown headings; top-level imports/definitions and syntax diagnostics per
code cell; kernel/language metadata. Workspace discovery scans `.ipynb` files
directly in the requested root.

A code cell that does not parse as Python is neutralized first: line magics
(`%matplotlib inline`), shell escapes (`!pip install pandas`), help forms
(`?obj`, `obj?`, `obj??`), and capture assignments (`files = !ls`) are
replaced by equal-width Python placeholders, so the rest of the cell is still
analyzed and every reported range still names its real notebook line and
column. A cell magic on the first line claims the whole cell and its body is
dropped, unless the magic runs that body as Python (`%%capture`, `%%debug`,
`%%prun`, `%%python`, `%%python2`, `%%python3`, `%%time`, `%%timeit`).

**Limits.** Workspace discovery is not recursive. Outputs and execution
counts never reach the parsed payloads. Neutralization is lexical, is skipped
for a cell that already parses as Python, and only recognizes those
constructs where IPython does — at the start of a logical line. A neutralized
cell that still does not parse reports `notebook-non-python-cell` instead of
`syntax-error`, so a cell mixing notebook syntax with genuinely broken Python
is reported under that code and not as a plain syntax error. String and
bracket context is tracked so that a magic-shaped line inside a literal or a
bracketed continuation is left alone, but a cell whose own string literals
are unterminated can still be misread, and backslash continuations inside a
magic are not modeled.

Cells are analyzed independently; there is no execution, magic expansion,
cross-cell binding resolution, MIME rendering, attachment extraction, or
nbformat schema dependency. Surrogate scanning covers what reaches the parsed
payloads: cell sources, cell types, and the kernel metadata. Cell outputs and
per-execution metadata never reach the parsed payloads, so they are not
scanned — a notebook whose outputs contain a lone surrogate stays fully
analyzable, and one whose sources do is reported as a decode error rather
than analyzed partially.

## Request scoping

| Contract item | Stable surface |
|---|---|
| Purpose | Let a caller declare a span during which the state the entrypoints read does not change, so repeated entrypoint calls inside it answer from the first one. |
| Entrypoints | `request_scope`, `request_inputs_changed`, `once_per_request` |

**Semantics.** `request_scope(db)` is a context manager bound to one
`Database` and to the calling context. `once_per_request(db, kind, args,
compute)` returns `compute()`, answering from the open scope when the same
`kind` and `args` already ran against that same `Database`.
`request_inputs_changed()` drops what the open scope has memoized, and also
reaches the kernel: when the caller holds a `Database.request_span`, the
declaration rolls that span onto a fresh request, so kernel-level
once-per-request work re-runs against the moved inputs.

**Limits.** The span is the caller's declaration, not a checked fact: a
caller that changes what the integrations read part-way through its own scope
must call `request_inputs_changed()`, and nothing detects the omission. Calls
made with no scope open, or against a `Database` other than the one the scope
was opened for, compute normally. The memo lives only for the span and is
never durable. It answers a repeated question inside one request; it does not
participate in the kernel's invalidation and is not a cache across requests.

`request_inputs_changed()` clears the innermost open scope only, so under
scopes nested for different `Database` objects it forgets nothing an outer
scope memoized: mutate inputs only for the innermost scope's database, or
re-enter the scopes that must forget. `once_per_request` keys its memo on
`kind` and `args`, so `args` must be hashable; unhashable arguments raise
`TypeError`, and only while a scope for that `Database` is open, so the
failure shows up under scoping rather than without it.

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
