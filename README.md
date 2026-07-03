# pyinc

[![CI](https://github.com/Brumbelow/pyinc/actions/workflows/ci.yml/badge.svg)](https://github.com/Brumbelow/pyinc/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/pyinc)](https://pypi.org/project/pyinc/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyinc)](https://pypi.org/project/pyinc/)
[![PyPI license](https://img.shields.io/pypi/l/pyinc)](https://pypi.org/project/pyinc/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Pytest](https://img.shields.io/badge/Pytest-fff?logo=pytest&logoColor=000)](#)

```
pip install pyinc
```

**`pyinc` re-runs only the work whose inputs actually changed — and proves the
result matches a from-scratch run.** You decorate functions with `@query`,
declare where external state comes from (files, env vars, directories), and
`pyinc` caches results, records the dependency graph as your code runs, and on
the next call recomputes just the affected queries.

It is pure-Python, stdlib-only, with zero runtime dependencies.

The design space is the one occupied by [Salsa][salsa], [Jane Street
Incremental][incr], and [Bazel/Skyframe][skyframe] — adapted to Python's
realities: mutable defaults, hidden ambient I/O, and pervasive object identity.

[salsa]: https://github.com/salsa-rs/salsa
[incr]: https://github.com/janestreet/incremental
[skyframe]: https://bazel.build/reference/skyframe

## The problem it solves

Programs that cache derived results usually invalidate by hand: a watcher that
clears a dict, a hash checked at the top of a function, a `stale?` flag flipped
at known points. Every shortcut of that shape has a failure mode where a real
input changed but the cache didn't notice — and the program silently serves a
stale value.

`pyinc` removes that class of bug. The caller never reasons about invalidation.
You declare inputs and resources and write plain functions; the runtime captures
the dependency graph, snapshots every value crossing a cached boundary, and
re-validates top-down on each request. Anything whose dependencies are unchanged
is reused. Anything that recomputes to a semantically equal result is
*backdated* (early cutoff), so its downstream consumers stay valid without
re-running.

## Quick example

```python
from pyinc import Database, FileResource, query

_FILES = FileResource()

@query
def read_config(db, path):
    return _FILES.read(db, path)      # tracked file read

@query
def parse_names(db, path):
    text = read_config(db, path)
    return [line.strip() for line in text.splitlines() if line.strip()]

db = Database(mode="strict")
result = db.get(parse_names, "/tmp/names.txt")   # computes from scratch
result = db.get(parse_names, "/tmp/names.txt")   # reuses memo — file unchanged

# Edit the file: only affected queries re-execute.
# Comment-only edits can be backdated (early cutoff) with a cutoff= function.
# Raw open() inside a query raises UntrackedReadError.
# In strict mode, returned values are frozen — mutation raises TypeError.
```

`examples/correctness_demo.py` walks through backdating, mutation protection,
untracked-read enforcement, and provenance inspection. The `examples/` directory
also has focused scripts for push observers, the artifact store, the
mutable-graph boundary, cross-run checkpoints, the notebook integration, the
`@action` reconciliation layer (`action_reconcile_demo.py`), and the end-to-end
`calc` fixture (`calc_demo.py`, `examples/calc/`).

## What's in the box

| Area | What it gives you | Learn more |
|---|---|---|
| **Kernel** | The `@query` runtime: dependency capture, red-green verification, backdating, three execution modes, bounded memoization, provenance/inspection. | [kernel-contract.md](docs/kernel-contract.md) |
| **Actions** | Turn query-derived *desired* artifacts into files on disk without side effects in queries — atomic writes, tamper repair, orphan cleanup, dry-run. | [action-contract.md](docs/action-contract.md) |
| **Integrations** | Narrow, stdlib-only analyzers (Python source, configs, requirements, symbols, notebooks…) that compose at the query layer. | [integration-contract.md](docs/integration-contract.md) |
| **Tooling** | `pyinc-tools`: a CLI analyzer and an LSP server, built only on the stable integration surface. | [pyinc-tools-guide.md](docs/pyinc-tools-guide.md) |
| **Codegen** | `pyinc_codegen`: a JSON-Schema → typed-Python compiler, the reference file→file consumer. | [codegen-guide.md](docs/codegen-guide.md) |
| **Benchmarks** | A reproducible timing + correctness harness (`bench/`), not shipped in the wheel. | [below](#benchmarks) |

New here? Start with [docs/architecture.md](docs/architecture.md) for the map,
then [docs/kernel-contract.md](docs/kernel-contract.md) for the guarantee.

## What pyinc guarantees

`pyinc` guarantees **from-scratch consistency**: incremental evaluation produces
the same result as a fresh evaluation on the same declared inputs and resources.
The guarantee holds when, and only when, three conditions hold:

1. **Value boundary ownership** — every value crossing a cached boundary is
   snapshot-safe (an immutable scalar, a tuple, a `freeze`-convertible container,
   a dataclass, or a registered `ValueAdapter`). Mutable graphs with shared
   identity or cycles round-trip through `FrozenGraph` / `FrozenRef`.
2. **Tracked ambient reads** — every read of external state inside a query goes
   through a `Resource` (or is explicitly declared via
   `db.report_untracked_read(reason)`). The runtime intercepts `builtins.open`,
   `io.open`, `os.getenv`, `os.environ`, `os.listdir`, `os.scandir`, and
   `Path.iterdir` during query execution and raises `UntrackedReadError` on
   escapes.
3. **Deterministic queries** — the same tracked dependencies produce a
   semantically equal value. Mutable closure or global captures are rejected at
   decoration time, so memo reuse can't silently depend on hidden mutation.

The full contract — soundness envelope, the three modes, out-of-scope cases, and
documented escape hatches — is in
[docs/kernel-contract.md](docs/kernel-contract.md).

## Kernel surface

The stable top-level API, grouped by what you reach for:

- **Define work** — `@query` for derived values; `Input` for base leaves;
  optional `eq=` / `cutoff=` policies for custom equivalence and backdating;
  `ValueAdapter` for custom snapshot-safe boundary types.
- **Track external state** — `FileResource`, `FileStatResource`, `EnvResource`,
  `DirectoryResource`.
- **Run** — pull-based recomputation with `strict` / `checked` / `fast` modes;
  bounded memoization via `Database(max_query_nodes=...)` (LRU at request
  boundaries; inputs and resources stay resident); atomic batch invalidation via
  `Database.set_many(...)`.
- **Inspect** — `Database.dependency_graph()` for a machine-readable export;
  `Database.inspect(...)` / `Database.explain(...)` for per-node provenance
  (observational and human-readable); `Database.statistics()` /
  `Database.query_profile()` for counters and per-query timing.
- **Observe** — `Database.observe(callback, query, *args, **kwargs)` for push
  observers. `QueryChangeEvent`s fire after the outermost request completes (so
  callbacks may safely re-enter) and only on `executed` decisions — `reused` and
  `backdated` don't fire because the stored value didn't move.
- **Mutable graphs** — `FrozenGraph` / `FrozenRef` carry shared or cyclic object
  graphs across the boundary; `freeze` memoizes containers by id and `thaw`
  reconstructs identity faithfully (a list-containing-itself round-trips). Pure
  trees pay no overhead.
- **Persist** — content-addressed artifact storage via the `ArtifactStore`
  Protocol (`InMemoryArtifactStore`, `FileSystemArtifactStore`).
  `Database(store=...)` writes every boundary snapshot keyed by its
  `fingerprint_snapshot` digest; `serialize_snapshot` / `deserialize_snapshot`
  expose the byte form.
- **Reconcile** — the `@action` layer: queries derive `Output(path, content)`
  (snapshot-safe, so `tuple[Output, ...]` is a valid query return); a separate
  `@action` reconciles them with the filesystem. Side effects never enter a
  query. See [action-contract.md](docs/action-contract.md).

`Database` is thread-safe across instances and on a single shared instance; the
ambient-read guard is installed once globally and dispatches per-context, so
threads inside queries on different databases don't interfere.

## Integrations

`pyinc.integrations` ships narrow, stdlib-only analyzers that compose at the
query layer — the kernel tracks cross-integration calls as ordinary dependency
edges, so no extra wiring is needed. Each entry is a one-line summary; the full
public surface per integration is in
[docs/integration-contract.md](docs/integration-contract.md).

| Integration | Analyzes |
|---|---|
| `python_source` | Workspace module discovery, top-level imports/definitions, export tracking, and import resolution (`workspace` / `stdlib` / `installed` / `missing` / `ambiguous`). |
| `installed_packages` | Installed packages via `.dist-info`, stdlib modules via `sys.stdlib_module_names`, and import-name resolution. |
| `deep_module_resolution` | `sys.path` walking, `.pth` processing, PEP 420 namespace packages, and dotted-name → file resolution. |
| `symbol_resolution` | Module- and class-level symbol tables, re-export following with cycle detection, annotation-text extraction, and a reverse-reference index. |
| `dependency_check` | Composes `installed_packages` + `python_source` to flag undeclared imports and missing / mismatched packages. |
| `toml_config` / `json_config` / `xml_config` | Single-file inspection: sections, keys, traversal, and parse diagnostics. |
| `requirements_txt` | Requirement specs, file references, index directives, editable/URL installs, and recursive `-r` following with cycle detection. |
| `requirement_evaluation` | PEP 440 specifier satisfaction and PEP 508 marker evaluation for the current environment. |
| `env_file` | `.env` parsing: quoted/unquoted values, `export` prefixes, and interpolation references. |
| `csv_data` | CSV/TSV structure: header/column discovery, delimiter sniffing, row counts, and inconsistency diagnostics. |
| `notebook` | Jupyter `.ipynb` analysis with cutoff-based backdating that ignores `outputs` / `execution_count`. |

`pyinc.integrations` re-exports only the stable dataclass/result types and
high-level entrypoints; low-level payload queries and decode helpers stay
experimental in their defining submodules.

## Consumer tooling

LSP wiring and filesystem watchers are deliberately **out of scope** for the
kernel. They live in `pyinc_tools`, a separate consumer layer built only on the
stable `pyinc.integrations` surface:

- `pyinc-tools analyze <root>` — one-shot or threaded `--watch` workspace
  analysis via a polling watcher.
- `pyinc-tools lsp` — a stdio LSP server with document/workspace symbols,
  diagnostics (push **and** LSP 3.17 pull channels), hover, goto-definition, and
  find-references, all backed by `symbol_resolution`. It starts a threaded
  filesystem watcher so external edits (`git pull`, formatters) publish fresh
  diagnostics even without editor `didChangeWatchedFiles` events.

See [docs/pyinc-tools-guide.md](docs/pyinc-tools-guide.md) for install, editor
wiring, the overlay model, and the supported-vs.-not-yet feature table.

## Code generation

`pyinc_codegen` is a reference consumer that compiles a JSON Schema into typed
Python models — one model and one doc file per definition plus an aggregate
`__init__.py` — emitted through the `@action` layer so only artifacts whose
content changed are rewritten. It is stdlib-only and builds on pyinc's public API
only.

```python
from pyinc import Database
from pyinc_codegen import generate

generate(Database(mode="strict"), "schema.json", "generated/")
```

Whitespace edits rewrite nothing; a description-only edit rewrites only the doc
file; a property change rewrites the affected model (and its reference-graph
dependents, each only if its output changed); adding or removing a definition
touches only that definition's files plus the index. See
[docs/codegen-guide.md](docs/codegen-guide.md).

## Diagnostics and escape hatches

- `Database.inspect(...)` is observational — it returns the last recorded
  provenance tree without a fresh pass. `Database.inspect_fresh(...)` verifies
  first, then returns the tree. See `examples/inspect_fresh_demo.py`.
- Query identity includes the function-definition payload and immutable captures;
  mutable closure/global captures are rejected. Preview the classification with
  `pyinc.explain_query_captures(fn)` before the first `db.get(...)`. See
  `examples/capture_diagnostics.py`.
- `Database.report_untracked_read(reason)` is the explicit impurity escape hatch:
  it marks the current query as always-re-executing and disables its backdating —
  the right trade-off when a dependency is real but not resource-trackable. See
  `examples/untracked_escape_hatch.py`.
- The package ships inline typing metadata via `py.typed`.

## Benchmarks

`bench/` is a reproducible benchmark + correctness harness (not shipped in the
wheel). It exercises four targets — synthetic kernel query graphs, the
calc-with-includes fixture, JSON-Schema code generation, and action
reconciliation — across a canonical edit sequence (cold, unchanged, unreferenced
edit, comment-only edit, localized edit, high-fan-out shared edit, removed
artifact, tampered output, checkpoint restore), comparing pyinc against full
recomputation, a naive per-key cache, and `joblib.Memory`.

**No performance claim ships without its harness: every scenario pairs its
timing with a correctness assertion that pyinc's incremental output equals a
fresh, cache-free run.** The report (CSV + markdown) lands in `bench/results/`;
the naive cache is included precisely to show that a shortcut can be fast but
stale where pyinc stays correct.

```bash
pip install -e '.[bench]'    # joblib is a bench-only optional dependency
PYTHONPATH=src python -m bench.run
```

## Development

```bash
git clone https://github.com/Brumbelow/pyinc.git && cd pyinc
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
pytest -q
python3 -m mypy src tests
python3 -m ruff check src tests
```

Key references: the [kernel contract](docs/kernel-contract.md) (soundness
envelope), the [integration contract](docs/integration-contract.md) (stable API
boundaries), and the [integration authoring guide](docs/integration-authoring.md)
(how to write a new one).
