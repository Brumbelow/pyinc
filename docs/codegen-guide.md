# `pyinc_codegen` — JSON-Schema → Typed Python Compiler

`pyinc_codegen` is a reference *consumer* of pyinc: the first useful file→file
compiler built on the kernel. It reads a JSON Schema document and generates a
typed Python model per definition, plus a documentation file per definition and
an aggregate `__init__.py`, emitting everything through the [`@action`
reconciliation layer](action-contract.md) so only the artifacts whose content
actually changed are rewritten.

It is **stdlib-only** — JSON Schema is parsed with `json` plus dict walking, not
a third-party schema library — and it builds on pyinc's **public API only**
(`pyinc` top-level: `@query`, `FileResource`, `Output` / `@action`). It never
imports kernel internals. This is the same architectural boundary that
`pyinc_tools` observes (see [architecture.md](architecture.md)).

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
`written` / `deleted` / `unchanged` sets. `schema_analysis(db, schema_path) ->
SchemaAnalysis` decodes the per-definition models for inspection without
generating.

## Output layout

For each definition `D` (under `$defs` or legacy `definitions`):

- `<snake(D)>.py` — the typed model (owned by `D`)
- `docs/<snake(D)>.md` — the model's documentation (owned by `D`)
- `__init__.py` — re-exports every model (owned by the aggregate index)

A definition rendered as an `object` becomes a frozen `@dataclass`; an `enum`
becomes a `typing.Literal` alias; a top-level `$ref`/primitive becomes a type
alias. Local `$ref`s render as the referenced class name with a matching
`from .<module> import <Name>`.

## Supported subset

- local JSON documents; `$defs` and legacy `definitions`
- local `$ref` (`#/$defs/X`, `#/definitions/X`)
- object `properties`, `required` vs optional
- arrays (`items`), primitives (`string`/`integer`/`number`/`boolean`/`null`)
- `enum`
- nullable unions (`type: ["X", "null"]`)
- `description` (rendered into docs only)
- deterministic diagnostics for unsupported constructs (remote `$ref`,
  `allOf`/`anyOf`/`oneOf`, unresolved `$ref`, unsupported unions) and for names
  that are not valid Python identifiers — `unsupported-field-name`,
  `unsupported-definition-name`, `unsupported-ref-name` — all surfaced on
  `SchemaModel.diagnostics`

**Identifier safety.** Names that are not valid Python identifiers are never
emitted as invalid code. A property named after a keyword (e.g. `class`) is
dropped from its model with an `unsupported-field-name` diagnostic; a definition
whose name is not an identifier renders a comment-only placeholder module,
is excluded from the aggregate index, and a `$ref` pointing at it falls back to
`object`. The generated package therefore always compiles.

Out of scope for this first cut: remote/HTTP `$ref`, combinators, conditional
schemas (`if`/`then`/`else`), `patternProperties`, format validation, and code
*validation* (the compiler emits models; it does not validate instances).

## Incremental behavior

The query graph is decomposed so the kernel's backdating gives output-granular
incrementality, which the action layer turns into write-granular incrementality:

- **whitespace / key reorder** — the `schema_text` cutoff is the canonicalized
  JSON, so formatting-only edits backdate and nothing downstream runs or writes.
- **description-only change** — rewrites only the affected `docs/<x>.md`; the
  `.py` model is description-independent, so it backdates and is not rewritten.
- **property type / requiredness change** — rewrites the affected model and its
  doc. Models that `$ref` it are re-validated (they are in the local
  reference-graph closure) and rewritten only if their emitted bytes change;
  models with no path to it are reused. (See *Reference-graph semantics* below.)
- **adding a definition** — creates its two files plus the aggregate
  `__init__.py`; existing models are untouched.
- **removing a definition** — deletes only the two files that definition owned;
  the index is updated.

Each of these is asserted byte-for-byte against a from-scratch run in
`tests/test_codegen.py`.

### Reference-graph semantics

`model_python(A)` depends on `definition_model(B)` for every `B` that `A`
references via `$ref`. A change to `B` therefore puts `A` into the
re-validation closure, but because `A` refers to `B` only by class name, an
internal change to `B` (e.g. one of `B`'s own properties changing requiredness)
does not change `A`'s emitted bytes — `A` backdates and is not rewritten.
Dependents *are* rewritten when the referenced interface changes in a way that
alters their output. Two distinct edits both land there: *removing* `B` so `A`'s
`$ref` dangles (the field falls back to `object` with an `unknown-ref`
diagnostic), and *consistently renaming* `B` (updating both the `$defs` key and
`A`'s `$ref` in one edit) so the graph stays fully resolved but the referenced
class name changes at `A`'s reference site. This is the efficient, correct
reading of "rewrite the affected model and its structural dependents": the
reference graph defines what is re-validated; content hashing defines what is
rewritten.

## Architectural boundary

`pyinc_codegen` lives in `src/pyinc_codegen/` and depends only on the stable
`pyinc` public surface. No JSON-Schema-specific concept lives under `src/pyinc`;
the kernel stays domain-agnostic. If the compiler needed something only
available as a kernel internal, that would be a signal to widen the public API
deliberately — not to reach around it.
