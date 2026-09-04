# `pyinc_codegen` — JSON-Schema → Typed Python Compiler

`pyinc_codegen` is a reference *consumer* of pyinc: a file→file compiler built
on the kernel. It reads a JSON Schema document and generates a typed Python
model per definition, a documentation file per definition, and an aggregate
`__init__.py`, emitting everything through the [`@action` reconciliation
layer](action-contract.md) so only the artifacts whose content changed are
rewritten. It is stdlib-only, uses only public `pyinc` names (`@query`,
`BinaryFileResource`, `Output` / `@action`), and keeps every JSON-Schema
concept out of the kernel; see the [package map](README.md#packages).

**Stability.** `pyinc_codegen` is **unstable** and outside the
semantic-versioning promise: its exported names and generated-output shape may
change in any release. [SECURITY.md](../SECURITY.md) states what the promise
covers.

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
exception exposes the full `analysis` and its error `diagnostics`. Validation is
completed before reconciliation starts, so malformed or unsupported input
cannot overwrite or delete outputs from the last valid run. The same boundary
applies when calling `generate_outputs.reconcile(...)` directly.

## Public surface

`pyinc_codegen` exports exactly the names below. The groups are editorial and a
later release may regroup them; what the table states is the union.

| Group | Names |
|---|---|
| Entrypoints | `generate`, `schema_analysis`, `generate_outputs` |
| Result types | `SchemaAnalysis`, `SchemaModel`, `FieldModel`, `Diagnostic` |
| Errors and enumerations | `SchemaGenerationError`, `DiagnosticSeverity` |

## Output layout

For each definition `D` (under `$defs` or legacy `definitions`):

- `<snake(D)>.py` — the typed model (owned by `D`)
- `docs/<snake(D)>.md` — the model's documentation (owned by `D`)
- `__init__.py` — re-exports every model (owned by the aggregate index)

A definition rendered as an `object` becomes a frozen `@dataclass`; an `enum`
becomes a `typing.Literal` alias; a top-level `$ref`/primitive, `const`, or
combinator becomes a type alias. Model references use deferred annotations and
imports guarded by `TYPE_CHECKING`; aliases store their type expression as a
forward-reference string, so mutually recursive models import on every
supported Python version. A schema node's shape is selected in one order
everywhere: `$ref`, then a combinator, then `enum`, then `const`, and only then
`type`.

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

The document root is metadata-only: `$schema`, `$id`, `title`, `$comment`, and
`description` are accepted there and ignored, and models must be declared under
`$defs` or `definitions`. `title` and `$comment` are ignored on every node;
nested `description` values are accepted, but only definition and property
descriptions are emitted into the generated documentation.

**Literal types.** `const` and `enum` compile to `typing.Literal` in every
position: `{"const": "user"}` renders `Literal['user']`, `{"enum": ["on", null]}`
renders `Literal['on', None]`. Members must be strings, integers, booleans, or
`null`; a declared nullable union is read as the type it names plus the null it
adds. `Literal` is imported only where a module's rendered types use it.

**Mappings and object models.** In property position an object node with a
schema-valued `additionalProperties` becomes `dict[str, T]`, recursively; an
object property without it stays `dict[str, object]`. In definition position an
object generates a frozen `@dataclass` whose fields are its `properties`, so a
schema-valued `additionalProperties` is rejected there, and a definition that
declares `type: object` with no `properties` keyword at all generates a
fieldless model with a warning — `{"properties": {}}` is silent, because it
declares the empty set. `{"type": ["object", "null"]}` takes the alias path and
renders `dict[str, object] | None`.

**Nullable and single-branch references.** `{"allOf": [S]}` renders as `S`, and
`{"anyOf": [S, {"type": "null"}]}` renders as `S` made optional, in either
branch order and not made optional twice. The null branch must be exactly
`{"type": "null"}` apart from annotations, which are validated as on any node.
Recursion through a model or a container keeps compiling (`list[Forest]`, a
field referring back to its own model); a loop formed only of aliases resolves
to no type and is rejected.

**Ignored keywords.** These never change the emitted type and record a
non-blocking `ignored-constraint` warning wherever they appear, including
beside `$ref` and `enum` and at the document root; the value shape is still
validated. Nothing here changes a field's Python type: a JSON `default` does
not become a dataclass default, and optional fields default to `None`.

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

Out of scope: remote/HTTP `$ref`, general combinators, conditional schemas,
`patternProperties`, positional (tuple) array types, and instance validation —
the compiler emits models and reports what it ignores.

## Diagnostics

Errors block generation and preserve the last valid output tree; warnings are
rendered into the definition's `docs/<x>.md`. The compiler never silently
guesses: an unconstrained shape is represented as `object` or `list[object]`
with a warning, and everything unsupported is an error at the keyword that
introduced it. Fallback renders (`Never` for an empty enum, `object` for an
unusable inline `enum` or `const`) keep inspection output valid Python while
the error still prevents reconciliation.

| Code | Severity | Raised when |
|---|---|---|
| `unsupported-root-schema` | error | A model keyword (`type`, `properties`, `required`, `items`, `enum`, `const`, `$ref`) or an unrecognized keyword sits at the document root. One diagnostic, pointed at `""`, names every such keyword. |
| `unsupported-construct` | error | A combinator the subset does not compile (multi-branch `allOf`, an `anyOf` that is not one schema plus `{"type": "null"}`, `oneOf`, `not`), a conditional, `patternProperties`, `prefixItems`, `unevaluatedProperties`, or schema-valued `additionalProperties` in definition position. |
| `unknown-ref` | error | A `$ref` names no definition, including a definition removed without updating its referrers. |
| `unsupported-tuple-items` | error | `items` is an array (the draft-07 tuple form). |
| `self-referential-alias` | error | An alias names only itself, in any spelling: a bare self `$ref`, a single-branch `allOf`, or a nullable `anyOf`. |
| `alias-cycle` | error | Aliases close a loop with no model or container between them; each member is reported with the cycle anchored at its alphabetically first member. |
| `reserved-definition-name` | error | A definition is named after one of the emitter's fixed imports (`dataclass`, `Literal`, `TypeAlias`, `TYPE_CHECKING`, `Never`) or the builtins its type expressions spell (`str`, `int`, `float`, `bool`, `list`, `dict`, `object`), compared after NFKC normalization. |
| `reserved-field-name` | error | A property is named after one of those, or after a data-model name such as `__dict__`, `__slots__`, or `__weakref__`. |
| `module-name-collision` | error | Two definitions produce the same portable module name after Unicode normalization, snake-casing, and case folding (`HTTPServer` beside `http_server`); both are reported. |
| `invalid-module-name`, `reserved-module-name`, `nonportable-module-name` | error | The snake-cased module stem is not a non-keyword identifier (`Class`); would occupy the generated `__init__.py`; or is a Windows-reserved device name (`CON`) or longer than portable filename limits. |
| `const-type-mismatch`, `enum-type-mismatch` | error | A `const` or `enum` member contradicts the declared `type`; a nullable union accepts both the named type and `null`. |
| `unsupported-const-value`, `unsupported-enum-value` | error | A member is not a string, integer, boolean, or `null`; PEP 586 allows no `float` in a `Literal`. |
| `empty-enum`, `duplicate-enum-value` | error | An `enum` has no members, or repeats one. |
| `invalid-constraint` | error | An ignored keyword carries a malformed value (`"minItems": -1`, `"format": 7`, `"multipleOf": 0`). |
| `invalid-description` | error | A `description` is not a string, on any node including a null branch. |
| `ignored-constraint` | warning | An annotation- or validation-only keyword from the table above is present; the type is unchanged. |
| `unconstrained-object-model` | warning | A definition declares `type: object` with no `properties` keyword, so a `$ref` to it types instance data away. |
| `unconstrained-schema` | warning | An explicitly unconstrained schema node is represented as `object`. |
| `unconstrained-array-items` | warning | An array has no `items` and no `prefixItems`, so it is represented as `list[object]`. |

## Incremental behavior

The query graph is decomposed so the kernel's backdating gives output-granular
incrementality, which the action layer turns into write-granular
incrementality:

- **whitespace / key reorder** — `schema_text` hands back the file's own bytes,
  so a formatting-only edit re-reads it; `document_diagnostics`,
  `definition_names`, `definition_raw` and `definition_pointer` each re-derive
  the same canonical value and backdate, so nothing downstream writes.
- **description-only change** — rewrites only the affected `docs/<x>.md`. The
  description-free `definition_structure` payload backdates, so the `.py`
  render query is reused and the model is not rewritten.
- **property type / requiredness change** — rewrites the affected model and its
  doc. Models that `$ref` it are re-validated and rewritten only if their
  emitted bytes change; models with no path to it are reused.
- **adding a definition** — creates its two files and updates the aggregate
  `__init__.py`; existing model render queries remain green.
- **removing an unreferenced definition** — deletes only the two files that
  definition owned; the index is updated. Removing a referenced definition is
  an `unknown-ref` error and preserves the prior output set.

Each of these is asserted byte-for-byte against a from-scratch run in
`tests/test_codegen.py`.

`model_python(A)` depends on `definition_structure(B)` for every `B` that `A`
references via `$ref`; `definition_structure` is the description-free payload
derived from `definition_model`, so the render query never observes
descriptions. A change to `B`'s structure puts `A` into the re-validation
closure, but because `A` refers to `B` only by class name, an internal change
to `B` does not change `A`'s emitted bytes, so `A` backdates and is not
rewritten. Reference existence is tracked per referenced definition rather
than through one dependency on the complete name set, so adding or removing an
unrelated definition does not revalidate every model. The reference graph
defines what is revalidated; content hashing defines what is rewritten.
