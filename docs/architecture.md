# pyinc Architecture

## Kernel

`pyinc` is a pull-based incremental query runtime. The database stores query memos, inputs, and resources as revisioned node records with:

- a stable node key
- a frozen boundary snapshot
- a semantic digest
- dependency edges captured dynamically at runtime
- `changed_at` and `verified_at` revisions
- the last decision: `executed`, `reused`, or `backdated`

`Database.inspect(...)` returns the last recorded provenance tree for a query key, and `Database.explain(...)` formats that tree without changing the node's recorded decision just by inspecting it.

Evaluation is top-down. `db.get()` verifies dependencies first, then either reuses the memo or re-executes the query. If a re-executed query returns a semantically equal value, the record is **backdated** (also called **early cutoff**) so downstream nodes stay clean.

`Database(max_query_nodes=...)` bounds only query memo nodes via LRU at top-level request boundaries. Inputs and resources remain resident.

## Value Membrane

Values crossing cached boundaries are frozen snapshots.

- `strict`: expose frozen values directly.
- `checked`: expose thawed copies and verify that queries did not mutate them.
- `fast`: expose thawed copies without mutation checks.

Hidden reads are not allowed in the core. Raw `open()` inside a query raises `UntrackedReadError` unless the access is routed through a resource object or explicitly marked via `db.report_untracked_read(...)`.

The runtime also blocks raw ambient reads through `os.getenv`, `os.environ`, `os.listdir`, `os.scandir`, and `Path.iterdir` during query execution. Resource `probe`/`load` hooks run in an internal allow-scope so resource implementations can perform those reads safely.

Query definitions are also checked for ambient state. Immutable constants and explicit `Input`/resource/query handles are allowed; mutable closure or global data is rejected so memo reuse never depends on hidden Python object mutation.

Query identity includes the supported function definition payload, so ambient immutable captures contribute to the fingerprint instead of being ignored.

Resource node identity includes resource configuration. Built-in resources are snapshot-safe dataclasses, and custom resources must either be snapshot-safe themselves or expose an `identity()` payload for keying.

Mutable object graphs with shared or cyclic references are supported in v2.0.0 via the `FrozenGraph` / `FrozenRef` snapshot variants. `freeze` memoizes mutable containers by id; pure trees retain the v1 flat shape. `thaw` runs a two-pass allocate-then-fill so cycles and shared identity are preserved across the boundary.

## Durable Cache

`Database(store=...)` accepts any object satisfying the `ArtifactStore` protocol (`InMemoryArtifactStore` and `FileSystemArtifactStore` ship in `pyinc.store`). The kernel writes serialized snapshot bytes for every value crossing the membrane, keyed by its `fingerprint_snapshot` digest. Bytes are produced by `serialize_snapshot` and consumed by `deserialize_snapshot`; both round-trip the full snapshot grammar including `FrozenGraph` / `FrozenRef`. External tools may use this for cross-run sharing.

Cross-run cache reuse ships in v2.0.0 via `Database.save_checkpoint(store=None) -> str` and `Database.load_checkpoint(key, store=None)`: the former serialises all current node records (plus snapshot bytes and dependency edges) to the store and returns a content-addressed key prefixed with `"ck"`; the latter reads the manifest back, verifies declared input digests and resource probe hints, and pre-warms the record cache so that the next `db.get(query)` reuses the stored result without re-executing the query function. Inputs must be set before loading; stale or unverifiable checkpoint records are silently skipped and the affected queries re-execute, preserving from-scratch consistency.

## Action Layer

`pyinc.actions` is an additive, stdlib-only subpackage that reconciles the
immutable values a query computes into files on disk, **outside** query
evaluation. It does not touch `Database` evaluation internals: queries return
snapshot-safe desired-artifact descriptions, and a `FilesystemReconciler` consumes
a `DesiredArtifactSet` and applies create/update/delete decisions driven by
SHA-256 of the real output bytes, bounded by a per-action ownership manifest kept
in a state directory outside the output root. Because the new integrations
(`graphql_schema`, `detection_rules`) must declare desired artifacts, the layer
lives in the `pyinc` wheel below `pyinc.integrations` — not in `pyinc_tools`,
which would invert the dependency direction. The contract is documented in
`docs/action-contract.md`; the kernel exposes the read-only helper
`pyinc.is_query_active()` so reconciliation can refuse to run inside a query.

## Package Shape Today

`pyinc` exposes a stable kernel surface from the top-level package:

- `Database`, `Input`, and `@query` for the query runtime
- `FileResource`, `FileStatResource`, `EnvResource`, and `DirectoryResource` for tracked external reads
- value-boundary helpers such as `freeze`, `thaw`, `semantic_equal`, and `ValueAdapter`
- structured inspection via `InspectionNode`, `Database.inspect(...)`, `Database.inspect_fresh(...)`, and `Database.explain(...)`
- observability via `Database.dependency_graph()`, `Database.statistics()`, and `Database.query_profile()`
- push observers via `Database.observe(callback, query, *args, **kwargs)` returning a `Subscription`, with `QueryChangeEvent` payloads *(added in the v2 development cycle)*
- mutable graph support via `FrozenGraph` / `FrozenRef` and the byte-stable `serialize_snapshot` / `deserialize_snapshot` helpers *(v2.0.0)*
- content-addressed artifact storage via `ArtifactStore`, `InMemoryArtifactStore`, `FileSystemArtifactStore`, and `Database(store=...)`, plus the durable checkpoint API `Database.save_checkpoint(store=None)` / `Database.load_checkpoint(key, store=None)` for cross-run cache reuse *(v2.0.0)*

`pyinc.integrations` exposes the stable dataclass/result types and high-level entrypoints from the shipped integrations:

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
- `notebook` *(added in v2 development cycle; see `docs/integration-contract.md`)*

Low-level payload queries, decode helpers, and resource helpers remain module-local experimental helpers. The public integration boundary is the dataclass/result layer plus the documented high-level entrypoints in `docs/integration-contract.md`.

The repository also includes small examples under `examples/`, dedicated tests for kernel semantics and from-scratch consistency, and a separate consumer tooling layer under `pyinc_tools` for editor/watcher-facing behavior built on top of the stable kernel.

A reproducible benchmark + correctness harness lives in the top-level `bench/` package (`python -m bench.run --output-dir bench/results`). It exercises the kernel and the file-generating integrations across fixed scenarios, asserts every timed incremental result byte-for-byte against a fresh cache-free `Database`, and emits CSV plus a Markdown report generated from it. Its only extra dependency, `joblib`, lives in the `bench` optional-dependency group; nothing under `src/pyinc` imports it, so the shipped package stays zero-runtime-dependency.

## Cross-Integration Composition

Integrations can compose at the query layer by importing `@query` functions from other integration modules. The kernel's dependency tracking extends automatically across integration boundaries -- no special wiring is required. When an upstream integration's query result changes, downstream queries that depend on it are re-verified and re-executed through the normal red-green algorithm.

Currently, `python_source` composes with `installed_packages`: it imports the `environment_index` query to classify non-workspace imports as `stdlib`, `installed`, or `missing`. This means that if the installed package environment changes (e.g., a package is installed or removed), `python_source`'s import resolution results are automatically invalidated and recomputed on the next request.

Composition queries like `environment_index` are public `@query` functions exported in their module's `__all__`, but they are intentionally not re-exported from `pyinc.integrations`. They exist for query-layer composition between integrations, not as user-facing entrypoints.

## Scope

Version 1 targets:

- module-defined `@query` functions
- explicit `Input` leaves
- explicit file, env, and directory resources
- optional file metadata resources (`FileStatResource`) for stat-level dependencies
- explanation/provenance for reuse vs recompute
- inline package typing via `py.typed`
- narrow supported integrations for Python source analysis, symbol resolution, config inspection (TOML, JSON, XML, `.env`, CSV), requirements parsing and evaluation, installed package discovery, dependency validation, and deep module resolution

The v1 line is closed at v1.2.1. v2.0.0 resolves the items that were deferred from v1, except for schedulers and worker pools which remain out of scope:

- **notebook integration** — landed as `pyinc.integrations.notebook`
- **push observers in the kernel** — landed as `Database.observe(...)` returning a `Subscription`, with `QueryChangeEvent` payloads
- **arbitrary mutable object graphs across cached boundaries** — landed as `FrozenGraph` / `FrozenRef` snapshot variants
- **content-addressed artifact storage** — landed as the `ArtifactStore` protocol with `InMemoryArtifactStore` and `FileSystemArtifactStore` implementations, `Database(store=...)`, and the durable checkpoint API (`Database.save_checkpoint()` / `load_checkpoint()`) for cross-run node-record reuse

Schedulers and worker pools remain out of scope for v2.

Watcher loops, mirror workspaces, and LSP adapters belong to consumer tooling above the kernel. They can live in the repository, but they do not widen `src/pyinc`'s semver contract unless a concrete correctness gap forces a kernel change. The v1.2.0 additions — `textDocument/references` (workspace-wide reverse-reference index) and the threaded `PollingWorkspaceWatcher.start()` live polling mode — land entirely in `pyinc_tools` on top of stable `pyinc.integrations` entrypoints; `src/pyinc` is unchanged.
