# Integration Authoring Guide

An integration is a domain-specific query graph built on the pyinc kernel. The kernel
provides revisions, dependency tracking, red-green verification, and backdating. The
integration provides domain types, query decomposition, and external-state resources.

This guide extracts the shared patterns from the shipped integrations. It uses
`pyinc.integrations.python_source` as the reference template and
`toml_config` / `requirements_txt` as smaller companion examples. Read
[kernel-contract.md](kernel-contract.md) for the soundness guarantee and its
conditions, and [integration-contract.md](integration-contract.md) for the current
public boundary.

## Three-Layer Query Structure

Integrations use three layers of queries.

### Layer 1: Payload queries

Payload queries are `@query`-decorated functions that read resources, parse data, and
return snapshot-safe payloads. They are the kernel-level cached nodes. They come in two
kinds, which differ in where a comparison policy may live:

- *Raw-text reads* return a file's text as a plain `str`. They are compared by exact
  equality and carry no comparison policy. A token coarser than the text would let the
  node serve new bytes while reporting that nothing changed (see *Comparison Policies*).
  Examples: `source_text` and `notebook_text`.
- *Projection payloads* return the parsed structure a consumer needs, usually as a
  tuple. An edit the projection does not carry leaves it unchanged. The node then
  either re-parses to an equal payload, which the kernel backdates on the value itself
  under default equality, or is reused behind a node that already did. Either way the
  consumers below stay valid, and no comparison policy was declared anywhere. Examples:
  `imports_for_file`, `definitions_for_file`, and `notebook_cells_payload`.

### Layer 2: Composition queries

Composition queries call other queries and assemble richer composite payloads. Example:
`workspace_analysis_payload` calls `module_analysis_payload` in a loop over discovered
Python files.

### Layer 3: High-level entrypoints

High-level entrypoints are plain functions, without `@query`, that call `db.get()` and
decode tuple payloads into frozen dataclasses. They are the public API. Examples:
`file_analysis` and `workspace_analysis`. Call them from outside a query. The
[integration contract](integration-contract.md#composition-and-experimental-helpers)
states how a query body that reaches one is refused.

### Why this layering?

The kernel caches and compares tuple payloads efficiently, because they are
snapshot-safe and hashable by default. The decode layer converts them to ergonomic
dataclasses only at the public boundary. Internal graph nodes stay cheap to hash and
compare, and the external API stays user-friendly.

## Result Types

All public result types must be `@dataclass(frozen=True)` with snapshot-safe fields:

- Use snapshot-safe scalars, tuples, and nested frozen dataclasses such as
  `SourcePosition` and `SourceRange`.
- Use `tuple[T, ...]` instead of `list[T]` for collections (tuples are hashable and
  immutable).
- Keep `list`, `dict`, and `set` out of result type fields.
- Reference: `ImportRef`, `PythonFileAnalysis`, and `PythonWorkspaceAnalysis`.

The public dataclasses are decoded *after* `db.get()` returns the cached tuple payload.
Their frozen shape gives callers an immutable, typed result and keeps arbitrary classes
out of the snapshot contract. If a dataclass itself crosses a cached boundary, `freeze`
stores it as a `FrozenRecord`, and ordinary `thaw` returns a dictionary. Preserving the
original class requires a matching `ValueAdapter`. The kernel ships such adapters for its
own resource snapshot types: a `FileStatResource` reading arrives as a
`FileStatSnapshot` in every mode. An integration's own dataclasses are ordinary user
classes and follow the rule above.

## Payload Type Aliases

Define a `TypeAlias` for each internal payload shape. The tuple may use a compact
representation that differs from the corresponding public dataclass:

```python
ImportPayload: TypeAlias = tuple[str, ImportKind, int]
#                                 module, kind,  internal one-based line
```

Internal payloads may retain compact line-oriented coordinates. Each layer has
a `_decode_*` function that reconstructs the dataclass and converts source
locations to the public zero-based, code-point `SourceRange` contract. When the
payload originates from Python's AST, use the public `DocumentMap` at the parser
boundary, so UTF-8 byte columns are converted once. Reference:
`ImportPayload`, `FileAnalysisPayload`, `_decode_import`, and
`_decode_file_analysis`.

Tuple payloads are cheap for the kernel to cache and compare (see *Three-Layer Query
Structure*). The `TypeAlias` documents the conversion in both directions.

## Resources

Inside a query, all reads of external state must go through the Resource API. During
query execution the kernel intercepts the calls
[condition 2](kernel-contract.md#2-tracked-ambient-reads) enumerates, and raises
`UntrackedReadError` when one happens outside a resource hook.

The built-in resources `FileResource`, `BinaryFileResource`, `FileStatResource`,
`EnvResource`, `DirectoryResource`, and `ResolvedPathResource` cover common cases.

### Custom resources

When the built-in resources do not fit, subclass `Resource[KeyT, ValueT, ProbeT]`. You
write three of the six public hooks, and the other three come with working defaults.
The class itself must also meet one requirement that is not a hook.

The base class raises `NotImplementedError` for each required hook:

- `probe(key)` returns a cheap, snapshot-safe fingerprint for change detection. It must
  work without `db`. The kernel probes a resource when a request needs that node
  verified. A request makes at most one standalone probe per resource key, and only for
  nodes it reaches.
- `load(db, key)` performs the I/O. The database applies raw-read allowance internally
  while it invokes resource hooks. The `db` a hook receives serves that allowance only,
  and reading back through it fails. `get`, `read_input`, `read_resource`, and every
  administrative and observational entry point raise `ReentrantDatabaseError` when a
  hook calls them (kernel-contract.md condition 2).
- `label(key)` returns a human-readable string for provenance display.

Instances of the class must be snapshot-safe. Use a frozen dataclass whose fields are
themselves snapshot-safe, or a class whose `identity()` returns a snapshot-safe value.
The kernel enforces this. A resource that is neither is refused by the first `get()` of
a query that captures it, with `UnsupportedValueError` naming the resource, before any
hook runs.

You may override these inherited defaults:

- `read(db, key)` is the public read method. It delegates to
  `db.read_resource(self, key)`.
- `probe_and_load(db, key)` probes, then loads. Override it to observe the probe and
  value from one underlying state when separate calls could race.
- `identity()` returns the resource itself. Override it to declare snapshot-safe
  resource configuration when the instance is not snapshot-safe on its own.

The capture fingerprinter walks hook bodies the same way as query bodies. So a hook
that touches mutable module state is refused like a query, before it runs.

Reference: `_SourceTextResource` uses SHA-256 content hashing in `probe` for precise
invalidation beyond stat-based detection.

### Singletons and node keys

By convention, instantiate resources as **module-level singletons**:
`_FILES = _SourceTextResource()`, `_DIRECTORIES = DirectoryResource()`. The convention
gives fewer identity fingerprints and one obvious handle. Duplicates collapse without
it. Two equal instances of the same snapshot-safe resource map to one node, because the
node key is derived from the resource type, the `identity()` payload, and the parameter.

### Why resources?

Resources are how the kernel enforces tracked ambient reads (kernel-contract.md
condition 2). `probe_and_load` prevents torn observations, and `probe` keeps validation
cheap on the fast path. Resource configuration is part of the node key, so the resource
must be snapshot-safe.

## Conservative Resolution and Untracked Reads

From-scratch consistency is the kernel's primary guarantee. A caller cannot tell a
stale answer from a fresh one when it reads it, so the kernel chooses re-execution over
risky reuse. An integration that guesses wrong about reuse causes silent staleness that
breaks the soundness guarantee. Two principles keep that guarantee.

### Prefer conservative outcomes over optimistic reuse

When your integration cannot determine a dependency statically, return `ambiguous` or
`missing`. Never guess, because optimistic reuse risks from-scratch inconsistency.
Reference: `_resolve_workspace_module` returns `"ambiguous"` when multiple paths match
a module prefix.

### Mark unsupported cases as untracked

When a query depends on state the guard cannot intercept (dynamic behavior, time,
randomness, network state, subprocess output), call `db.report_untracked_read(reason)`.
The call prevents reuse and nothing more. The read stays untracked and as
nondeterministic as it was. The node re-executes on every request and never backdates,
so stale reuse cannot happen. Reference: `module_export_surface` marks dynamic
`__all__` as untracked.

## Comparison Policies

`@query(cutoff=fn)` maps a query result to a snapshot-safe comparison token. The kernel
backdates the node when two runs produce equal tokens. Cheapness is the reason to reach
for it, and it comes with one precondition: **the token must determine the value the
query returns.** Equal tokens must imply equal values.

The precondition is a correctness rule. The kernel stores the fresh snapshot before it
decides, then rolls `changed_at` back when the tokens match. So a token coarser than the
value it guards suppresses a real ripple instead of a false one. The query hands back
the new value while declaring that nothing changed. Every dependent that reads
position, byte offsets or whitespace out of it stays valid on the strength of that
declaration.

The kernel contract states the general form of that rule as **substitutivity**. It
allows a coarser policy at a price: from-scratch consistency then holds modulo the
equivalence the policy declares. The token rule above is the stronger of the two, so a
query that meets it owes no separate substitutivity argument (see
[condition 3](kernel-contract.md#conditions-for-from-scratch-consistency)).

For raw text this means **a query that returns a file's text takes no `cutoff=`.** Any
token you could write for it is some projection of the file, and every projection of a
file is coarser than the file.

Put the lossy projection in a query that *returns* the projection instead:

```python
@query
def source_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)[0]


@query
def import_statements_for_file(db: Database, path: str) -> tuple[ImportStatementPayload, ...]:
    tree = _try_parse(source_text(db, path))
    ...
```

An edit the projection does not carry re-runs both. `source_text` executes and answers
with what is on disk. `import_statements_for_file` re-parses and lands an equal payload.
The kernel backdates it on the value itself, under default equality and with no policy
at all. Everything downstream of the parse is reused, and nothing was told the file is
unchanged.

An edit the projection *does* carry gets none of that, even when it is comment-only.
The payload's line number is part of this projection. A comment inserted above the
statement whose line number the payload carries moves it, so the re-parse lands a
different payload. There is nothing to backdate, and the parse's consumers run again.
Judge by what the payload carries, whatever the edit looks like. A coarser payload buys
incrementality, and only for the edits the payload drops.

Backdating is the Salsa/Skyframe optimization that prevents false ripple when
recomputation yields a semantically equivalent result, and it is worth having. A
projection query earns it at the node where the projection *is* the result. There the
precondition above holds by construction.

## Cycle-Safe Traversal

When your integration traverses directory trees or recursive structures:

- Track a `visited` set of canonical (resolved) paths.
- Canonicalize through `ResolvedPathResource`, never through a raw `Path.resolve()`.
  Resolving a fully qualified path is an ambient read the guard cannot intercept
  (kernel contract, limitation 1). An untracked call records no dependency edge. After a
  symlink is retargeted, warm containment and visited-set decisions stay stale while a
  fresh database recomputes them. A path that is not fully qualified is refused
  outright.
- Check root containment before recursing to prevent escaping the workspace.
- Reference: `_collect_python_files` uses `visited_directories`, a tracked
  resolution read, and `_is_within_root` for safe traversal.

## Stable API Surface

Define the public boundary explicitly:

1. Add `__all__` to your integration module listing stable dataclass types,
   high-level entrypoints, and any payload/composition queries other integrations
   depend on at the query layer. `python_source.__all__` is the reference shape.
2. Add re-exports in `src/pyinc/integrations/__init__.py` for only those stable
   names.
3. Experimental helpers (payload queries, decode functions, internal utilities) stay
   importable from the submodule and are **not** re-exported from
   `pyinc.integrations`.

## Cross-Integration Composition

An integration can depend on queries defined in another integration module. The kernel
tracks these cross-integration calls as ordinary dependency edges. When the upstream
query's result changes, the downstream query is re-verified and re-executed as needed.

Rules:

- Cross-integration query imports must target public `@query` functions listed in the
  upstream module's `__all__`. Never import `_`-prefixed helpers from another
  integration module.
- Pure parsing primitives shared by multiple integrations belong behind a named
  interface in a dedicated internal module. For example, `requirement_evaluation`
  and `dependency_check` both use `_pep440`, and neither imports the other's
  private helpers.
- The importing integration gains an incremental dependency edge tracked by the runtime.
  The only wiring needed is calling `db.get()` on the imported query, or calling it
  directly inside another `@query`, which the kernel intercepts. This holds at the
  query layer only. A high-level entrypoint is not a query and is unavailable inside
  one ([composition](integration-contract.md#composition-and-experimental-helpers)).
  Compose with the payload query the entrypoint decodes, or call the entrypoint
  outside the query.
- Composition queries are public `@query` functions, and by design they are **not**
  re-exported from `pyinc.integrations`. They exist for query-layer use only and are
  not user-facing entrypoints.

Reference: `python_source` imports `environment_index` from `installed_packages`
and calls it during import resolution to classify non-workspace imports as `stdlib`,
`installed`, or `missing`.

## Testing

An integration needs three kinds of tests.

### Contract lock tests

Verify that `__all__` has not drifted and that experimental helpers stay out of the
re-exports. Reference: `test_package_namespace_exports_only_stable_api` in
`tests/test_python_source.py`.

### Mode-parametrized correctness tests

Verify results across `strict`, `checked`, and `fast` modes. Reference:
`test_file_analysis_reports_top_level_symbols_by_mode` in `tests/test_python_source.py`.

Verify backdating explicitly, against the property the payload claims instead of a
class of edits. An edit the payload does not carry lands an equal payload, backdates,
and leaves everything downstream reused. An edit it does carry lands a different
payload and re-runs those consumers. Both halves need a test. References:
`test_comment_only_edit_reuses_downstream_analysis` and
`test_comment_inserted_above_the_import_reruns_downstream_analysis` in the same file.

### From-scratch consistency tests

These are the gold standard. Compare incremental results against fresh-database
recomputation over a sequence of state changes. Reference:
`test_workspace_analysis_matches_fresh_recomputation_over_changes` in
`tests/test_python_source.py`.

## Checklist

A new integration needs:

- [ ] All public result types are `@dataclass(frozen=True)` with snapshot-safe fields
- [ ] All ambient reads go through resources or `db.report_untracked_read()`
- [ ] Payload queries return documented snapshot-safe payloads (`TypeAlias`-typed
      tuples, or a plain string for raw text), with explicit decode transformations
      where the public dataclass shape differs
- [ ] High-level entrypoints decode payloads into frozen dataclasses
- [ ] Custom resources are frozen dataclasses whose fields are themselves snapshot-safe
- [ ] Custom resources implement the required `probe`/`label`/`load` hooks, and override
      the inherited `read`/`probe_and_load`/`identity` defaults only where those
      defaults do not fit
- [ ] Resource instances are module-level singletons by convention. Equal instances of
      a snapshot-safe resource already share one node
- [ ] Uncertain resolution cases return conservative outcomes and avoid optimistic reuse
- [ ] Dynamic or unsupported cases call `db.report_untracked_read(reason)`
- [ ] Recursive traversal uses canonical visited sets and root containment checks
- [ ] Queries that return raw text carry no `cutoff=`
- [ ] Any `@query(cutoff=fn)` sits on a query whose token determines the value that
      query returns. A coarser comparison belongs on the payload query that returns
      the projection
- [ ] `__all__` lists only stable types and entrypoints
- [ ] `integrations/__init__.py` re-exports only the stable surface
- [ ] Contract lock test verifies `__all__` and that experimental helpers stay
      unexported
- [ ] Mode-parametrized correctness tests cover `strict`, `checked`, and `fast`
- [ ] From-scratch consistency test compares incremental vs fresh over edit sequences
- [ ] Every high-level entrypoint refuses a query body before any other work
- [ ] A test pins the comparison property on real edits: equal tokens imply an equal
      public payload, and an edit the payload does carry re-runs its consumers

## Canonical End-to-End Example: `calc`

`examples/calc/` is a consumer kept small on purpose. It exercises this whole pattern
end to end:

- A single shared `FileResource`.
- A parse layer whose payload drops comments and blank lines, so those edits never
  reach the evaluated results.
- Cross-file dependency tracking via `include`.
- Per-name incremental evaluation. Each `binding_expr` backdates, so unaffected
  `evaluate_name` nodes are reused.
- Structural cycle detection that avoids relying on the kernel's `CycleError`.
- Reconciliation of the emitted results to disk through the
  [`@action` layer](action-contract.md).

It is the recommended worked example. Read it alongside `python_source` when authoring
a new query graph or a file→file compiler. `tests/test_calc.py` holds the incremental,
provenance, and from-scratch assertions.
