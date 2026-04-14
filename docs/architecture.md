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

## Package Shape Today

`pyinc` exposes a stable kernel surface from the top-level package:

- `Database`, `Input`, and `@query` for the query runtime
- `FileResource`, `FileStatResource`, `EnvResource`, and `DirectoryResource` for tracked external reads
- value-boundary helpers such as `freeze`, `thaw`, `semantic_equal`, and `ValueAdapter`
- structured inspection via `InspectionNode`, `Database.inspect(...)`, and `Database.explain(...)`

`pyinc.integrations` exposes the stable high-level surfaces from the four shipped integrations:

- `python_source` for narrow Python source and workspace-local module analysis
- `toml_config` for narrow `pyproject.toml` inspection
- `requirements_txt` for narrow `requirements.txt` inspection
- `installed_packages` for installed package discovery, stdlib module identification, and import name resolution

Low-level payload queries, decode helpers, and resource helpers remain module-local experimental helpers. The public integration boundary is the dataclass/result layer plus the documented high-level entrypoints in `docs/integration-contract.md`.

The repository also includes small examples under `examples/` plus dedicated tests for kernel semantics, property-based from-scratch consistency, and each shipped integration.

## Scope

Version 1 targets:

- module-defined `@query` functions
- explicit `Input` leaves
- explicit file, env, and directory resources
- optional file metadata resources (`FileStatResource`) for stat-level dependencies
- explanation/provenance for reuse vs recompute
- inline package typing via `py.typed`
- narrow supported integrations for Python source analysis, TOML config inspection, requirements parsing, and installed package discovery

Version 1 does not include:

- notebook integration
- push observers
- schedulers or worker pools
- content-addressed artifact storage
- arbitrary mutable object graphs across cached boundaries
