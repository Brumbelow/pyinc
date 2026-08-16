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
  directories. v3 writes schema v2 manifests named with the SHA-256 digest of
  the full tool identity and binds each ledger to its resolved output root. The
  next reconcile safely claims its desired files;
  an old ledger is never used to delete files.

## Kernel API changes

Inputs now require a stable, non-empty key:

```python
SOURCE = Input[str]("build.source", cutoff=cutoff_source)
```

The key replaces process-global creation ordinals. Within one `Database`, two
different `Input` objects may share a key only when their complete definition
is compatible. A conflict raises `InputKeyError` immediately.

`Query` is public. `@query(key="build.parse")` supplies an explicit stable key;
without it the key is `module:qualname`. Coroutine, async-generator, and
generator functions are rejected when decorated. Query identity includes the
function implementation, defaults, immutable captures, transitive query
captures, and equality/cutoff policies. Policy captures and callable instance
state must be snapshot-safe. Local or dynamically unbound class objects are not
stable capture handles; define implementation types at module scope.

`Resource[KeyT, ValueT, ProbeT]` is public and exposes `read`, `probe`, `load`,
`probe_and_load`, `identity`, and `label`. Custom resources should use those
hooks and callers may use `Database.read_resource`; documented calls to private
database resource methods should be replaced. Use `BinaryFileResource` for
bytes and `FileResource` for decoded text.

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
`repaired`; `deleted`, `unchanged`, and `dry_run` remain. Handle the categories
explicitly or concatenate the three changed-path tuples when only a combined
view is needed.

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
