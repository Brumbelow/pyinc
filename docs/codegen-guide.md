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
becomes a `typing.Literal` alias; a top-level `$ref`/primitive, `const`, or
combinator becomes a type alias. Model references use deferred annotations and
imports guarded by `TYPE_CHECKING`; aliases store their type expression as a
forward-reference string. Mutually recursive models therefore compile and
import on every supported Python version, and an alias that names a model
caught in such a cycle imports fine too. A cycle formed entirely of aliases
(alias to alias and back, with no model or container type between) resolves to
no type and is rejected — see [Nullable and single-branch
references](#nullable-and-single-branch-references).

A schema node's shape is selected in one order everywhere the compiler reads
one — `$ref`, then a combinator, then `enum`, then `const`, and only then
`type`. A definition is no exception: `{"type": "object", "const": "ticket"}`
is read as the `const` it declares, so its member checks report a
`const-type-mismatch` error rather than the keyword being dropped into an empty
dataclass. Which keywords may legitimately sit beside the selected shape is
covered by the ambiguity rules in [Ignored keywords](#ignored-keywords).

## Supported subset

- local JSON documents; `$defs` and legacy `definitions`
- local `$ref` (`#/$defs/X`, `#/definitions/X`)
- object `properties`, `required` vs optional
- arrays (`items`), primitives (`string`/`integer`/`number`/`boolean`/`null`)
- `enum` and `const`, rendered as `typing.Literal` (see [Literal
  types](#literal-types))
- nullable unions (`type: ["X", "null"]`)
- schema-valued `additionalProperties` in property position, compiled to
  `dict[str, T]` (see [Mappings and object models](#mappings-and-object-models))
- single-branch `allOf` and nullable `anyOf` (see [Nullable and single-branch
  references](#nullable-and-single-branch-references))
- `description` (rendered for definitions and properties; accepted as an
  annotation on nested schema nodes)
- annotation- and validation-only keywords, accepted with an
  `ignored-constraint` warning (see [Ignored keywords](#ignored-keywords))
- deterministic error diagnostics for malformed schema shapes, remote or
  unresolved `$ref`, unsupported combinators and conditionals, unsupported
  unions, invalid `enum`/`const` members, aliases that name only themselves,
  and names that cannot be emitted safely
- portable module collision checks after Unicode normalization, snake-case
  conversion, and case folding

**Identifier safety.** Invalid definition and property names are error
diagnostics rather than lossy substitutions. Generation stops before touching
the output tree. Property names reserved by Python's data model, such as
`__dict__`, `__slots__`, and `__weakref__`, are rejected as well. Names that
shadow a binding the generated modules rely on are rejected too: the emitter's
fixed imports (`dataclass`, `Literal`, `TypeAlias`, `TYPE_CHECKING`, `Never`)
and the builtins its type expressions spell (`str`, `int`, `float`, `bool`,
`list`, `dict`, `object`). A definition with one of those names is a
`reserved-definition-name` error, because every module that imports it under
`TYPE_CHECKING` would rebind the name; a property with one is a
`reserved-field-name` error, because the field binds the name for the rest of
its own class body, so a later `zone: str` in the same model would stop naming
the builtin. Both comparisons run after NFKC normalization, so the fullwidth
spellings are rejected with them. Definition
names that would produce the same portable module name (for example
`HTTPServer` and `http_server`) are rejected together, as is a definition that
would occupy the generated `__init__.py` path. The snake-cased module stem must
itself be a non-keyword Python identifier and may not be a Windows-reserved
device name, so names such as `Class` and `CON` fail analysis instead of
producing an unimportable or non-portable package.

Combinators and conditionals on the document root are errors, each reported at
its own keyword; the two supported combinator spellings are compiled inside
definitions only, because the root declares no model. A model keyword at the
document root — `type`, `properties`, `required`, `items`, `enum`, `const`, or
`$ref` — and any keyword the compiler does not recognize are collected into
exactly one `unsupported-root-schema` error, pointed at the whole document
(`json_pointer == ""`), naming every keyword it collected and the rule they
violate: **models must be declared under `$defs` or `definitions`**, because
the root is metadata-only and its schema keywords describe no model. Root
metadata such as `$schema`, `$id`, `title`, and `description` remains accepted.
So are the annotation- and validation-only keywords in [Ignored
keywords](#ignored-keywords): the root is a schema node like any other for
them, so `{"$defs": {...}, "minimum": 0}` records the same non-blocking
`ignored-constraint` warning at `/minimum` that it would record anywhere else,
and generation proceeds. An unsupported root cannot be mistaken for an empty
desired model set, so the validation boundary described in [Usage](#usage)
applies.

The accepted non-semantic metadata policy is deliberately narrow. `title` and
`$comment` are accepted as string annotations on schema nodes and ignored by
generation, silently — they are documentation, not constraints. At the document
root, `$schema` and `$id` are also accepted as string metadata and ignored:
they do not select a dialect, change reference resolution, or enable remote
references. Nested `description` values are accepted but only definition and
property descriptions are emitted into the generated documentation.

### Literal types

`const` and `enum` name a closed set of values, so they compile to
`typing.Literal`:

| Schema node | Emitted type |
| --- | --- |
| `{"const": "user"}` | `Literal['user']` |
| `{"enum": ["red", "green"]}` | `Literal['red', 'green']` |
| `{"enum": ["on", null]}` | `Literal['on', None]` (already nullable) |

A definition whose body is an `enum` becomes a `Literal` type alias, as before.
In property, array-item, mapping-value, and combinator-branch position both
keywords render inline; the member checks that were always performed — at least
one member, no duplicates, supported member type, agreement with a declared
`type` — are reported against the same node and still block generation.

Members must be strings, integers, booleans, or `null`. PEP 586 does not allow
a `float` in a `Literal`, so `{"const": 1.5}` is an `unsupported-const-value`
error and a float `enum` member is an `unsupported-enum-value` error. A member
that contradicts a declared `type` is a `const-type-mismatch` or
`enum-type-mismatch` error. A declared nullable union is read as the type it
names plus the null it adds, so both members of
`{"type": ["string", "null"], "enum": ["red", null]}` agree with it, in either
branch order; `{"type": ["string", "null"], "enum": ["red", 7]}` still reports
the `7`. `Literal` is imported into a generated module only
when that module's rendered types actually use it.

### Mappings and object models

`additionalProperties` is compiled where it can be expressed as a type. In
**property** position an object node with a schema-valued `additionalProperties`
becomes `dict[str, T]`, recursively: `$ref`, array, and nested-mapping values
all work, and a referenced definition joins the model's reference graph. An
object property without it stays `dict[str, object]`.

In **definition** position an object generates a frozen `@dataclass` whose
fields are its `properties`, and a dataclass cannot carry free-form entries.
Schema-valued `additionalProperties` therefore remains an
`unsupported-construct` error there. A definition that declares `type: object`
with no `properties` at all is accepted, but records a non-blocking
`unconstrained-object-model` warning: it generates a model with no fields, so a
`$ref` to it types instance data away. Compiling either case into a
`dict[str, T]` alias would change what already-generated definitions emit and
what downstream code imports, so that is a major-version decision rather than
part of this subset.

That warning is keyed on the *absence* of `properties`, not on the emitted
dataclass — one cause, one diagnostic:

- a definition that carries `properties` at all is silent, even when the map is
  empty. `{"type": "object", "properties": {}}` emits the same fieldless
  dataclass with no diagnostic, because declaring the empty set says the model
  has no fields where omitting the keyword leaves that unsaid. The type-less
  spelling `{"properties": {}}` is silent too.
- a definition with a schema-valued `additionalProperties` does constrain its
  instances, and is rejected on that keyword alone; the warning would misname
  the cause and is not also reported
- `{"type": ["object", "null"]}` takes the alias path and renders
  `dict[str, object] | None`, which keeps the instance data rather than typing
  it away, so it is accepted with no diagnostic at all

### Nullable and single-branch references

The subset cannot otherwise express an *optional reference* to another model —
a JSON-Schema `type` union carries type names, not a `$ref` — so the two
idiomatic spellings that name exactly one type are compiled:

- `{"allOf": [S]}` renders as whatever `S` renders as
- `{"anyOf": [S, {"type": "null"}]}` renders as `S` made optional, in either
  branch order (and is not made optional twice if `S` is already nullable)

The null branch must be exactly `{"type": "null"}`, apart from annotations, and
those annotations are validated there exactly as they are on any other schema
node — `{"type": "null", "description": 123}` is an `invalid-description` error
even though the branch itself names no type.

Everything else remains an `unsupported-construct` error reported at the
keyword: a multi-branch `allOf`, an `anyOf` that is not one schema plus
`{"type": "null"}`, and `oneOf` in every shape. An `anyOf` whose branches are
*both* `{"type": "null"}` is rejected for the same reason a one- or three-branch
`anyOf` is: it names no type to make optional. General unions are out of
scope — the compiler has no rule for choosing one Python type for them.

An alias that names only itself denotes no type:
`{"Loop": {"anyOf": [{"$ref": "#/$defs/Loop"}, {"type": "null"}]}}` would emit
`Loop: TypeAlias = 'Loop | None'`, which resolves to nothing, so it is a
`self-referential-alias` error reported at the definition — in every spelling
that reaches it, a bare self `$ref`, a single-branch `allOf`, and a nullable
`anyOf`. Recursion that passes through a model or a container still names a
type and keeps compiling: `{"Forest": {"type": "array", "items": {"$ref":
"#/$defs/Forest"}}}` renders `list[Forest]`, and a dataclass field may refer
back to its own model.

The same problem reappears across definitions: `{"A": {"$ref": "#/$defs/B"},
"B": {"$ref": "#/$defs/A"}}` closes a loop of aliases with no type in between,
so each member is an `alias-cycle` error naming the cycle anchored at its
alphabetically first member (`A -> B -> A`). As with the single-definition
case, a container or object field between the aliases breaks the loop and keeps
them compiling.

### Ignored keywords

Annotation- and validation-only keywords are accepted wherever a schema node is
accepted, including beside `$ref` and `enum`. They never change the emitted
type, so each one records a **non-blocking `ignored-constraint` warning** that
names the keyword and points at it. Generation proceeds; the warning is
rendered into the definition's `docs/<x>.md` alongside the model.

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

The value shape is still validated: a malformed value (`"minItems": -1`,
`"format": 7`, `"multipleOf": 0`) is an `invalid-constraint` **error** that
blocks generation, because a schema that cannot be read cannot be honoured
silently either. A *boolean* `additionalProperties` is unenforceable by a
dataclass or a mapping and is merely ignored; a *schema* value is compiled or
rejected per [Mappings and object models](#mappings-and-object-models). Nothing
in this table changes a field's Python type — in particular a JSON `default`
does not become a dataclass default; optional fields default to `None`.

Every keyword outside both the supported subset and this table remains an
error, including `patternProperties`, `prefixItems`, `unevaluatedProperties`,
`not`, `oneOf`, and conditionals.

**Fallback policy.** The supported subset never silently guesses. An explicitly
unconstrained schema is represented as `object` with a non-blocking
`unconstrained-schema` warning; an array without `items` similarly uses
`list[object]` with an `unconstrained-array-items` warning — but not when
`prefixItems` is present, because the items are constrained there and that
keyword is reported where it appears. Unsupported constructs and invalid data
are errors. The draft-07 tuple form of `items` (an array of positional schemas)
is a valid schema node, so it is reported as `unsupported-tuple-items` rather
than as a malformed node. An `enum` definition with no usable members renders
internally as `Never`, and an unusable inline `enum` or `const` falls back to
`object`, so inspection output remains valid Python — but the `empty-enum`,
`unsupported-enum-value`, and `unsupported-const-value` errors prevent
reconciliation.

Out of scope for v3: remote/HTTP `$ref`, general combinators (anything beyond
the two spellings in [Nullable and single-branch
references](#nullable-and-single-branch-references)), conditional schemas
(`if`/`then`/`else`), `patternProperties`, positional (tuple) array types, and
code *validation* — the compiler emits models and reports what it ignores; it
does not validate instances.

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
