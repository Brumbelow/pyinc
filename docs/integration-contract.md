# Integration Contract

`pyinc.integrations` contains stdlib-only analyzers built on the kernel. This
document defines the stable, user-facing surface. It lists the names
re-exported from `pyinc.integrations`, the shapes each analyzer accepts, and
the limits each one has by design.

All public result records are frozen dataclasses whose collection fields are
tuples. Public source positions use zero-based, end-exclusive `SourceRange`
values. High-level entrypoints decode cached tuple payloads into those records.
The payload queries and decoding helpers in individual modules are outside
this contract.

Call a high-level entrypoint from outside a query. A query body that reaches
one is refused, as the composition section below sets out. Several threads may
call the entrypoints at once, on one `Database` or on several. The memo of
decoded records they share is locked. If a process forks while another thread
holds that lock, the child gets a new lock.

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
| Key limits | It does not execute imports or infer dynamic exports. Conditional or dynamically constructed bindings are reported conservatively, and ambiguous module names stay ambiguous. |

## Installed packages

| Contract item | Stable surface |
|---|---|
| Purpose | Snapshot installed distributions and classify top-level import names. |
| Entrypoints | `installed_packages_analysis`, `resolve_import_name` |
| Result types | `ImportNameResolution`, `InstalledPackageRef`, `InstalledPackagesAnalysis` |
| Supported shapes | `.dist-info` metadata, `top_level.txt`, distribution name fallback, `Requires-Dist`, and the running interpreter's stdlib module names. |
| Key limits | Legacy egg formats, package installation, marker evaluation, and import-loader execution are out of scope. Deep module resolution handles namespace layout, and distribution metadata plays no part in it. |

## Deep module resolution

| Contract item | Stable surface |
|---|---|
| Purpose | Resolve a dotted import name to a regular module, regular package, namespace package, stdlib classification, or missing result. |
| Entrypoints | `deep_module_resolution_analysis`, `resolve_module_path` |
| Result types | `DeepModuleResolutionAnalysis`, `ModulePathEntry`, `NamespacePackage`, `PthDirective`, `ResolvedModuleLocation` |
| Supported shapes | Existing directory entries from the live `sys.path`; direct `.pth` files in those entries; simple path lines; `.py` modules; packages with `__init__.py`; and PEP 420 namespace directories. |
| Key limits | The live mutable `sys.path` is declared untracked, so it is scanned again on every request and never kept as durable state. Empty, non-string, missing, duplicate, relative, and (on Windows) rooted driveless entries are ignored. `.pth` import lines are recorded and diagnosed but never executed. Zip imports, extension modules, legacy eggs, editable-install pointer formats, path hooks, and meta-path finders stay unresolved. |

## Dependency checking

| Contract item | Stable surface |
|---|---|
| Purpose | Compare declared requirements with installed versions and optionally identify undeclared imports in a workspace. |
| Entrypoints | `dependency_check_analysis`, `workspace_dependency_check` |
| Result types | `DependencyCheckAnalysis`, `DependencyStatus`, `UndeclaredImport` |
| Supported shapes | Normalized distribution names and common PEP 440 comparisons, including compatible, wildcard, and arbitrary (`===`) equality forms. |
| Key limits | Dependency resolution, package installation, transitive-dependency traversal, marker evaluation, and lock-file comparison are out of scope. Unsupported or unparseable constraints are reported as ambiguous and never guessed. |

## Nesting caps

`toml_config`, `json_config`, and `xml_config` each cap how deeply a document
may nest. Each names its cap in the diagnostic that rejects the document.

The cap bounds the depth-driven growth of the *cache*. The parser's own stack
is a different limit, which the last paragraph of this section covers. Every
section re-emits its ancestors' dot path, so cached payloads grow with the square of the
nesting depth. At the cap, with the key lengths each integration's own cap
rationale measures, a document stays inside the same ~1 MiB payload budget
and inside what `freeze` will snapshot.

The cap bounds depth only. Payload size also scales with key length and with
document width. A wide document at shallow depth and a document at the cap
with long keys are both accepted, and either can cache more than that budget.

When the interpreter's stack runs out, each of the three reports a diagnostic
with its own fixed text and raises no `RecursionError`. Stack exhaustion is a
property of the caller's remaining stack and is no fault of the file.

## TOML configuration

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect TOML sections and summarize Python project dependencies and tool tables. |
| Entrypoints | `config_analysis`, `workspace_config_analysis` |
| Result types | `ConfigAnalysis`, `ConfigKey`, `ConfigSection` |

### Semantics

It reads any single TOML file, and workspace discovery finds
`pyproject.toml`. It reports nested sections, project dependencies, optional
dependency groups, tool names, and parse and project-shape diagnostics. Values
are summarized as stable strings, with date/time values rendered in ISO form.

### Limits

It never executes a build backend, validates against a schema, resolves
dependencies, or mutates files. Table or array nesting deeper than 200 levels
is rejected with a `toml-decode-error` diagnostic that names the limit. Depth
is measured on the parsed document, and its implicit top-level table counts
as the first level. So `[a.b]` is three levels deep, and so is `[[a]]` (an
array wrapping a table). On stack exhaustion that diagnostic carries the
fixed text `TOML parsing exhausted the interpreter stack`.

## JSON configuration

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect keys and nested sections in a JSON object. |
| Entrypoints | `json_analysis`, `workspace_json_analysis` |
| Result types | `JsonAnalysis`, `JsonKey`, `JsonSection` |

### Semantics

It reads standard JSON, and workspace discovery defaults to `package.json`.
Objects become sections and nested subsections. It reports scalar, array,
object, boolean, and null value kinds.

### Limits

A non-object top level has no sections. JSONC, JSON5, schema validation, JSON
Pointer/Path, and `$ref` resolution are out of scope. Duplicate keys and
non-finite numeric constants are rejected and never silently normalized.
Object or array nesting deeper than 200 levels is rejected too, with a
`json-decode-error` diagnostic that names the limit. Depth is counted from the
file text before parsing, so the rejection is the same from every call site. On stack
exhaustion that diagnostic carries the fixed text
`JSON parsing exhausted the interpreter stack`.

## Requirements files

| Contract item | Stable surface |
|---|---|
| Purpose | Parse requirements files and optionally follow their requirement-file includes. |
| Entrypoints | `requirements_analysis`, `deep_requirements_analysis`, `workspace_requirements_analysis` |
| Result types | `FileReference`, `IndexDirective`, `RequirementRef`, `RequirementsAnalysis` |

### Semantics

It parses names, extras, version text, markers, editable/direct URL lines,
continuations, index/find-links directives, `-r` requirement references, and
`-c` constraint references. Per-requirement options (for example the
`--hash=...` lines `pip-compile --generate-hashes` emits) are split off the
requirement and kept out of its version text. The options themselves are
ignored and never verified. Deep analysis follows in-root `-r` files, with
cycle and missing-file diagnostics.

### Limits

Marker evaluation is separate. It never fetches URLs or VCS sources, solves
versions, or applies constraints recursively. Constraint references are
recorded but not followed. Project-root escapes are diagnosed.

## Requirement evaluation

| Contract item | Stable surface |
|---|---|
| Purpose | Evaluate version specifiers and environment markers, then combine requirements with the installed environment. |
| Entrypoints | `evaluate_version_specifier`, `evaluate_markers`, `applicable_requirements`, `workspace_applicable_requirements` |
| Result types | `ApplicableRequirement`, `ApplicableRequirementsAnalysis`, `MarkerEvaluation`, `PythonEnvironmentSnapshot`, `VersionSpecifierEvaluation` |

### Semantics

It supports PEP 440 epochs, prerelease/post/dev/local labels, wildcards,
compatible releases, and arbitrary equality (`===`). It evaluates PEP 508
boolean marker expressions against the running Python environment.

An installed version is checked with pre-releases allowed, matching
dependency checking. `evaluate_version_specifier` keeps resolver-style
pre-release exclusion unless the specifier opts in. `===` compares the version
as written, with no normalization, padding, or case folding. So it is decided
without parsing, and pre-release exclusion does not apply to it.

### Limits

Evaluation targets the current process environment only. Extras are not
modeled. Noisy or unknown marker variables produce diagnostics. This API never
resolves or installs dependencies. Unsupported or unparseable constraints are
reported as ambiguous and never guessed.

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

### Semantics

It reads well-formed XML documents. Namespace-qualified element and attribute
names are exposed by local name. Workspace discovery defaults to `pom.xml`.
Formatting-only changes can backdate parsed results.

### Limits

Every XML `DOCTYPE` and entity declaration is rejected with an
`xml-parse-error` diagnostic. Element nesting deeper than 256 levels is
rejected the same way, and the diagnostic names that limit. Depth counts the
document's root element as the first level. On stack exhaustion the diagnostic
carries the fixed text `XML parsing exhausted the interpreter stack`. DTD/XSD validation, external
entities, XInclude, streaming APIs, and general XPath are unsupported. Dot
paths identify hierarchy but do not index repeated siblings.

## CSV data

| Contract item | Stable surface |
|---|---|
| Purpose | Summarize delimited table structure and inconsistent row widths. |
| Entrypoints | `csv_analysis`, `workspace_csv_analysis` |
| Result types | `CsvAnalysis`, `CsvColumn` |
| Supported shapes | CSV/TSV text handled by the stdlib CSV parser, delimiter/header sniffing, columns, row counts, and inconsistent-column diagnostics. Workspace discovery defaults to `data.csv`. |
| Key limits | The complete file is read and parsed. Delimiter and header sniffing inspect only the first 8192 characters, so a file whose dialect or header shape shows only later may be misclassified. Line endings are translated before the dialect is sniffed. A sniffed delimiter that is a line terminator or the quote character is refused, and the text is read as comma-delimited. Text the fallback dialect also fails to read is reported as an empty table. Each step down is recorded as a `csv-dialect-error` diagnostic. Schema/type inference and a streaming result API are out of scope, and dialects the stdlib sniffer cannot identify carry no guarantee. |

## Lexical scope

| Contract item | Stable surface |
|---|---|
| Purpose | Represent lexical scopes and resolve a source position to a stable workspace symbol identity. |
| Entrypoints | `scope_tree`, `symbol_at` |
| Result types | `Binding`, `Scope`, `ScopeTree`, `SymbolId` |
| Supported shapes | Module, class, function, lambda, and comprehension scopes; parameters and ordinary Python binding forms; `global`, `nonlocal`, and assignment-expression behavior. |
| Key limits | Resolution is static and conservative. A position that is ambiguous, dynamic, or outside a resolvable workspace binding returns no symbol and never a speculative target. |

## Symbol resolution

| Contract item | Stable surface |
|---|---|
| Purpose | Build module/workspace symbol indexes, follow static re-exports, find identity-based references, and model workspace classes. |
| Entrypoints | `module_symbol_table`, `workspace_symbol_index`, `find_references`, `class_model` |
| Result types | `ClassMember`, `ClassModel`, `ModuleSymbolTable`, `Parameter`, `Reference`, `ReferenceQueryResult`, `Signature`, `Symbol`, `WorkspaceSymbolEntry`, `WorkspaceSymbolIndex` |

### Semantics

It covers functions, methods, classes, variables, imports/re-exports,
annotations as source text, lexical references, workspace inheritance, and
`self` attributes assigned directly in methods.

Inheritance is flattened depth-first, left-to-right, nearest-definition-wins.
A member name is claimed by the definition at the shortest inheritance
distance from the starting class. A tie at equal distance goes to the earlier
arrival in depth-first, left-to-right order. A class reached again at a
strictly shallower distance is walked again, and its members are reclaimed.
So every flattened `ClassMember` is fixed by the inheritance graph, its base
declaration order, and the depth cap below. That holds for its
`defining_path`, `defining_class`, `range`, `annotation` and `signature` as
well as its name. The order in which the walk happens to reach a class plays
no part.

### Limits

Runtime attribute inference, type evaluation/checking, decorator semantics,
installed-source navigation, and a complete Python method-resolution-order
model are out of scope. The nearest-definition rule above is not C3. For a
name defined at several points in a diamond, it can pick a different winner
than the interpreter. Re-export and inheritance cycles or ambiguous chains
produce conservative results.

Both walks stop at depth 8. Re-export following reports an `ambiguous`
result, observable through `follow_depth`/`trail`. Base-class following lists
every base the cap stopped it from walking in `ClassModel.truncated_bases`.
Members inherited eight or more levels above a class are omitted, and that
list records the omission.

Both base tuples hold base source text as written at the stopped edge, so an
aliased base is reported under its alias. Both are deduplicated in
first-encounter order, and they report different facts. `truncated_bases`
holds a base that resolved to a workspace class but sat past the cap.
`unresolved_bases` holds a base that never resolved to a workspace class at
all. A base in neither tuple was followed.

## Notebooks

| Contract item | Stable surface |
|---|---|
| Purpose | Inspect Jupyter notebook metadata and source-bearing cells without executing them. |
| Entrypoints | `notebook_analysis`, `workspace_notebook_analysis` |
| Result types | `NotebookAnalysis`, `NotebookCell`, `NotebookDefinition`, `NotebookDiagnostic`, `NotebookImport` |

### Semantics

It reads JSON `.ipynb` files with code, markdown, raw, and unknown cells. It
reports markdown headings, kernel/language metadata, and the top-level
imports/definitions and syntax diagnostics of each code cell. Workspace
discovery scans `.ipynb` files directly in the requested root.

A code cell that fails to parse as Python is neutralized first. Line magics
(`%matplotlib inline`), shell escapes (`!pip install pandas`), help forms
(`?obj`, `obj?`, `obj??`), and capture assignments (`files = !ls`) are
replaced by equal-width Python placeholders. The rest of the cell is still
analyzed, and every reported range still names its real notebook line and
column. A cell magic on the first line claims the whole cell, and its body is
dropped. The exception is a magic that runs that body as Python (`%%capture`,
`%%debug`, `%%prun`, `%%python`, `%%python2`, `%%python3`, `%%time`,
`%%timeit`).

### Limits

Workspace discovery is not recursive. Outputs and execution counts never
reach the parsed payloads. Neutralization is lexical and is skipped for a
cell that already parses as Python. It recognizes those constructs only where
IPython does, at the start of a logical line. A neutralized cell that still
fails to parse reports `notebook-non-python-cell` instead of `syntax-error`.
So a cell that mixes notebook syntax with broken Python is reported under that
code, and never as a plain syntax error. String and bracket context is
tracked, so a magic-shaped line inside a literal or a bracketed continuation
is left alone. A cell whose own string literals are unterminated can still be
misread, and backslash continuations inside a magic are not modeled.

Cells are analyzed independently and never executed. Magic expansion,
cross-cell binding resolution, MIME rendering, and attachment extraction are
out of scope, and the analyzer has no nbformat schema dependency. Surrogate
scanning covers what reaches the parsed payloads: cell sources, cell types,
and the kernel metadata. Cell outputs and per-execution metadata never reach
the parsed payloads, so they are not scanned. A notebook whose outputs
contain a lone surrogate stays fully analyzable. A notebook whose sources
contain one is reported as a decode error and gets no partial analysis.

## Request scoping

| Contract item | Stable surface |
|---|---|
| Purpose | Let a caller declare a span during which the state the entrypoints read stays fixed, so repeated entrypoint calls inside it answer from the first one. |
| Entrypoints | `request_scope`, `request_inputs_changed`, `once_per_request` |

### Semantics

`request_scope(db)` is a context manager bound to one `Database`, to the
calling context, and to the thread that opens it. Every thread started on a
free-threaded 3.14 build copies the context, and so does `asyncio.to_thread`.
A copy made while the scope is open sees no scope from another thread. That
includes a later thread given the ident of an opener that has exited. Once
the scope has closed, the copy sees none at all. A closed scope releases its
`Database` and its memo, so a copied context keeps neither alive.

`once_per_request(db, kind, args, compute)` returns `compute()`. It answers
from the open scope when the same `kind` and `args` already ran against that
same `Database`.

`request_inputs_changed()` drops what the open scope has memoized. It also
reaches the kernel. When the caller holds a `Database.request_span`, the
declaration rolls that span onto a fresh request, so kernel-level
once-per-request work re-runs against the moved inputs.

### Limits

The span is the caller's declaration, and nothing checks it. A caller that
changes what the integrations read part-way through its own scope must call
`request_inputs_changed()`. Nothing detects a missing call. Calls made with no
scope open, or against a `Database` other than the one the scope was opened
for, compute normally. The memo lives only for the span and is never durable.
It answers a repeated question inside one request. It stays outside the
kernel's invalidation and is not a cache across requests.

`request_inputs_changed()` clears the innermost open scope only. Under scopes
nested for different `Database` objects, an outer scope keeps everything it
memoized. Mutate inputs only for the innermost scope's database, or re-enter
the scopes that must forget. `once_per_request` keys its memo on `kind` and
`args`, so `args` must be hashable. Unhashable arguments raise `TypeError`,
and only while a scope for that `Database` is open. So the failure appears
only under scoping.

## Composition and experimental helpers

Integrations call one another at the cached query layer where composition is
needed:

- Python import analysis uses installed-package and deep-module results.
- Dependency checking combines installed metadata with workspace imports.
- Requirement evaluation combines parsed requirements, environment markers,
  and installed versions.
- Scope and symbol analysis build on shared Python source.

Those calls become ordinary dependency edges and need no user wiring.

That composition is between queries. A high-level entrypoint is not a query
and is unavailable inside one. A query body that reaches an entrypoint is
refused before the entrypoint runs. The refusal raises `CompositionError`
where the call is reached, and `UnsupportedValueError` where the kernel
rejects what the query captured before its body starts. Both derive from
`PyIncError`, so one `except` covers the boundary either way. Read the payload
query an entrypoint decodes, or call the entrypoint outside the query.

The three names under `Request scoping` are exempt from this rule. They
declare and use a span and analyze nothing, and the entrypoints themselves
call one of them.

Individual integration modules also expose payload queries and helper names
for in-repository composition. They are left out of
`pyinc.integrations.__all__` on purpose and fall outside this stable
contract. To rely on semver compatibility, import only the names listed in the
`Entrypoints`, `Result types`, and `Shared types` rows above.

LSP protocol behavior, filesystem watchers, scheduling, and code generation
belong to consumer packages and stay outside this integration surface.
