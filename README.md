# pyinc

[![CI](https://github.com/Brumbelow/pyinc/actions/workflows/ci.yml/badge.svg)](https://github.com/Brumbelow/pyinc/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/pyinc)](https://pypi.org/project/pyinc/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyinc)](https://pypi.org/project/pyinc/)
[![PyPI license](https://img.shields.io/pypi/l/pyinc)](https://pypi.org/project/pyinc/)
[![Lint: Ruff](https://img.shields.io/badge/lint-Ruff-D7FF64.svg)](https://docs.astral.sh/ruff/)

**Correct incremental recomputation for Python tools.**

Python tools that repeatedly analyze a workspace or regenerate files usually
face a bad choice: rerun everything after every edit, or maintain a collection
of caches and invalidation rules that can silently serve stale results.

`pyinc` provides a third option. Declare keyed inputs, tracked resources, and
pure query functions. It records the dependencies your code actually uses,
reuses unaffected work, and stops changes from propagating when recomputation
produces the same semantic result.

It is an embeddable, Salsa-style incremental computation engine for Python
developer tools: linters, language servers, source indexers, code generators,
repository analyzers, and small compilers.

`pyinc` is pure Python, stdlib-only, and has zero runtime dependencies. Python
3.11–3.14 are tested on Linux, macOS, and Windows.

```console
python -m pip install pyinc
```

## Quick start

Queries call one another as ordinary Python functions. `pyinc` captures the
resulting dependency graph while they run.

```python docs-check
from pyinc import Database, Input, query

NAMES = Input[tuple[str, ...]]("example.names")


@query
def normalized_names(db: Database) -> tuple[str, ...]:
    return tuple(sorted({name.strip() for name in NAMES.read(db)}))


@query
def rendered_names(db: Database) -> str:
    return "".join(f"- {name}\n" for name in normalized_names(db))


db = Database(mode="strict")
db.set(NAMES, (" Grace ", "Ada", "Ada"))
assert db.get(rendered_names) == "- Ada\n- Grace\n"

# The input changes, so normalized_names runs again. Its semantic result does
# not change, so it is backdated and rendered_names remains valid.
db.set(NAMES, ("Grace", "Ada"))
assert db.get(rendered_names) == "- Ada\n- Grace\n"

assert db.inspect(normalized_names).last_decision == "backdated"
assert db.inspect(rendered_names).last_decision == "reused"
```

The caller did not clear a cache or list the affected functions.
`normalized_names` recorded its dependency on `NAMES`, and `rendered_names`
recorded its dependency on `normalized_names`. When the changed input produced
the same normalized value, early cutoff prevented the change from reaching the
renderer.

For files, environment variables, and directories, use a `Resource` instead of
reading ambient state directly inside a query. Common hidden reads are rejected
so they cannot silently make a memo stale.

## Where pyinc fits

`pyinc` is most useful when an application repeatedly asks related questions
over changing inputs and the work naturally decomposes into deterministic
stages.

| Strong fit | Example query graph |
|---|---|
| Linters and static analyzers | file → syntax → symbols → cross-file diagnostics |
| DSL compilers and language servers | document → parse → resolve → navigate/diagnose |
| Schema and API code generators | schema → semantic model → generated modules and docs |
| Repository policy tools | source/config → dependency graph → violations and reports |
| Documentation and configuration compilers | source/includes → normalized model → output files |
| Long-lived development daemons | changed resources → affected analysis → observable result |

It is usually not the right tool for distributed workflows, arbitrary
side-effect orchestration, network-heavy queries, large mutable numerical
objects without adapters, or a one-shot command whose full run is already
cheap. See [Scope and limitations](#scope-and-limitations).

## A real file-to-file consumer

The distribution includes `pyinc_codegen`, a reference consumer built entirely
on the public `pyinc` API. It compiles a documented subset of JSON Schema into
typed Python models and per-definition documentation.

```python
from pyinc import Database
from pyinc_codegen import generate, generate_outputs

db = Database(mode="strict")

result = generate(db, "schema.json", "generated/")
print(result.created, result.updated, result.deleted)

# Preview the same validated reconciliation without changing the filesystem.
plan = generate_outputs.plan(db, "schema.json", root="generated/")
assert plan.dry_run
```

Its query graph is decomposed so different edits cause different amounts of
work:

| Schema change | Incremental result |
|---|---|
| Whitespace or key reordering | Canonical JSON input backdates; no downstream query runs and no generated file is rewritten. |
| Description-only edit | Only the affected documentation file changes. |
| Property type or requiredness edit | The affected model and documentation are updated; unrelated models remain valid. |
| Add a definition | Its files are created and the package index is updated. |
| Remove an unreferenced definition | Only its owned files are deleted and the index is updated. |
| Corrupt a generated file manually | The action layer repairs it without recomputing an unchanged query graph. |

This compiler is deliberately a worked example, not a claim of complete JSON
Schema support. Its supported subset and failure behavior are documented in the
[codegen guide](https://github.com/Brumbelow/pyinc/blob/main/docs/codegen-guide.md).

For a smaller implementation that can be read end to end, see the
[include-aware `calc` compiler](https://github.com/Brumbelow/pyinc/tree/main/examples/calc)
and [`calc_demo.py`](https://github.com/Brumbelow/pyinc/blob/main/examples/calc_demo.py).

## What pyinc guarantees

`pyinc` guarantees **from-scratch consistency**: incremental evaluation
produces the same result as a fresh evaluation on the same declared inputs and
resources, when all three conditions hold:

1. **Owned value boundaries.** Query arguments, query results, and `Input`
   values are snapshot-safe or handled by a registered `ValueAdapter`.
2. **Tracked ambient reads.** External state read by a query goes through a
   `Resource`; reads the guard cannot intercept are declared with
   `db.report_untracked_read(reason)`.
3. **Deterministic queries.** The same tracked dependencies produce a
   semantically equal result.

The guarantee is conditional by design. The
[kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md)
defines the exact value grammar, intercepted operations, execution modes,
checkpoint trust boundary, escape hatches, and explicit limitations.

## How it works

Evaluation is demand-driven and top-down:

1. `db.get(query, ...)` identifies the requested query node.
2. The database verifies the inputs, resources, and subqueries that node
   actually depended on during its previous execution.
3. If those dependencies remain valid, the memo is reused.
4. If a dependency changed, the affected query re-executes.
5. If its new value is semantically equal to its old value, the node is
   **backdated**. Its dependents remain valid and do not re-execute.

Dependency edges are captured dynamically. If a query takes a different branch
and reads a different dependency, a successful execution rewires the graph;
stale edges are removed.

The database records `changed_at` and `verified_at` revisions plus the last
decision—`executed`, `reused`, or `backdated`—for every resident query node.

## Core API

| API | Purpose |
|---|---|
| `Input[T](key)` | A stable, application-supplied leaf value. |
| `@query` / `Query` | A pure derived computation with a stable identity. |
| `Resource` | A tracked read from external state with separate probe/load behavior. |
| `Database` | Owns inputs, query memos, dependency edges, revisions, adapters, and observers. |
| `eq=` / `cutoff=` | Defines semantic equality or a snapshot-safe comparison token. |
| `ValueAdapter` | Brings a custom value type across the owned snapshot boundary. |
| `ArtifactStore` | Stores content-addressed snapshots and durable checkpoints. |
| `@action` / `Output` | Reconciles a complete query-derived desired file set. |

The stable query surface is documented by the
[kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md);
the filesystem surface is documented by the
[action contract](https://github.com/Brumbelow/pyinc/blob/main/docs/action-contract.md).

## Track external state explicitly

Inputs are pushed into a database. Resources are pulled from external state and
re-probed when a request needs them.

```python docs-check
from pyinc import Database, FileResource, query

FILES = FileResource()


@query
def nonempty_lines(db: Database, path: str) -> tuple[str, ...]:
    text = FILES.read(db, path)
    return tuple(line.strip() for line in text.splitlines() if line.strip())
```

Built-in resources cover:

- text files (`FileResource`)
- binary files (`BinaryFileResource`)
- file metadata (`FileStatResource`)
- environment variables (`EnvResource`)
- directory listings (`DirectoryResource`)

During query execution, raw `open()`, `io.open()`, environment access,
directory listing, and `Path.iterdir()` are intercepted outside resource scope
and raise `UntrackedReadError`.

Not every ambient read can be intercepted. Low-level `os.open()`, subprocess
output, network calls, time, randomness, and C-extension I/O must be modeled by
a custom resource or declared with:

```python
db.report_untracked_read("result depends on subprocess output")
```

That node then executes on every request and cannot backdate. The escape hatch
preserves correctness at the cost of reuse.

## Choose a boundary mode

The mode changes how values are exposed and how mutation is checked. It does
not disable dependency tracking or ambient-read enforcement.

| Mode | Values exposed to queries and callers | Mutation behavior |
|---|---|---|
| `strict` | Immutable snapshot views such as `FrozenList`, `FrozenDict`, `FrozenSet`, and `FrozenRecord` | Writes fail immediately. |
| `checked` | Owned ordinary-container copies | Before/after fingerprints detect in-query mutation. |
| `fast` | Owned ordinary-container copies | Mutation is not checked; determinism is the caller's responsibility. |

Start with `Database(mode="strict")`. Move a measured workload to `checked` or
`fast` only when it genuinely needs ordinary mutable containers at a boundary.

Shared and cyclic mutable object graphs are supported through `FrozenGraph` and
`FrozenRef`; pure trees retain the simpler flat snapshot representation.

## Reconcile declared outputs

Queries derive values and remain free of side effects. A separate `@action`
turns a complete desired `Output` set into files.

```python
from pyinc import Database, Input, Output, action, query

NAME = Input[str]("example.greeting.name")


@query
def greeting(db: Database) -> str:
    return f"Hello, {NAME.read(db)}!\n"


@action(tool="example/greeting-v1")
def write_greeting(db: Database) -> tuple[Output, ...]:
    return (Output.text("greeting.txt", greeting(db)),)


db = Database(mode="strict")
db.set(NAME, "Ada")

plan = write_greeting.plan(db, root="generated/")
result = write_greeting.reconcile(db, root="generated/")

print(result.created, result.updated, result.repaired, result.deleted)
```

The action layer provides:

- complete preflight validation before mutation
- atomic per-file replacement
- cross-process reconciliation locks
- content-hash change and tamper detection
- repair of missing or modified owned outputs
- deletion of outputs previously owned but no longer declared
- dry-run planning under the same validation and lock
- portable root-relative path validation

An action deletes only paths recorded in its own validated ownership ledger.
The exact guarantees and non-goals are in the
[action contract](https://github.com/Brumbelow/pyinc/blob/main/docs/action-contract.md).

## Reuse work across processes

An `ArtifactStore` can persist boundary snapshots and a validated checkpoint of
the current query graph.

```python
from pyinc import Database, FileSystemArtifactStore

store = FileSystemArtifactStore(".pyinc-cache")

first = Database(mode="strict", store=store)
# Set the application's keyed Inputs, then request its top-level query.
# first.set(...)
# first.get(...)
checkpoint = first.save_checkpoint()

later = Database(mode="strict", store=store)
# Set the same keyed Inputs before loading.
# later.set(...)
later.load_checkpoint(checkpoint)
# later.get(...) verifies live dependencies and reuses compatible records.
```

A checkpoint is cache warming, not workflow resumption. The loader validates
the complete manifest before staging records and pins query code, policies,
captures, resources, adapters, and interpreter/build identity. Live resources
are re-probed. Records that cannot be verified miss safely and re-execute;
malformed or tampered manifests fail loudly.

See [Durable Cache](https://github.com/Brumbelow/pyinc/blob/main/docs/architecture.md#durable-cache)
and the
[checkpoint trust boundary](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md#checkpoint-save-and-load).

## Inspect incremental decisions

Incrementality is observable rather than hidden behind a decorator.

```python
value = db.get(rendered_names)

print(db.explain(rendered_names))
print(db.inspect(rendered_names))
print(db.statistics())
print(db.query_profile())
print(db.dependency_graph())
```

- `inspect()` reports the most recently recorded provenance without triggering
  another verification pass.
- `inspect_fresh()` verifies current inputs/resources first, then returns the
  provenance tree.
- `statistics()` reports executions, reuses, backdates, and resource loads.
- `query_profile()` exposes bounded per-query timing aggregates.
- `dependency_graph()` exports a machine-readable graph.
- `observe()` delivers `QueryChangeEvent` notifications after a stored query
  value actually changes.

## Packages and worked consumers

The distribution contains three typed top-level packages, plus the stable
`pyinc.integrations` subpackage.

| Package | Role | Documentation |
|---|---|---|
| `pyinc` | The domain-independent query kernel, resources, snapshots, stores, checkpoints, and actions. | [Kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md) |
| `pyinc.integrations` | Stable analysis results and high-level entrypoints for Python source, configuration, dependencies, symbols, and notebooks. | [Integration contract](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-contract.md) |
| `pyinc_tools` | A CLI analyzer, polling watcher, `WorkspaceSession`, and stdio LSP server built on the integration API. | [`pyinc-tools` guide](https://github.com/Brumbelow/pyinc/blob/main/docs/pyinc-tools-guide.md) |
| `pyinc_codegen` | The JSON-Schema-to-typed-Python reference compiler built on queries and actions. | [Codegen guide](https://github.com/Brumbelow/pyinc/blob/main/docs/codegen-guide.md) |

The kernel is the product. `pyinc_tools`, `pyinc_codegen`, and the shipped
integrations are batteries and worked consumers that use its stable public API;
they do not widen the domain-independent kernel.

### Shipped integrations

`pyinc.integrations` includes high-level entrypoints and frozen result types for:

- Python source, imports, modules, symbols, scopes, references, and class models
- TOML, JSON, XML, CSV/TSV, and `.env` inspection
- requirements files, recursive `-r` includes, PEP 440/508 evaluation, and
  installed-package discovery
- dependency diagnostics and deep module resolution
- Jupyter notebook analysis with output-insensitive semantic cutoff

Integrations compose through ordinary query calls, so the kernel records
cross-integration dependencies without special wiring. Supported shapes and
conservative analysis limits are documented in the
[integration contract](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-contract.md).

## How pyinc differs from adjacent tools

| Tool/category | What it does | Where pyinc differs |
|---|---|---|
| [`functools.cache`](https://docs.python.org/3/library/functools.html#functools.cache) | Memoizes one function by arguments. | No transitive query graph, tracked external resources, revision verification, or semantic early cutoff. |
| [`joblib.Memory`](https://joblib.readthedocs.io/en/stable/memory.html) | Persists function results for repeated argument values. | pyinc maintains a revisioned dynamic dependency graph and revalidates the dependencies each query actually read. |
| [`doit`](https://pydoit.org/) / Snakemake | Runs explicit tasks based on declared dependencies, files, and up-to-date rules. | pyinc is an embeddable, fine-grained semantic query runtime rather than a task runner or workflow CLI. |
| Bazel / Pants | Provides a complete build system with scheduling, sandboxing, and broader execution infrastructure. | pyinc is a small synchronous Python library that applications embed in their own process. |
| [Salsa](https://salsa-rs.github.io/salsa/overview.html) | Provides on-demand incremental queries for Rust applications such as language tooling. | Salsa is the closest conceptual relative; pyinc addresses Python values, ambient I/O, portable checkpoints, and declared filesystem outputs. |
| [`incr`](https://github.com/Anyesh/incr) | Provides a Rust incremental engine, Python bindings, concurrency, and delta-based collections. | pyinc is pure Python and centers Python developer-tool correctness: resource guards, owned boundaries, validated checkpoints, and actions. |

These systems overlap in ideas, not necessarily in intended deployment. Choose
pyinc when the desired abstraction is an in-process Python query graph with an
explicit correctness boundary.

## Verification and benchmarks

Correctness and deterministic work are release gates. Wall-clock timings are
informational because incremental value depends on the workload, graph shape,
and edit sequence.

The reproducible harness exercises four targets:

- a synthetic shared query graph
- the include-aware `calc` compiler
- JSON Schema code generation
- declared-output reconciliation

Each scenario compares incremental results with a fresh, cache-free evaluation.
The canonical sequence covers cold evaluation, no-op requests, unreferenced
edits, formatting-only edits, localized edits, high-fan-out edits, output
removal, output tampering, and checkpoint restore.

The v3.0.0 release ran five isolated repetitions of a fixed 67-row matrix. Every
pyinc result matched fresh recomputation. Deterministic work counts and memo-node
ceilings are also gated so a regression to full-graph recomputation fails even
when the final result remains correct.

```console
python -m pip install -e '.[bench]'
PYTHONPATH=src python -m bench.run --output bench/results --repetitions 5
```

Read the
[benchmark methodology](https://github.com/Brumbelow/pyinc/blob/main/bench/README.md)
or inspect the
[v3.0.0 release run](https://github.com/Brumbelow/pyinc/actions/runs/29214340501).

The deeper property and adversarial suites cover fresh-run equivalence, dynamic
dependency rewiring, mutation and aliasing, LRU eviction, cross-process
checkpoints, corrupted stores, changed implementations, path traversal,
symlinks, output tampering, and concurrent access.

## Documentation

- [Getting started](https://github.com/Brumbelow/pyinc/blob/main/docs/getting-started.md) — build a graph, track a file, choose a mode, inspect work, and write a first action.
- [Architecture](https://github.com/Brumbelow/pyinc/blob/main/docs/architecture.md) — understand package ownership and the kernel/consumer boundaries.
- [Kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md) — depend on the exact from-scratch consistency guarantee.
- [Action contract](https://github.com/Brumbelow/pyinc/blob/main/docs/action-contract.md) — reconcile declared outputs safely.
- [Integration contract](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-contract.md) — use stable analyzer entrypoints and result types.
- [`pyinc-tools` guide](https://github.com/Brumbelow/pyinc/blob/main/docs/pyinc-tools-guide.md) and [LSP reference](https://github.com/Brumbelow/pyinc/blob/main/docs/lsp-reference.md) — operate the CLI, watcher, workspace session, and language server.
- [Codegen guide](https://github.com/Brumbelow/pyinc/blob/main/docs/codegen-guide.md) — inspect the reference file-to-file compiler.
- [Integration authoring](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-authoring.md) — add an integration without widening the kernel.
- [Migrating from 2.x](https://github.com/Brumbelow/pyinc/blob/main/docs/migration-v3.md) — update code and discard incompatible v2 state.

The [documentation index](https://github.com/Brumbelow/pyinc/blob/main/docs/README.md)
groups guides by task and contract.

## Development

```console
git clone https://github.com/Brumbelow/pyinc.git
cd pyinc
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'

python3 scripts/check_docs.py
pytest -q
python3 -m mypy src tests bench scripts
python3 -m ruff check src tests bench scripts
```

The documentation checker validates local links and anchors, executable
examples, CLI output, and the documented stable integration surface.

Issues and pull requests are welcome. If you are evaluating pyinc for an
existing analyzer, generator, compiler, or repository tool, an issue describing
one repeated computation and its current invalidation strategy is especially
useful.

## License

Apache License 2.0. See [LICENSE](https://github.com/Brumbelow/pyinc/blob/main/LICENSE).
