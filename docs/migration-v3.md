# Migrating from pyinc 2.x to 3.x

Version 3 intentionally removes compatibility aliases for contracts whose old
shape could not provide deterministic identity, safe persistence, or precise
source geometry. Upgrade code and discard v2 durable state before starting a
v3 process.

## Discard persisted state

- Delete saved checkpoint keys and create new checkpoints. Manifest schema v7
  rejects v1-v6 manifests with `CheckpointVersionError`. Snapshot objects using
  the `K2` value encoding remain valid, but no v2 checkpoint ledger is trusted.
- Delete `.pyinc-action.*.json` v1 ledgers after confirming their owned output
  directories. Current v3 writes schema v3 manifests named with the SHA-256 digest of
  the full tool identity and binds each ledger to its resolved output root. The
  next reconcile safely claims its desired files;
  an old ledger is never used to delete files.

## Kernel API changes

Inputs now require a stable, non-empty exact `str` key; string subclasses are
rejected rather than reduced to their characters:

```python
SOURCE = Input[str]("build.source", cutoff=cutoff_source)
```

The key replaces process-global creation ordinals. Within one `Database`, two
different `Input` objects may share a key only when their complete definition
is compatible. A conflict raises `InputKeyError` immediately.

`Query` is public. `@query(key="build.parse")` supplies an explicit stable exact
`str` key; string subclasses are rejected. Without it, the key is
`module:qualname`. Coroutine, async-generator, and
generator functions are rejected when decorated. Query identity includes the
supported static function implementation, defaults, statically discovered
immutable captures, transitive query captures, and equality/cutoff policies.
Policy captures and callable instance state found by that analysis must be
snapshot-safe. Local or dynamically unbound class objects are not stable
capture handles; define implementation types at module scope. Dynamic namespace
or reflection reads such as `globals()[name]`, `vars`, dynamic `getattr`,
`eval`/`exec`, and runtime imports are outside static capture analysis; move
their behavior-bearing state to an `Input`/`Resource` or declare it untracked.
Callable objects that expose `__wrapped__` are not treated as transparent
decorators and are rejected as captures, equality/cutoff policies, or
state-observation resource hooks. Decorators that return ordinary Python
functions, including `functools.wraps` decorators, and decorated bound methods
remain supported.

Distinct equal objects in defaults, reflected annotations, function state, or
any direct global/nonlocal capture are no longer interchangeable: Python can
expose their identity through `id`, `is`, protocols, and extension callables.
This includes captured managed handles, functions, modules, methods, and types.
Those definition sites carry a process-local incarnation, so a cross-process
checkpoint executes rather than reusing them. A capture-free query can still
warm an explicit query-argument call across processes.

Mappings crossing a v3 value boundary do not promise ordinary insertion order.
`FrozenDict` and thawed dictionaries iterate in canonical frozen-key
fingerprint order in every mode and after checkpoint reload. Code whose
mapping order is semantic should migrate that value to a tuple of pairs or a
dedicated adapter.

`Resource[KeyT, ValueT, ProbeT]` is public and exposes `read`, `probe`, `load`,
`probe_and_load`, `identity`, and `label`. Custom resources implement the
required `label`, `probe`, and `load` hooks; inherited defaults supply `read`,
`probe_and_load`, and `identity`. Module-level singleton resources are a
convention, not a semantic requirement. Callers may use
`Database.read_resource`; documented calls to private database resource methods
should be replaced. Use `BinaryFileResource` for bytes and `FileResource` for
decoded text.

Resource hooks now have an explicit no-managed-dependencies rule. `identity`,
`label`, `probe`, `load`, and `probe_and_load` may observe external state but
must not call a `Database` observation or read an `Input`, query, or another
resource. Move that composition into the reading query. Violations raise the
public `ResourceDependencyError` in strict, checked, and fast modes, including
when a hook catches the initial error or targets another database.

## Source and symbol APIs

All public source geometry uses zero-based `SourcePosition(line, character)`
and end-exclusive `SourceRange(start, end)`. Columns count Unicode code points.
Replace `lineno`, `col_offset`, and `end_col_offset` construction and access
with a range. Python AST byte columns are converted at the parser boundary.
Use `DocumentMap` for AST and LSP coordinate conversion; `PositionEncoding`
names the supported `"utf-8"`, `"utf-16"`, and `"utf-32"` encodings.
`EnvEntry.line_number` is likewise replaced by `EnvEntry.range`.

Name-only symbol lookup has been removed: `resolve_symbol` and
`ResolvedSymbol` are no longer public. Build a `ScopeTree` with
`scope_tree(...)`, resolve a position with `symbol_at(...)`, and pass the
resulting `SymbolId` to `find_references`. `ReferenceQueryResult.target` is a
`SymbolId`. `Scope` and `Binding` expose the lexical resolution used by all
supported navigation and refactoring features. Unsupported attribute chains
now return no result instead of a speculative target.

The language server negotiates UTF-8, UTF-16, or UTF-32 positions according to
the LSP 3.18 client preference order and defaults to UTF-16. Clients should not
assume Python code-point columns on the wire.

## Action API changes

`ReconcileResult.written` has been replaced by `created`, `updated`, and
`repaired`; `deleted`, `unchanged`, and `dry_run` remain. `deleted` records only
completed removals; `plan()` leaves it empty and places predicted removals in
`would_delete`. Handle the categories explicitly or concatenate the three
changed-path tuples when only a combined view is needed.

Action paths and manifests are now strict trust boundaries. Catch
`ActionPathError`, `ActionManifestError`, and `ActionLockTimeoutError` where an
operator-facing recovery is appropriate. Configure the 30-second default lock
timeout with `@action(..., lock_timeout=...)` or a reconcile call.

Filesystem artifact-store keys must be either a 64-character lowercase digest
or `ck` followed by one. Malformed keys raise `ArtifactStoreKeyError`.

## Code generation

`Diagnostic` now carries severity and a JSON Pointer. `generate` raises
`SchemaGenerationError` before action reconciliation when analysis contains an
error diagnostic, so existing output files remain untouched. Fix malformed
schemas, unresolved local references, unsupported constructs, invalid names,
and portable module-name collisions before retrying.
