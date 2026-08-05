# pyinc Architecture

The pull-based graph algorithm is established prior work: top-down dependency
verification and equal-result backdating are documented in Salsa, while
demand-driven repair predates it in Adapton. pyinc's proposed contribution is
the integrated Python-specific assurance envelope around that algorithm. See
[Related Work and Positioning](related-work.md) for dated, pinned primary
sources and the scoped comparison.

## Kernel

`pyinc` is a pull-based incremental query runtime. The database stores query
memos, inputs, and resources as revisioned node records with:

- a stable node key
- a frozen boundary snapshot
- a semantic digest
- dependency edges captured dynamically at runtime
- `changed_at` and `verified_at` revisions
- the last decision: `executed`, `reused`, `backdated`, or `failed` (a resource
  whose `load` raised)

`Database.inspect(...)` returns the last recorded provenance tree for a query
key, and `Database.explain(...)` formats that tree; inspection never changes a
node's recorded decision.

Evaluation is top-down. `db.get()` verifies dependencies first, then either
reuses the memo or re-executes the query. If a re-executed query returns a
semantically equal value, the record is **backdated** (also called **early
cutoff**) so downstream nodes stay clean.

`Database(max_query_nodes=...)` bounds only query memo nodes via LRU at
top-level request boundaries. Inputs and resources remain resident.

## Value Membrane

Values crossing cached boundaries are frozen snapshots.

- `strict`: expose frozen values directly.
- `checked`: expose thawed copies and verify that queries did not mutate them.
- `fast`: expose thawed copies without mutation checks.

Mappings lose ordinary Python insertion order at this membrane. Their entries
are ordered by canonical frozen-key fingerprint, so strict `FrozenDict`
views and checked/fast thawed dictionaries all iterate in the same canonical
order after fresh execution, warm reuse, or checkpoint reload. Use an ordered
tuple of pairs or an explicit adapter when order is semantic.

Hidden reads are not allowed in the core. Raw `open()` inside a query raises
`UntrackedReadError` unless the access is routed through a `Resource`.
`db.report_untracked_read(reason)` does not lift that guard: it exists for
reads the guard cannot intercept — `os.open`, C-extension I/O, time, and
randomness — and declaring one marks the query node always-re-executing, so its
memo is never reused and never backdated.

The runtime also blocks raw ambient reads through `os.getenv`, `os.environ`,
`os.listdir`, `os.scandir`, and `Path.iterdir` during query execution.
Resource `probe`/`load` hooks run in an internal allow-scope so resource
implementations can perform those external reads safely. That allow-scope does
not make database-managed reads legal: `identity`, `label`, `probe`, `load`,
and `probe_and_load` may not call a `Database` read or compose an `Input`,
query, or another resource. Such composition belongs in the reading query and
raises `ResourceDependencyError` from a hook in every execution mode.

Queries execute synchronously. The runtime rejects its enumerated thread,
executor, multiprocessing, fork, and external-command launch entry points with
`QueryConcurrencyError` before work starts; catching the first error cannot
publish the enclosing query. Resource hooks may run an external command as
tracked I/O, but may not launch workers or fork the live database process.
Concurrency started outside `db.get()` remains available.

Query definitions also undergo static capture analysis. Immutable constants and
explicit `Input`/resource/query handles discovered from direct global/nonlocal
references are allowed; directly captured mutable closure or global data is
rejected. This is not runtime namespace tracing: `globals()[name]`, dynamic
`getattr`/`vars`, `eval`/`exec`, runtime imports, and similar reflection can
reach state outside the analyzed definition. Such state must use an
`Input`/`Resource` or be declared with `report_untracked_read()` before access.
Callable objects that expose `__wrapped__` are rejected as captures,
equality/cutoff policies, and state-observation resource hooks: unlike an
ordinary decorated Python function or bound method, that attribute does not
identify the object's `__call__` implementation or instance state.

Query identity is based on a stable key and a canonical typed encoding of the
complete supported static function definition. It includes code objects and
nested constants, statically discovered immutable and transitive captures,
defaults, equality/cutoff policies, resource implementations, and relevant
interpreter/build flags. Every directly captured global/nonlocal and directly
accessible default, reflected annotation, or function attribute also carries a
process-local site incarnation because Python code can observe object identity;
this includes captured `Input`, `Query`, `Resource`, function, module, method,
and type handles. Cross-process checkpoint readers conservatively execute those
queries. Capture-free queries can still reuse explicit query-argument calls
across processes. A
registered `ValueAdapter` is pinned as a `Database` lifetime invariant and in
checkpoint metadata; its configuration is not ordinary query identity. Query
identity does not depend on marshal reference-table behavior.

The complete definition fingerprint is recomputed once per request before a
stored query identity can be selected or reused. Repeated uses inside that
request share only the final digest; no live-definition fingerprint cache entry
crosses the request boundary. A `request_span` is one such declared-stable
request, and `request_inputs_changed()` rolls it forward and clears the
request-local digest cache.

Resource node identity includes configuration plus the implementations of
`probe`, `load`, `probe_and_load`, and `identity`. The public generic
`Resource[KeyT, ValueT, ProbeT]` contract exposes those hooks; callers use
`Database.read_resource` instead of a private database method.

Mutable object graphs with shared or cyclic references are supported via the
`FrozenGraph` / `FrozenRef` snapshot variants. `freeze` memoizes mutable
containers by id; pure trees retain the flat snapshot shape and avoid that
graph envelope, while still paying the ordinary deep-freeze traversal and
allocation cost. `thaw` runs a two-pass allocate-then-fill so cycles and shared
identity are preserved across the boundary.

## Durable Cache

`Database(store=...)` accepts any object satisfying the `ArtifactStore`
protocol (`InMemoryArtifactStore` and `FileSystemArtifactStore` ship in
`pyinc.store`). The kernel writes serialized snapshot bytes for every value
crossing the membrane, keyed by an internally derived content digest. Bytes are
produced by the public `serialize_snapshot` and consumed by
`deserialize_snapshot`; the encoding is byte-stable and both round-trip the
full snapshot grammar including `FrozenGraph` / `FrozenRef`. The digest helper
is not public API: callers use the artifact-store and checkpoint operations
instead of manufacturing kernel store keys.

`Database.save_checkpoint(store=None) -> str` serializes current node records,
snapshot addresses, and dependency edges to a content-addressed key prefixed
with `"ck"`. `Database.load_checkpoint(key, store=None)` accepts manifest
schema v7 only and validates the entire manifest before staging any record:
kernel version, execution mode, identities, input keys, dependency references,
duplicates, types, and content addresses. Cross-mode loads are rejected rather
than sharing values across different boundary semantics. Records whose live
code or resources no longer match miss safely; structurally malformed or
foreign manifests raise a typed checkpoint error without partially warming the
database.

Schema v7 also excludes manifests written before resource-hook dependencies
were rejected. Those manifests can omit a managed read performed by a hook and
therefore cannot be trusted even when their probe hint still matches.

Checkpoint publication does not trust the store's `contains` presence
optimization. Save performs the required idempotent `put` for every referenced
object so an address preseeded with different bytes is rejected before a new
manifest can claim a self-contained checkpoint.

`FileSystemArtifactStore` accepts only digest-shaped keys and serializes each
digest across processes. It flushes a same-directory temporary file before
atomic publication and refuses conflicting bytes.

## Package shape

### Kernel surface (`pyinc`)

The top-level package exposes a stable kernel surface:

- `Database`, stable keyed `Input`, public `Query`, and `@query` for the
  query runtime
- public generic `Resource`, `Database.read_resource`, `FileResource`,
  `BinaryFileResource`, `FileStatResource`, `EnvResource`,
  `DirectoryResource`, and `ResolvedPathResource` for tracked external reads
- value-boundary helpers such as `freeze`, `thaw`, `semantic_equal`, and
  `ValueAdapter`
- structured inspection via `InspectionNode`, `Database.inspect(...)`,
  `Database.inspect_fresh(...)`, and `Database.explain(...)`
- observability via `Database.dependency_graph()`, `Database.statistics()`,
  and `Database.query_profile()`
- push observers via `Database.observe(callback, query, *args, **kwargs)`
  returning an identity-scoped `Subscription`, with event-time recipient
  snapshots and `QueryChangeEvent` payloads only for changes from a prior value
- mutable graph snapshots (`FrozenGraph`, `FrozenRef`) and the
  `serialize_snapshot` / `deserialize_snapshot` helpers, described in
  [Value Membrane](#value-membrane) and [Durable Cache](#durable-cache)
- content-addressed artifact stores and the durable checkpoint API for
  cross-run cache reuse, described in [Durable Cache](#durable-cache)
- declared-output reconciliation via the `@action` layer (`Output`,
  `ReconcileResult`, `Action.reconcile`/`plan`): the complete cycle is
  cross-process locked, path/manifest trust is prevalidated, files publish
  atomically, and the schema-v3 ledger is published last; see
  [action-contract.md](action-contract.md)

### Integrations (`pyinc.integrations`)

`pyinc.integrations` exposes the stable dataclass/result types and high-level
entrypoints from the shipped integrations. Its shared public infrastructure
includes `SourcePosition`, `SourceRange`, `DocumentMap`, `PositionEncoding`,
`SymbolId`, `Scope`, `Binding`, `ScopeTree`, `scope_tree`, and `symbol_at`:

- `python_source`
- `toml_config`
- `requirements_txt`
- `installed_packages`
- `json_config`
- `dependency_check`
- `env_file`
- `xml_config`
- `csv_data`
- `deep_module_resolution`
- `requirement_evaluation`
- `symbol_resolution`
- `notebook`

Low-level payload queries, decode helpers, and resource helpers remain
module-local experimental helpers. The public integration boundary is the
dataclass/result layer plus the documented high-level entrypoints in
[integration-contract.md](integration-contract.md).

### Consumer layers and supporting material

Two consumer layers build on the stable kernel: `pyinc_tools` for
editor/watcher-facing behavior, and `pyinc_codegen`, a JSON-Schema →
typed-Python compiler. Both consumers use only public `pyinc` and
`pyinc.integrations` contracts. Within `pyinc_tools`, `WorkspaceSession`
remains the cohesive, lock-owning tools façade; internal modules separately
own document geometry, analysis/resolution, edit generation, workspace
mirroring/watching, and JSON-RPC framing. The [Scope](#scope) section states
which responsibilities belong to which layer.

The repository also includes small examples under `examples/` and dedicated
tests for kernel semantics and from-scratch consistency. The include-aware
`calc` fixture under `examples/calc/` is the canonical worked example of a
small query graph that reconciles outputs to disk.

A reproducible benchmark + correctness harness lives under `bench/` (not
shipped in the wheel); it exercises the kernel, calc, codegen, and action
targets across a canonical edit sequence and pairs every timing with an
incremental-equals-fresh correctness assertion. Its only comparison
dependency, `joblib`, sits in the `bench` optional-dependency group and is
never imported by runtime packages.

## Cross-Integration Composition

Integrations can compose at the query layer by importing `@query` functions
from other integration modules. The kernel's dependency tracking extends
automatically across integration boundaries -- no special wiring is required.
When an upstream integration's query result changes, downstream queries that
depend on it are re-verified and re-executed through the normal red-green
algorithm.

As a worked example, `python_source` composes with `installed_packages`: it
imports the `environment_index` query to classify non-workspace imports as
`stdlib`, `installed`, or `missing`. If the installed package environment
changes (e.g., a package is installed or removed), `python_source`'s import
resolution results are automatically invalidated and recomputed on the next
request. `deep_module_resolution` and `dependency_check` compose with
`installed_packages` the same way. The stable surface and composition boundary
are summarized in the [integration contract](integration-contract.md#composition-and-experimental-helpers).

Composition queries like `environment_index` are public `@query` functions
exported in their module's `__all__`, but they are intentionally not
re-exported from `pyinc.integrations`. They exist for query-layer composition
between integrations, not as user-facing entrypoints.

## Scope

Version 3 is a synchronous, serialized `Database` with explicit keyed inputs,
queries, and resources. Built-in query scheduling, worker pools, async queries,
distributed execution, and universal interception of arbitrary native reads or
worker launches remain out of scope. Common Python launch APIs are rejected
during query execution; this is an enumerated guard, not a capability sandbox.
Custom equality policy purity and `fast`-mode mutation hazards remain caller
contracts.

Python analysis is conservative and declaration-driven, not a full type checker
or formatter. Unsupported attribute shapes produce no navigation/refactoring
result rather than a guess. Remote JSON Schema references, combinators,
conditionals, general validation, and full JSON Schema coverage likewise remain
outside the code generator's deliberately narrow subset.

Watcher loops, mirror workspaces, protocol-position conversion, and
LSP/JSON-RPC adapters belong to `pyinc_tools`; the shared code-point geometry
contract lives in `pyinc.integrations`. JSON Schema analysis belongs to
`pyinc_codegen`. Neither consumer widens the domain-agnostic kernel contract.
