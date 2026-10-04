# `pyinc_codegen`: JSON-Schema → Typed Python Compiler

`pyinc_codegen` is a reference *consumer* of pyinc: a file→file compiler built
on the kernel. It reads a JSON Schema document and generates a typed Python
model per definition, a documentation file per definition, and an aggregate
`__init__.py`. Everything goes through the [`@action` reconciliation
layer](action-contract.md), so only artifacts whose content changed are
rewritten.

It is stdlib-only and uses only public `pyinc` names (`@query`,
`BinaryFileResource`, `Output` / `@action`). JSON-Schema concepts stay in
`pyinc_codegen`, outside the kernel. See the [package map](README.md#packages).

`pyinc_codegen` is **unstable** and outside the semantic-versioning promise.
Its exported names and generated-output shape may change in any release.
[SECURITY.md](../SECURITY.md) states what the promise covers.

## Usage

```python
from pyinc import Database
from pyinc_codegen import generate, generate_outputs

db = Database(mode="strict")
result = generate(db, "schema.json", "generated/")   # writes models into generated/
# Re-run after editing schema.json: only affected files are rewritten.
plan = generate_outputs.plan(db, "schema.json", root="generated/")  # dry-run, writes nothing
```

`generate(db, schema_path, out_dir) -> ReconcileResult` returns the
`created` / `updated` / `repaired` / `deleted` / `unchanged` sets.
`schema_analysis(db, schema_path) -> SchemaAnalysis` decodes the per-definition
models and structured diagnostics without generating. Each diagnostic has an
`error` or `warning` severity and an RFC 6901 `json_pointer` into the source
document.

`generate` raises `SchemaGenerationError` when analysis contains an error. The
exception exposes the full `analysis` and its error `diagnostics`. Validation
finishes before reconciliation starts, so malformed or unsupported input leaves
the outputs from the last valid run in place. The same boundary applies when
you call `generate_outputs.reconcile(...)` directly.

## Public surface

`pyinc_codegen` exports the names below and only those. The grouping is
editorial and may change in a later release. The export list is the union of
all rows.

| Group | Names |
|---|---|
| Entrypoints | `generate`, `schema_analysis`, `generate_outputs` |
| Result types | `SchemaAnalysis`, `SchemaModel`, `FieldModel`, `Diagnostic` |
| Errors and enumerations | `SchemaGenerationError`, `DiagnosticSeverity` |

## Output layout

For each definition `D` (under `$defs` or legacy `definitions`):

- `<snake(D)>.py`: the typed model (owned by `D`)
- `docs/<snake(D)>.md`: the model's documentation (owned by `D`)
- `__init__.py`: re-exports every model (owned by the aggregate index)

A definition rendered as an `object` becomes a frozen `@dataclass`. An `enum`
becomes a `typing.Literal` alias. A top-level `$ref`/primitive, `const`, or
combinator becomes a type alias.

Model references use deferred annotations and imports guarded by
`TYPE_CHECKING`. Aliases store their type expression as a forward-reference
string. Together these let mutually recursive models import on every supported
Python version.

A schema node's shape is selected in the same order everywhere: `$ref`, then a
combinator, then `enum`, then `const`, and last `type`.

## Supported subset

- local JSON documents; `$defs` and legacy `definitions`
- local `$ref` (`#/$defs/X`, `#/definitions/X`)
- object `properties`, `required` vs optional
- arrays (`items`), primitives (`string`/`integer`/`number`/`boolean`/`null`)
- `enum` and `const`, rendered as `typing.Literal`
- nullable unions (`type: ["X", "null"]`)
- schema-valued `additionalProperties` in property position, compiled to
  `dict[str, T]`
- single-branch `allOf` and nullable `anyOf`
- `description` (rendered for definitions and properties; accepted as an
  annotation on nested schema nodes)
- annotation- and validation-only keywords, accepted with an
  `ignored-constraint` warning
- deterministic diagnostics for everything else, listed under
  [Diagnostics](#diagnostics)

The document root is metadata-only. It accepts and ignores `$schema`, `$id`,
`title`, `$comment`, and `description`. Declare models under `$defs` or
`definitions`. `title` and `$comment` are ignored on every node. Nested
`description` values are accepted, and the generated documentation includes
only definition and property descriptions.

### Literal types

`const` and `enum` compile to `typing.Literal` in every position.
`{"const": "user"}` renders `Literal['user']`, and `{"enum": ["on", null]}`
renders `Literal['on', None]`. Members must be strings, integers, booleans, or
`null`. A declared nullable union is read as the type it names plus the null it
adds. `Literal` is imported only where a module's rendered types use it.

### Mappings and object models

In property position, an object node with a schema-valued
`additionalProperties` becomes `dict[str, T]`, recursively. An object property
without it stays `dict[str, object]`.

In definition position, an object generates a frozen `@dataclass` whose fields
are its `properties`, so a schema-valued `additionalProperties` is rejected
there. A definition that declares `type: object` with no `properties` keyword at
all generates a fieldless model with a warning. `{"properties": {}}` declares
the empty set, so it generates a fieldless model silently.

`{"type": ["object", "null"]}` takes the alias path and renders
`dict[str, object] | None`.

### Nullable and single-branch references

`{"allOf": [S]}` renders as `S`. `{"anyOf": [S, {"type": "null"}]}` renders as
`S` made optional once, in either branch order. The null branch may hold only
`{"type": "null"}` and annotations, and those annotations are validated as on
any node. Recursion through a model or a container compiles (`list[Forest]`, a
field referring back to its own model). A loop formed only of aliases resolves
to no type and is rejected.

### Ignored keywords

These keywords leave the emitted type unchanged. Wherever they appear,
including beside `$ref` and `enum` and at the document root, they record a
non-blocking `ignored-constraint` warning. Their value shape is still
validated. A JSON `default` never becomes a dataclass default, and optional
fields default to `None`.

| Keyword | Accepted value shape |
| --- | --- |
| `format`, `pattern` | a string |
| `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` | a number |
| `multipleOf` | a number greater than zero |
| `minLength`, `maxLength`, `minItems`, `maxItems` | a non-negative integer |
| `uniqueItems`, `deprecated`, `readOnly`, `writeOnly` | a boolean |
| `additionalProperties` | a boolean (`false` or `true`) |
| `examples` | an array |
| `default` | any JSON value |

### Out of scope

The subset excludes remote/HTTP `$ref`, general combinators, conditional
schemas, `patternProperties`, positional (tuple) array types, and instance
validation. The compiler emits models and reports what it ignores.

## Diagnostics

Errors block generation and keep the last valid output tree. Warnings are
rendered into the definition's `docs/<x>.md`. The compiler reports every guess
it makes. An unconstrained shape becomes `object` or `list[object]` with a
warning, and anything unsupported is an error at the keyword that introduced
it. Fallback renders (`Never` for an empty enum, `object` for an unusable
inline `enum` or `const`) keep inspection output valid Python while the error
still blocks reconciliation.

| Code | Severity | Raised when |
|---|---|---|
| `unsupported-root-schema` | error | A model keyword (`type`, `properties`, `required`, `items`, `enum`, `const`, `$ref`) or an unrecognized keyword sits at the document root. One diagnostic, pointed at `""`, names every such keyword. |
| `unsupported-construct` | error | A combinator the subset does not compile (multi-branch `allOf`, an `anyOf` that is not one schema plus `{"type": "null"}`, `oneOf`, `not`), a conditional, `patternProperties`, `prefixItems`, `unevaluatedProperties`, or schema-valued `additionalProperties` in definition position. |
| `unknown-ref` | error | A `$ref` names no definition, including a definition removed without updating its referrers. |
| `unsupported-tuple-items` | error | `items` is an array (the draft-07 tuple form). |
| `self-referential-alias` | error | An alias names only itself, in any spelling: a bare self `$ref`, a single-branch `allOf`, or a nullable `anyOf`. |
| `alias-cycle` | error | Aliases close a loop with no model or container between them. Each member is reported, with the cycle anchored at its alphabetically first member. |
| `reserved-definition-name` | error | A definition is named after one of the emitter's fixed imports (`dataclass`, `Literal`, `TypeAlias`, `TYPE_CHECKING`, `Never`) or the builtins its type expressions spell (`str`, `int`, `float`, `bool`, `list`, `dict`, `object`), compared after NFKC normalization. |
| `reserved-field-name` | error | A property is named after one of those, or after a data-model name such as `__dict__`, `__slots__`, or `__weakref__`. |
| `module-name-collision` | error | Two definitions produce the same portable module name after Unicode normalization, snake-casing, and case folding (`HTTPServer` beside `http_server`). Both are reported. |
| `invalid-module-name`, `reserved-module-name`, `nonportable-module-name` | error | The snake-cased module stem is a keyword or not an identifier (`Class`), would occupy the generated `__init__.py`, or is a Windows-reserved device name (`CON`) or longer than portable filename limits. |
| `const-type-mismatch`, `enum-type-mismatch` | error | A `const` or `enum` member contradicts the declared `type`. A nullable union accepts both the named type and `null`. |
| `unsupported-const-value`, `unsupported-enum-value` | error | A member is not a string, integer, boolean, or `null`. PEP 586 allows no `float` in a `Literal`. |
| `empty-enum`, `duplicate-enum-value` | error | An `enum` has no members, or repeats one. |
| `invalid-constraint` | error | An ignored keyword carries a malformed value (`"minItems": -1`, `"format": 7`, `"multipleOf": 0`). |
| `invalid-description` | error | A `description` is not a string, on any node including a null branch. |
| `ignored-constraint` | warning | An annotation- or validation-only keyword from the table above is present. The type is unchanged. |
| `unconstrained-object-model` | warning | A definition declares `type: object` with no `properties` keyword, so a `$ref` to it types instance data away. |
| `unconstrained-schema` | warning | An explicitly unconstrained schema node is represented as `object`. |
| `unconstrained-array-items` | warning | An array has no `items` and no `prefixItems`, so it is represented as `list[object]`. |

## Incremental behavior

The query graph is split so the kernel's backdating gives output-granular
incrementality. The action layer turns that into write-granular
incrementality:

- Whitespace or key reorder: `schema_text` hands back the file's own bytes, so a
  formatting-only edit re-reads it. `document_diagnostics`, `definition_names`,
  `definition_raw` and `definition_pointer` each re-derive the same canonical
  value and backdate, so every downstream output stays as it is.
- Description-only change: rewrites only the affected `docs/<x>.md`. The
  description-free `definition_structure` payload backdates, so the `.py`
  render query is reused and the model file stays as it is.
- Property type or requiredness change: rewrites the affected model and its
  doc. Models that `$ref` it are re-validated and rewritten only if their
  emitted bytes change. Models with no path to it are reused.
- Adding a definition: creates its two files and updates the aggregate
  `__init__.py`. Existing model render queries stay green.
- Removing an unreferenced definition: deletes only the two files that
  definition owned and updates the index. Removing a referenced definition is
  an `unknown-ref` error and keeps the prior output set.

Each of these is asserted byte-for-byte against a from-scratch run in
`tests/test_codegen.py`.

`model_python(A)` depends on `definition_structure(B)` for every `B` that `A`
references via `$ref`. `definition_structure` is the description-free payload
derived from `definition_model`, so the render query never observes
descriptions.

A change to `B`'s structure puts `A` into the re-validation closure. `A` refers
to `B` only by class name, so an internal change to `B` leaves `A`'s emitted
bytes the same. `A` then backdates, and its file stays as it is.

Reference existence is tracked per referenced definition. A single dependency
on the complete name set would make adding or removing an unrelated definition
revalidate every model. The reference graph defines what is revalidated.
Content hashing defines what is rewritten.
