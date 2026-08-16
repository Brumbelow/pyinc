# pyinc Architecture

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

Hidden reads are not allowed in the core. Raw `open()` inside a query raises
`UntrackedReadError` unless the access is routed through a `Resource`.
`db.report_untracked_read(reason)` does not lift that guard: it exists for
reads the guard cannot intercept — `os.open`, C extensions, subprocesses,
time, randomness — and declaring one marks the query node always-re-executing,
so its memo is never reused and never backdated.

The runtime also blocks raw ambient reads through `os.getenv`, `os.environ`,
`os.listdir`, `os.scandir`, and `Path.iterdir` during query execution.
Resource `probe`/`load` hooks run in an internal allow-scope so resource
implementations can perform those reads safely.

Query definitions are also checked for ambient state. Immutable constants and
explicit `Input`/resource/query handles are allowed; mutable closure or global
data is rejected, so a memo is reused against the definitions the fingerprint
folded rather than against whatever a mutable object happens to hold now. Two
shapes still leave a cached result depending on Python state no fold observed
— a chain that lands on a class or a frozen dataclass instance, and a patched
standard-library callable — and the
[kernel contract](kernel-contract.md#explicit-limitations) states both.

Query identity is based on a stable key and a canonical typed encoding of the
complete supported function definition. It includes code objects and nested
constants, immutable and transitive captures, defaults, equality/cutoff
policies, resource implementations, and relevant interpreter/build flags. It
does not depend on marshal reference-table behavior. Registered adapters are
not part of it: their implementations and configuration are checkpoint
identity, and configuration drift is caught in-process by the request-scope
check instead, adapter by adapter, for every adapter whose configuration could
be digested at construction.

Resource node identity includes configuration plus the implementations of
`probe`, `load`, `probe_and_load`, and `identity`. The public generic
`Resource[KeyT, ValueT, ProbeT]` contract exposes those hooks; callers use
`Database.read_resource` instead of a private database method.

Mutable object graphs with shared or cyclic references are supported via the
`FrozenGraph` / `FrozenRef` snapshot variants. `freeze` memoizes mutable
containers by id; pure trees retain the flat snapshot shape. `thaw` runs a
two-pass allocate-then-fill so cycles and shared identity are preserved across
the boundary.

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
kernel version, identities, input keys, dependency references, duplicates,
types, and content addresses. Records whose live code/resources no longer
match miss safely; structurally malformed or foreign manifests raise a typed
checkpoint error without partially warming the database.

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
  returning a `Subscription`, with `QueryChangeEvent` payloads
- mutable graph snapshots (`FrozenGraph`, `FrozenRef`) and the
  `serialize_snapshot` / `deserialize_snapshot` helpers, described in
  [Value Membrane](#value-membrane) and [Durable Cache](#durable-cache)
- content-addressed artifact stores and the durable checkpoint API for
  cross-run cache reuse, described in [Durable Cache](#durable-cache)
- declared-output reconciliation via the `@action` layer (`Output`,
  `ReconcileResult`, `Action.reconcile`/`plan`): the complete cycle is
  cross-process locked, path/manifest trust is prevalidated, files publish
  atomically, and the schema-v2 ledger is published last; see
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
distributed execution, and interception of every possible ambient read remain
out of scope. Custom equality policy purity and `fast`-mode mutation hazards
remain caller contracts.

Python analysis is conservative and declaration-driven, not a full type checker
or formatter. Unsupported attribute shapes produce no navigation/refactoring
result rather than a guess. Remote JSON Schema references, combinators,
conditionals, general validation, and full JSON Schema coverage likewise remain
outside the code generator's deliberately narrow subset.

Watcher loops, mirror workspaces, protocol-position conversion, and
LSP/JSON-RPC adapters belong to `pyinc_tools`; the shared code-point geometry
contract lives in `pyinc.integrations`. JSON Schema analysis belongs to
`pyinc_codegen`. Neither consumer widens the domain-agnostic kernel contract.
