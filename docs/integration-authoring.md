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

**Layer 1 -- Payload queries.** `@query`-decorated functions that return
snapshot-safe payloads -- typically tuples, though a raw-text query such as
`source_text` returns a plain `str`. These are the kernel-level cached nodes. They
read resources, parse data, and return simple hashable structures. Examples include
`source_text`, plus `imports_for_file` and `definitions_for_file`, which return
tuple payloads.

**Layer 2 -- Composition queries.** Queries that call other queries and assemble richer
composite payloads. Example: `workspace_analysis_payload` calls `module_analysis_payload`
in a loop over discovered Python files. Any Layer-2 query intended for composition by
another module is a stable module-level API listed in the defining module's `__all__`;
it is not re-exported from the aggregate `pyinc.integrations` namespace.

**Layer 3 -- High-level entrypoints.** Non-query functions that call `db.get()` and
decode tuple payloads into frozen dataclasses. These are the public API. Examples:
`file_analysis` and `workspace_analysis`. They are top-level consumer boundaries,
not query-composition functions. Calling any Layer-3 entrypoint from a query raises
`QueryContextError` before arguments, paths, resources, or integration memos are
touched. Query bodies must compose the stable Layer-2 query APIs instead.

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
cached tuple payload. Their frozen shape gives callers a typed result that
rejects ordinary field assignment (not capability-level reflection) without
making arbitrary classes part of the snapshot contract. If a
dataclass itself crosses a cached boundary, `freeze` stores it as a
`FrozenRecord` and ordinary `thaw` returns a dictionary; preserving the
original class requires a matching `ValueAdapter`.

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
The file resources accept regular files only: they open nonblockingly, validate
the opened descriptor with `fstat`, and refuse FIFOs, sockets, devices, and
directories with their ordinary missing-file semantics. This descriptor-first
boundary prevents a discovered path from becoming a blocking special file
between a pathname check and the read.

**Custom resources:** When built-in resources do not fit, subclass the public
`Resource[KeyT, ValueT, ProbeT]`. The only hooks without usable base
implementations are `label`, `probe`, and `load`:

- `label(key)` -- required; returns a human-readable provenance label.
- `probe(key)` -- required; returns a cheap, snapshot-safe change fingerprint
  and does not receive `db`. It runs only when a requested resource node needs
  verification, not once for every database request.
- `load(db, key)` -- required; performs the actual I/O. The database applies
  raw-read allowance internally while invoking resource hooks.
- `read(db, key)` -- inherited default delegates to
  `db.read_resource(self, key)`; override only to normalize a public key.
- `probe_and_load(db, key)` -- inherited default calls `probe` then `load`;
  override to observe both from one underlying state when separate calls race.
- `identity()` -- inherited default returns `self`; override to expose a
  different snapshot-safe configuration identity.

Every hook observes external state directly. A hook must not call a `Database`
observation API or read an `Input`, query, or another resource, even through a
different database. Compose those managed values in the `@query` that calls
`read`; otherwise all three execution modes raise `ResourceDependencyError`.
This restriction does not prevent direct file, environment, or other external
I/O inside the state-observation hooks. A hook may invoke a supported external
command when its probe and value fully describe the observation, but it may not
launch worker threads, executors, multiprocessing workers, or fork the live
database process. Those operations raise `QueryConcurrencyError` before work
starts, in every mode.

Reference: `_SourceTextResource` uses SHA-256 content hashing in `probe` for precise
invalidation beyond stat-based detection.

Module-level singleton instances such as `_FILES = _SourceTextResource()` are a
useful allocation and naming convention, not a semantic requirement. Distinct
instances with the same complete identity describe the same resource node.

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
or native-extension I/O — call `db.report_untracked_read(reason)`. Be clear
about what that buys: it does not make the read deterministic or tracked, it
prevents reuse. The node re-executes on every request and never backdates, so
stale reuse cannot happen; the read itself stays as nondeterministic as it was.
External command output is different: common process-launch APIs are rejected
in a query, so observe it through a Resource hook. Reference:
`module_export_surface` marks dynamic `__all__` as untracked.

**Why?** From-scratch consistency is the kernel's primary guarantee. Preventing
memo reuse removes one source of staleness, but re-executing a nondeterministic
read is not equivalent to tracking it and may differ from a separately timed
fresh evaluation. An integration that guesses wrong about reuse causes silent
staleness that violates the soundness envelope.

## Exact Raw Boundaries and Semantic Payloads

A query that returns raw `str` or `bytes` must use the kernel's default exact,
typed equality. Never attach a parser-shaped cutoff to raw text: any caller may
observe comments, whitespace, ordering, ranges, or spelling, so two different
strings are not substitutable even when today's parser ignores the difference.

Put semantic backdating on a separate parsed query whose returned payload is the
complete observable value. Default equality is usually sufficient:

```python
@query
def source_text(db: Database, path: str) -> str:
    return FILES.read(db, path)


def _source_payload(source: str) -> tuple[str, str]:
    try:
        return ("ast", ast.dump(ast.parse(source)))
    except SyntaxError:
        return ("source", source)


@query
def source_structure(db: Database, path: str) -> tuple[str, str]:
    return _source_payload(source_text(db, path))
```

On a comment-only edit, `source_text` executes and publishes the new exact text;
`source_structure` executes and backdates when its complete payload is equal;
consumers of `source_structure` can then be reused. This example deliberately
returns an AST dump without position attributes. If its public payload exposed
`lineno`, `end_lineno`, offsets, or source ranges, a preceding comment or blank
line that shifts those positions would not be equivalent and the payload would
have to retain them.

If a parsed query does use `cutoff=`, it carries the stronger law: equal tokens
must imply equal complete public query results for every accepted input. A
token that represents only some fields, positions, or ordering is not a valid
cutoff for a richer payload. Every shipped cutoff needs a property or
adversarial test of `equal token => equal public payload`, including geometry
and nested values.

**Why?** Backdating is sound only at a substitutive boundary. Separating exact
bytes from semantic structure makes that law local and keeps arbitrary raw-text
consumers correct while still preventing false ripple after parsing.

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
   high-level entrypoints and result/shared types. Layer-2 query handles stay on
   their defining module so the two composition tiers cannot be confused.
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
- Never call an aggregate or submodule Layer-3 entrypoint from a query. The uniform
  `QueryContextError` is a contract boundary, not a fallback composition mechanism;
  import the corresponding stable Layer-2 query handle from its defining module.
- Pure parsing primitives shared by multiple integrations belong behind a named
  interface in a dedicated internal module. For example, `requirement_evaluation`
  and `dependency_check` both use `_pep440` rather than importing one another's
  private helpers.
- The importing integration gains an incremental dependency edge tracked by the runtime.
  No special wiring is required beyond calling `db.get()` on the imported query (or
  calling it directly inside another `@query`, which the kernel intercepts).
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
`tests/test_python_source.py`. Verify both halves explicitly: non-semantic edits
must execute exact raw queries, then equal complete parsed payloads may backdate
and permit downstream reuse. Reference:
`test_trailing_comment_executes_exact_source_and_backdates_equal_analysis` in the same file.

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
- [ ] Custom resources implement required `label`/`probe`/`load` hooks and
      override inherited `read`/`probe_and_load`/`identity` only when needed
- [ ] Resource configuration is snapshot-safe; singleton instances are optional
- [ ] Resource hooks read external state only; managed Input/query/resource
      composition lives in a query
- [ ] Uncertain resolution cases return conservative outcomes, not optimistic reuse
- [ ] Dynamic or unsupported cases call `db.report_untracked_read(reason)`
- [ ] Recursive traversal uses canonical visited sets and root containment checks
- [ ] Raw `str`/`bytes` queries use default exact typed equality, never a semantic cutoff
- [ ] Parsed queries expose complete substitutive payloads; any custom cutoff obeys
      equal-token implies equal-public-payload
- [ ] `__all__` lists only stable types and entrypoints
- [ ] `integrations/__init__.py` re-exports only the stable surface
- [ ] Every Layer-3 entrypoint rejects query-time use before normalization, reads,
      decoding, or memoization; query composition uses listed Layer-2 query handles
- [ ] Contract lock test verifies `__all__` and non-export of experimental helpers
- [ ] Mode-parametrized correctness tests cover `strict`, `checked`, and `fast`
- [ ] From-scratch consistency test compares incremental vs fresh over edit sequences

## Canonical End-to-End Example: `calc`

`examples/calc/` is a deliberately small consumer that exercises this whole
pattern end to end: a single shared `FileResource`, exact raw text, a complete
parse payload that backdates on comment/whitespace edits, cross-file dependency tracking
via `include`, per-name incremental evaluation (each `binding_expr` backdates so
unaffected `evaluate_name` nodes are reused), structural cycle detection that
avoids relying on the kernel's `CycleError`, and reconciliation of the emitted
results to disk through the [`@action` layer](action-contract.md). It is the
recommended worked example to read alongside `python_source` when authoring a
new query graph or a file→file compiler. See `tests/test_calc.py` for the
incremental, provenance, and from-scratch assertions.
