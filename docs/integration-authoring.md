# Integration Authoring Guide

An integration is a domain-specific query graph built on the pyinc kernel. The kernel
provides revisions, dependency tracking, red-green verification, and backdating. The
integration provides domain types, query decomposition, and external-state resources.

This guide extracts the shared patterns from the shipped integrations, using
`pyinc.integrations.python_source` as the reference template and
`toml_config` / `requirements_txt` as smaller companion examples. Read
[kernel-contract.md](kernel-contract.md) for the soundness envelope and
[integration-contract.md](integration-contract.md) for the current public boundary.

## Three-Layer Query Structure

Integrations use a layered query architecture:

**Layer 1 -- Payload queries.** `@query`-decorated functions that read resources, parse
data, and return snapshot-safe payloads. These are the kernel-level cached nodes, and
they come in two kinds, separated by where a comparison policy may live:

- *Raw-text reads* return a file's text as a plain `str`. They are compared by exact
  equality and carry no comparison policy: a token coarser than the text would let the
  node serve new bytes while reporting that nothing changed (see *Comparison Policies*).
  Examples: `source_text` and `notebook_text`.
- *Projection payloads* return the parsed structure a consumer needs, usually as a
  tuple. An edit the projection does not carry cannot change it, so the node either
  re-parses to an equal payload -- which the kernel backdates on the value itself,
  under default equality -- or is reused behind a node that already did. Either way the
  consumers below stay valid and no comparison policy was declared anywhere. Examples:
  `imports_for_file`, `definitions_for_file`, and `notebook_cells_payload`.

**Layer 2 -- Composition queries.** Queries that call other queries and assemble richer
composite payloads. Example: `workspace_analysis_payload` calls `module_analysis_payload`
in a loop over discovered Python files.

**Layer 3 -- High-level entrypoints.** Non-query functions that call `db.get()` and
decode tuple payloads into frozen dataclasses. These are the public API. Examples:
`file_analysis` and `workspace_analysis`. They are called from outside a query: a
query body that reaches a high-level entrypoint is refused before the entrypoint
runs, raising `CompositionError` where the call is reached and
`UnsupportedValueError` where the kernel rejects what the query captured before
its body starts. Both derive from `PyIncError`.

**Why this layering?** The kernel caches and compares tuple payloads efficiently (they are
snapshot-safe and hashable by default). The decode layer converts to ergonomic dataclasses
only at the public boundary. Internal graph nodes stay cheap to hash and compare; the
external API stays user-friendly.

## Result Types

All public result types must be `@dataclass(frozen=True)` with snapshot-safe fields:

- Use snapshot-safe scalars, tuples, and nested frozen dataclasses such as
  `SourcePosition` and `SourceRange`.
- Use `tuple[T, ...]` instead of `list[T]` for collections (tuples are hashable and
  immutable).
- No `list`, `dict`, or `set` in result type fields.
- Reference: `ImportRef`, `PythonFileAnalysis`, and `PythonWorkspaceAnalysis`.

**Why?** The public dataclasses are decoded *after* `db.get()` returns the
cached tuple payload. Their frozen shape gives callers an immutable, typed
result without making arbitrary classes part of the snapshot contract. If a
dataclass itself crosses a cached boundary, `freeze` stores it as a
`FrozenRecord` and ordinary `thaw` returns a dictionary; preserving the
original class requires a matching `ValueAdapter`. The kernel ships such
adapters for its own resource snapshot types — a `FileStatResource` reading
arrives as a `FileStatSnapshot` in every mode — but an integration's own
dataclasses are ordinary user classes and follow the rule above.

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
boundary so UTF-8 byte columns are converted exactly once. Reference:
`ImportPayload`, `FileAnalysisPayload`, `_decode_import`, and
`_decode_file_analysis`.

**Why?** Tuple payloads are cheap for the kernel to cache and compare (see *Three-Layer
Query Structure*), and the `TypeAlias` makes the bidirectional conversion
self-documenting.

## Resources

All reads of external state inside a query must go through the Resource API. The kernel
intercepts `open()`, `os.getenv`, `os.listdir`, `os.scandir`, and `Path.iterdir` during
query execution and raises `UntrackedReadError` otherwise.

**Built-in resources:** `FileResource`, `BinaryFileResource`, `FileStatResource`,
`EnvResource`, `DirectoryResource`, and `ResolvedPathResource` cover common cases.

**Custom resources:** When built-in resources do not fit, define a custom resource as a
frozen dataclass implementing the public `Resource[KeyT, ValueT, ProbeT]` hooks:

- `read(db, key)` -- public read method; delegates to `db.read_resource(self, key)`.
- `label(key)` -- returns a human-readable string for provenance display.
- `probe(key)` -- returns a cheap, snapshot-safe fingerprint for change detection.
  Must not require `db`. Called on every request.
- `load(db, key)` -- performs the actual I/O. The database applies raw-read allowance
  internally while invoking resource hooks.
- `probe_and_load(db, key)` -- observes the probe and value from one underlying state
  when separate calls could race.
- `identity()` -- optionally returns snapshot-safe resource configuration.

Reference: `_SourceTextResource` uses SHA-256 content hashing in `probe` for precise
invalidation beyond stat-based detection.

Instantiate resources as **module-level singletons**: `_FILES = _SourceTextResource()`,
`_DIRECTORIES = DirectoryResource()`.

**Why?** Resources are how the kernel enforces tracked ambient reads (kernel-contract.md
condition 2). `probe_and_load` prevents torn observations while `probe` keeps validation
cheap on the fast path.
Resource configuration is part of the node key, so the resource must be snapshot-safe.

## Conservative Resolution and Untracked Reads

Two principles for maintaining the soundness guarantee:

**Prefer conservative outcomes over optimistic reuse.** When your integration cannot
determine a dependency statically, return `ambiguous` or `missing` rather than guessing.
Optimistic reuse risks from-scratch inconsistency. Reference:
`_resolve_workspace_module` returns `"ambiguous"` when multiple paths match a module
prefix.

**Mark unsupported cases as untracked.** When a query depends on state the
guard cannot intercept — dynamic behavior, time, randomness, network state,
subprocess output — call `db.report_untracked_read(reason)`. Be clear about
what that buys: it does not make the read deterministic or tracked, it
prevents reuse. The node re-executes on every request and never backdates, so
stale reuse cannot happen; the read itself stays as nondeterministic as it
was. Reference: `module_export_surface` marks dynamic `__all__` as untracked.

**Why?** From-scratch consistency is the kernel's primary guarantee. Re-execution is
always safe; stale reuse is never safe. An integration that guesses wrong about reuse
causes silent staleness that violates the soundness envelope.

## Comparison Policies

`@query(cutoff=fn)` maps a query result to a snapshot-safe comparison token, and the
kernel backdates the node when two runs produce equal tokens. Cheapness is the reason
to reach for it, but it is not the precondition. **The token must determine the value
the query returns:** equal token has to imply equal value.

That precondition is not a style preference. The kernel stores the fresh snapshot
before it decides, then rolls `changed_at` back when the tokens match -- so a token
coarser than the value it guards does not suppress a false ripple, it suppresses a real
one. The query hands back the new value while declaring that nothing changed, and every
dependent that reads position, byte offsets or whitespace out of it stays valid on the
strength of that declaration.

The consequence for raw text is direct: **a query that returns a file's text takes no
`cutoff=`.** Any token you could write for it is some projection of the file, and no
projection of a file is as fine as the file.

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

A comment-only edit re-runs both. `source_text` executes and answers with what is on
disk. `import_statements_for_file` re-parses and lands an equal payload, which the
kernel backdates on the value itself, under default equality and with no policy at all
-- so everything downstream of the parse is reused, and nothing was told the file is
unchanged.

**Why?** Backdating is the Salsa/Skyframe optimization that prevents false ripple when
recomputation yields a semantically equivalent result, and it is worth having. A
projection query earns it at the node where the projection *is* the result, which is
where the precondition above holds by construction.

## Cycle-Safe Traversal

When your integration traverses directory trees or recursive structures:

- Track a `visited` set of canonical (resolved) paths.
- Canonicalize through `ResolvedPathResource`, never through a raw
  `Path.resolve()`: resolution is an ambient read the guard cannot intercept
  (kernel contract, limitation 1), so an untracked call records no dependency
  edge and a retargeted symlink leaves warm containment and visited-set
  decisions stale while a fresh database recomputes them.
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
   importable from the submodule but are **not** re-exported from
   `pyinc.integrations`.

## Cross-Integration Composition

An integration can depend on queries defined in another integration module. The kernel
tracks these cross-integration calls as ordinary dependency edges -- if the upstream
query's result changes, the downstream query is re-verified and re-executed as needed.

**Rules:**

- Cross-integration query imports must target public `@query` functions listed in the
  upstream module's `__all__`. Never import `_`-prefixed helpers from another
  integration module.
- Pure parsing primitives shared by multiple integrations belong behind a named
  interface in a dedicated internal module. For example, `requirement_evaluation`
  and `dependency_check` both use `_pep440` rather than importing one another's
  private helpers.
- The importing integration gains an incremental dependency edge tracked by the runtime.
  No special wiring is required beyond calling `db.get()` on the imported query (or
  calling it directly inside another `@query`, which the kernel intercepts). That is
  the query layer only. A high-level entrypoint is not a query and is not available
  inside one: a query body that reaches an entrypoint is refused before it runs,
  raising `CompositionError` where the call is reached and `UnsupportedValueError`
  where the kernel rejects what the query captured first. Compose with the payload
  query the entrypoint decodes, or call the entrypoint outside the query.
- Composition queries are public `@query` functions but are intentionally **not**
  re-exported from `pyinc.integrations`. They exist for query-layer use, not as
  user-facing entrypoints.

**Reference:** `python_source` imports `environment_index` from `installed_packages`
and calls it during import resolution to classify non-workspace imports as `stdlib`,
`installed`, or `missing`.

## Testing

Three categories of tests for an integration:

**Contract lock tests.** Verify that `__all__` has not drifted and that experimental
helpers are not re-exported. Reference:
`test_package_namespace_exports_only_stable_api` in `tests/test_python_source.py`.

**Mode-parametrized correctness tests.** Verify results across `strict`, `checked`, and
`fast` modes. Reference: `test_file_analysis_reports_top_level_symbols_by_mode` in
`tests/test_python_source.py`. Verify backdating explicitly: non-semantic edits should
trigger backdating and downstream reuse. Reference:
`test_comment_only_edit_reuses_downstream_analysis` in the same file.

**From-scratch consistency tests.** The gold standard: compare incremental results against
fresh-database recomputation over a sequence of state changes. Reference:
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
- [ ] Custom resources implement the public
      `read`/`label`/`probe`/`load`/`probe_and_load`/`identity` hooks as frozen dataclasses
- [ ] Resource instances are module-level singletons
- [ ] Uncertain resolution cases return conservative outcomes, not optimistic reuse
- [ ] Dynamic or unsupported cases call `db.report_untracked_read(reason)`
- [ ] Recursive traversal uses canonical visited sets and root containment checks
- [ ] Queries that return raw text carry no `cutoff=`
- [ ] Any `@query(cutoff=fn)` sits on a query whose token determines the value that
      query returns; a coarser comparison belongs on the payload query that returns
      the projection instead
- [ ] `__all__` lists only stable types and entrypoints
- [ ] `integrations/__init__.py` re-exports only the stable surface
- [ ] Contract lock test verifies `__all__` and non-export of experimental helpers
- [ ] Mode-parametrized correctness tests cover `strict`, `checked`, and `fast`
- [ ] From-scratch consistency test compares incremental vs fresh over edit sequences
- [ ] Every high-level entrypoint refuses a query body before any other work

## Canonical End-to-End Example: `calc`

`examples/calc/` is a deliberately small consumer that exercises this whole
pattern end to end: a single shared `FileResource`, a parse layer whose payload
drops comments and blank lines so those edits never reach the evaluated
results, cross-file dependency tracking via `include`, per-name incremental
evaluation (each `binding_expr` backdates so unaffected `evaluate_name` nodes
are reused), structural cycle detection that avoids relying on the kernel's
`CycleError`, and reconciliation of the emitted results to disk through the
[`@action` layer](action-contract.md). It is the recommended worked example to
read alongside `python_source` when authoring a new query graph or a file→file
compiler. See `tests/test_calc.py` for the incremental, provenance, and
from-scratch assertions.
