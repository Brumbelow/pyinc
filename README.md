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

`pyinc` is a pull-based incremental query kernel for Python. It memoizes the
results of decorated functions, captures their dependencies as they execute,
and on the next request re-runs only the work whose declared inputs actually
changed. The design space is the one occupied by Salsa, Jane Street
Incremental, and Bazel/Skyframe, adapted to the constraints of Python:
mutable defaults, hidden ambient I/O, and pervasive object identity.

The library is pure-Python and stdlib-only, with zero runtime dependencies.

## What pyinc is for

Programs in Python that cache derived results usually invalidate by hand: a
watcher that clears a dict, a hash compared at the start of a function, a
"stale?" boolean updated at known points. Every shortcut of that shape has a
failure mode in which a real input changed but the cache did not notice, and
the program silently uses a stale value.

`pyinc` exists so that class of problem can be solved without the caller
having to reason about invalidation. The user declares base inputs, declares
resources for external state (files, environment variables, directories),
and writes queries as ordinary Python functions decorated with `@query`. The
runtime captures the dependency graph as queries execute, stores frozen
snapshots of every value crossing a cached boundary, and re-validates
dependencies top-down on each request. Anything whose recorded dependencies
are unchanged is reused. Anything whose recomputation produces a
semantically equal result is *backdated*, so downstream consumers stay valid
without re-running.

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

See `examples/correctness_demo.py` for a walkthrough of backdating, mutation
protection, untracked-read enforcement, and provenance inspection. The
`examples/` directory also contains focused scripts for the push-observer
and artifact-store APIs, the mutable-graph boundary, the cross-run checkpoint
API, the Jupyter notebook integration, and several shipped integrations.

## What pyinc guarantees

`pyinc` guarantees **from-scratch consistency**: the result of incremental
evaluation matches a fresh evaluation on the same declared inputs and
resources. The guarantee holds when, and only when, three conditions hold:

1. **Value boundary ownership** — every value crossing a cached boundary is
   snapshot-safe: an immutable scalar, a tuple, a container that `freeze`
   can deep-convert (`list` → `tuple`, `dict` → `FrozenDict`, `set` →
   `frozenset`, dataclass → `FrozenRecord`), or a value handled by a
   registered `ValueAdapter`. Mutable graphs with shared identity or cycles
   are supported via `FrozenGraph` / `FrozenRef` and round-trip through the
   boundary without losing aliasing.
2. **Tracked ambient reads** — every read of external state inside a query
   goes through a `Resource` (or is explicitly declared via
   `db.report_untracked_read(reason)`). The runtime intercepts
   `builtins.open`, `io.open`, `os.getenv`, `os.environ`, `os.listdir`,
   `os.scandir`, and `Path.iterdir` while a query is executing, and raises
   `UntrackedReadError` on uses that escape this protocol.
3. **Deterministic queries** — given the same tracked dependencies, a query
   returns a semantically equal value. Mutable closure or global captures in
   a query definition are rejected at decoration time so memo reuse cannot
   silently depend on hidden Python object mutation.

The full contract — the soundness envelope, the three execution modes, the
explicit out-of-scope cases, and the documented escape hatches — is in
[docs/kernel-contract.md](docs/kernel-contract.md).

## Kernel surface

- `@query` for derived values, `Input` for explicit base leaves, optional
  `eq=` / `cutoff=` policies for custom equivalence and backdating, and
  `ValueAdapter` for custom snapshot-safe boundary types.
- `FileResource`, `FileStatResource`, `EnvResource`, and `DirectoryResource`
  for tracked external reads.
- Pull-based recomputation with revisions, dependency capture, red-green
  verification, and backdating (early cutoff) on semantic equality.
- `strict`, `checked`, and `fast` execution modes with explicit boundary
  semantics.
- Bounded query memoization via `Database(max_query_nodes=...)` (LRU at
  top-level request boundaries; inputs and resources stay resident).
- Atomic batch invalidation via `Database.set_many(...)` (single revision
  bump for any number of input updates).
- `Database.dependency_graph()` for a machine-readable export of all nodes
  and edges; `Database.inspect(...)` and `Database.explain(...)` for
  per-node provenance, observational and human-readable respectively;
  `Database.statistics()` and `Database.query_profile()` for aggregate
  counters and per-query timing.
- `Database.observe(callback, query, *args, **kwargs)` for push observers
  on query nodes. Events are delivered as `QueryChangeEvent` after the
  outermost request scope completes (so callbacks may safely re-enter the
  database) and fire only on `executed` decisions; `reused` and
  `backdated` decisions do not fire because the stored value did not move.
- Mutable object graphs across cached boundaries via `FrozenGraph` and
  `FrozenRef` snapshot variants. `freeze` memoizes mutable containers by
  id; `thaw` reconstructs identity faithfully via two-pass
  allocate-then-fill so a list-with-itself round-trips to an actual
  self-referential list. Pure trees pay no overhead and retain the flat
  snapshot shape.
- Content-addressed artifact storage via the `ArtifactStore` Protocol, with
  `InMemoryArtifactStore` and `FileSystemArtifactStore` implementations.
  Passing `Database(store=...)` writes the serialized snapshot bytes for
  every value crossing the membrane, keyed by its `fingerprint_snapshot`
  digest. `serialize_snapshot` and `deserialize_snapshot` expose the byte
  form to external callers and round-trip the full snapshot grammar.
- `Database` is thread-safe for concurrent use across instances and on a
  single shared instance. The ambient-read guard is installed once globally
  and dispatches per-context, so threads inside queries on different
  databases do not interfere with each other's enforcement.

## Integrations

`pyinc.integrations` ships narrow, stdlib-only integrations that compose at
the query layer. The kernel tracks cross-integration calls as ordinary
dependency edges; no extra wiring is required.

- `python_source` — workspace-local module discovery, top-level imports and
  definitions, simple assignment tracking for export surfaces, and
  conservative import resolution with `workspace` / `stdlib` / `installed` /
  `missing` / `ambiguous` outcomes (stdlib and installed classification via
  composition with `installed_packages`; installed imports'
  `resolved_path` populated via `deep_module_resolution`).
- `toml_config` — single-file TOML inspection: section and key extraction,
  dependency and optional-dependency discovery, and tool-config discovery.
- `requirements_txt` — requirements parsing: normalized requirement specs,
  file references, index directives, editable installs, URL requirements,
  and `deep_requirements_analysis` for recursive `-r` / `--requirement`
  file following with cycle detection.
- `installed_packages` — installed package discovery via
  `importlib.metadata`-compatible `.dist-info` directories, stdlib module
  identification via `sys.stdlib_module_names`, and import-name resolution
  (`stdlib` / `installed` / `unknown`).
- `deep_module_resolution` — `sys.path` walking, `.pth` file processing
  with backdating on whitespace and comment-only edits, PEP 420 namespace
  package collection, and dotted-name → file resolution. Exposes
  `resolve_module_path` and `deep_module_resolution_analysis`.
- `json_config` — single-file JSON inspection: section and key extraction
  with type detection, nested-object traversal, and parse-error
  diagnostics.
- `dependency_check` — cross-integration dependency validation: composes
  `installed_packages` and `python_source` to detect undeclared imports
  and missing or version-mismatched packages.
- `env_file` — `.env` file parsing: key/value extraction with quoted and
  unquoted values, `export` prefix handling, and interpolation-reference
  detection.
- `xml_config` — XML inspection via `xml.etree.ElementTree`: element and
  attribute extraction, dot-path traversal, and namespace-aware tag
  normalization.
- `csv_data` — CSV/TSV structural analysis via stdlib `csv`: header
  detection, column discovery, delimiter sniffing, row counting, and
  inconsistent-column diagnostics.
- `requirement_evaluation` — PEP 440 version-specifier satisfaction and
  PEP 508 environment-marker evaluation; composes with `requirements_txt`
  and `installed_packages` to surface the effective applicable and
  satisfied requirement set for the current Python environment.
- `symbol_resolution` — workspace-wide symbol tables (module-level and
  class-level), cross-module re-export following with cycle detection,
  type-annotation text extraction via `ast.unparse` (no type evaluation),
  and a workspace-wide reverse-reference index for a given qualified
  name.
- `notebook` — Jupyter `.ipynb` analysis via stdlib `json`: per-cell
  type, source, and heading extraction; per-cell AST imports and
  definitions for code cells; and cutoff-based backdating that ignores
  `outputs` and `execution_count`, so re-running cells does not invalidate
  downstream consumers.

`pyinc.integrations` re-exports only the stable dataclass and result types
and the high-level entrypoints. Low-level payload queries, decode helpers,
and resource helpers remain experimental in their defining submodules.

## Verification

- The kernel contract is summarized in
  [docs/kernel-contract.md](docs/kernel-contract.md).
- The repository includes dedicated test modules for value semantics,
  runtime behavior, provenance and explanation formatting, property-based
  from-scratch consistency checks, and each shipped integration.
- The integration suites exercise `strict`, `checked`, and `fast` modes
  and compare incremental results against fresh recomputation over edit
  sequences.

The integration boundary is summarized in
[docs/integration-contract.md](docs/integration-contract.md).

## Diagnostics and escape hatches

- `Database.inspect(...)` is observational: it returns the last recorded
  provenance tree for a query key without forcing a fresh revalidation
  pass. `Database.inspect_fresh(...)` runs verification first and then
  returns the tree. See `examples/inspect_fresh_demo.py`.
- Query identity includes the function-definition payload. Captured
  ambient values contribute to the query fingerprint; mutable closure or
  global captures are rejected. Run `pyinc.explain_query_captures(fn)`
  before the first `db.get(...)` to see how each capture will be
  classified. See `examples/capture_diagnostics.py`.
- `Database.report_untracked_read(reason)` is an explicit impurity escape
  hatch. It marks the current query as always re-executing and disables
  backdating for that node, which is the right trade-off when a dependency
  is real but not resource-trackable. See
  `examples/untracked_escape_hatch.py`.
- The package ships inline typing metadata via `py.typed`.

## Consumer tooling

LSP wiring and push-based filesystem watchers are deliberately out of scope
for the kernel package. They live in a separate consumer layer,
`pyinc_tools`, that builds only on the stable `pyinc.integrations` public
surface:

- `pyinc-tools analyze <root>` runs one-shot or threaded `--watch`
  workspace analysis via a polling watcher.
- `pyinc-tools lsp` runs a stdio LSP server with document and workspace
  symbols, diagnostics, hover, goto-definition, and find-references, all
  backed by `pyinc.integrations.symbol_resolution`. The server starts a
  threaded filesystem watcher by default so external edits (e.g.
  `git pull`, formatter scripts) publish fresh diagnostics even when the
  editor does not emit `workspace/didChangeWatchedFiles`.

See [docs/pyinc-tools-guide.md](docs/pyinc-tools-guide.md) for install,
editor wiring, the overlay model, and the supported-vs.-not-yet feature
table.

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

The kernel contract is summarized in
[docs/kernel-contract.md](docs/kernel-contract.md). Integration API
boundaries are summarized in
[docs/integration-contract.md](docs/integration-contract.md). A guide to
authoring new integrations is at
[docs/integration-authoring.md](docs/integration-authoring.md).
