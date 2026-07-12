# `pyinc_codegen` — JSON-Schema → Typed Python Compiler

`pyinc_codegen` is a reference *consumer* of pyinc: the first useful file→file
compiler built on the kernel. It reads a JSON Schema document and generates a
typed Python model per definition, plus a documentation file per definition and
an aggregate `__init__.py`, emitting everything through the [`@action`
reconciliation layer](action-contract.md) so only the artifacts whose content
actually changed are rewritten.

It is stdlib-only and depends only on pyinc's public API; see [Architectural
boundary](#architectural-boundary) below.

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

## Output layout

For each definition `D` (under `$defs` or legacy `definitions`):

- `<snake(D)>.py` — the typed model (owned by `D`)
- `docs/<snake(D)>.md` — the model's documentation (owned by `D`)
- `__init__.py` — re-exports every model (owned by the aggregate index)

A definition rendered as an `object` becomes a frozen `@dataclass`; an `enum`
becomes a `typing.Literal` alias; a top-level `$ref`/primitive becomes a type
alias. Model references use deferred annotations and imports guarded by
`TYPE_CHECKING`; aliases store their type expression as a forward-reference
string. Mutually recursive models and aliases therefore compile and import on
every supported Python version.

## Supported subset

- local JSON documents; `$defs` and legacy `definitions`
- local `$ref` (`#/$defs/X`, `#/definitions/X`)
- object `properties`, `required` vs optional
- arrays (`items`), primitives (`string`/`integer`/`number`/`boolean`/`null`)
- `enum`
- nullable unions (`type: ["X", "null"]`)
- `description` (rendered for definitions and properties; accepted as an
  annotation on nested schema nodes)
- deterministic error diagnostics for malformed schema shapes, remote or
  unresolved `$ref`, schema combinators and conditionals, unsupported unions,
  invalid enum members, and names that cannot be emitted safely
- portable module collision checks after Unicode normalization, snake-case
  conversion, and case folding

**Identifier safety.** Invalid definition and property names are error
diagnostics rather than lossy substitutions. Generation stops before touching
the output tree. Property names reserved by Python's data model, such as
`__dict__`, `__slots__`, and `__weakref__`, are rejected as well. Definition
names that would produce the same portable module name (for example
`HTTPServer` and `http_server`) are rejected together, as is a definition that
would occupy the generated `__init__.py` path. The snake-cased module stem must
itself be a non-keyword Python identifier and may not be a Windows-reserved
device name, so names such as `Class` and `CON` fail analysis instead of
producing an unimportable or non-portable package.

Unsupported combinators, conditionals, and direct model keywords on the
document root are errors too. Models must live in `$defs` or `definitions`;
root metadata such as `$schema`, `$id`, `title`, and `description` remains
accepted. An unsupported root cannot be mistaken for an empty desired model
set, so the validation boundary described in [Usage](#usage) applies.

The accepted non-semantic metadata policy is deliberately narrow. `title` and
`$comment` are accepted as string annotations on schema nodes and ignored by
generation. At the document root, `$schema` and `$id` are also accepted as
string metadata and ignored: they do not select a dialect, change reference
resolution, or enable remote references. Nested `description` values are
accepted but only definition and property descriptions are emitted into the
generated documentation. Every other keyword outside the supported subset is
an error, including validation-only constraints such as `format`, `minimum`,
`additionalProperties`, and `minItems`.

**Fallback policy.** The supported subset never silently guesses. An explicitly
unconstrained schema is represented as `object` with a non-blocking
`unconstrained-schema` warning; an array without `items` similarly uses
`list[object]` with a warning. Unsupported constructs and invalid data are
errors. Empty enums render internally as `Never` so inspection output remains
valid Python, but their `empty-enum` error prevents reconciliation.

Out of scope for v3: remote/HTTP `$ref`, combinators, conditional schemas
(`if`/`then`/`else`), `patternProperties`, validation constraints, and code
*validation* (the compiler emits models; it does not validate instances).

## Incremental behavior

The query graph is decomposed so the kernel's backdating gives output-granular
incrementality, which the action layer turns into write-granular incrementality:

- **whitespace / key reorder** — the `schema_text` cutoff is the canonicalized
  JSON, so formatting-only edits backdate and nothing downstream runs or writes.
- **description-only change** — rewrites only the affected `docs/<x>.md`. The
  description-free `definition_structure` payload backdates, so the `.py`
  render query is reused and the model is not rewritten.
- **property type / requiredness change** — rewrites the affected model and its
  doc. Models that `$ref` it are re-validated (they are in the local
  reference-graph closure) and rewritten only if their emitted bytes change;
  models with no path to it are reused. (See *Reference-graph semantics* below.)
- **adding a definition** — creates its two files and updates the aggregate
  `__init__.py`; existing model render queries remain green and are not
  executed.
- **removing an unreferenced definition** — deletes only the two files that
  definition owned; the index is updated. Removing a referenced definition is
  an error and preserves the prior output set.

Each of these is asserted byte-for-byte against a from-scratch run in
`tests/test_codegen.py`.

### Reference-graph semantics

`model_python(A)` depends on `definition_structure(B)` for every `B` that `A`
references via `$ref`. `definition_structure` is the description-free payload
derived from `definition_model`, so the dependency on `definition_model` is
transitive; `db.inspect` on `model_python` shows `definition_structure` edges.
This intermediate is exactly what makes description-only edits cheap: the
render query never observes descriptions, so a description change cannot reach
it. A change to `B`'s structure therefore puts `A` into the
re-validation closure, but because `A` refers to `B` only by class name, an
internal change to `B` (e.g. one of `B`'s own properties changing requiredness)
does not change `A`'s emitted bytes — `A` backdates and is not rewritten.
Dependents *are* rewritten when the referenced interface changes in a way that
alters their output. A consistent rename of `B` (updating both the `$defs` key
and `A`'s `$ref` in one edit) keeps the graph resolved while changing the class
name at `A`'s reference site. Removing `B` without updating `A` instead
produces an `unknown-ref` error; as with any error, the last valid output tree
is preserved. The reference graph defines what is revalidated; content hashing
defines what is rewritten.

Reference existence is tracked per referenced definition rather than through a
single dependency on the complete definition-name set. Adding or removing an
unrelated definition therefore does not place every existing model in the
revalidation closure.

## Architectural boundary

`pyinc_codegen` lives in `src/pyinc_codegen/` and is **stdlib-only**: JSON
Schema is parsed with `json` plus dict walking, not a third-party schema
library. It builds on pyinc's **public API only** (`pyinc` top-level:
`@query`, `BinaryFileResource`, `Output` / `@action`) and never imports kernel
internals — the same architectural boundary that `pyinc_tools` observes (see
[architecture.md](architecture.md)). No JSON-Schema-specific concept lives
under `src/pyinc`; the kernel stays domain-agnostic. If the compiler needed
something only available as a kernel internal, that would be a signal to widen
the public API deliberately — not to reach around it.
