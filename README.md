# pyinc

Python is mutable by default, identity-heavy, and full of hidden side effects. These properties make incremental computation unsound in practice: cached results silently depend on mutated state, untracked file reads, and object identity that the cache cannot see.

`pyinc` is a correctness-first incremental computation engine that solves this problem. It is a pure-Python, stdlib-only query kernel in the design space of Salsa, Jane Street Incremental, and Bazel/Skyframe — but designed specifically for the challenges Python creates.

The package remains alpha. The v1 kernel contract is stable within the documented soundness envelope.

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

See `examples/correctness_demo.py` for a full walkthrough of backdating (early cutoff), mutation protection, untracked read enforcement, and provenance inspection.

## What pyinc guarantees

pyinc guarantees **from-scratch consistency** — the result of incremental evaluation matches a fresh evaluation on the same declared inputs and resources — when:

1. **Value boundary ownership**: all values crossing cached boundaries are snapshot-safe (frozen)
2. **Tracked ambient reads**: all external state reads go through the Resource API
3. **Deterministic queries**: given the same tracked dependencies, queries return semantically equal values

The full contract, including explicit limitations and escape hatches, is in [docs/kernel-contract.md](docs/kernel-contract.md).

## Current scope

- `@query` for derived values, `Input` for explicit base leaves
- optional `eq=` and `cutoff=` policies for custom equivalence and backdating (early cutoff)
- `ValueAdapter` for custom snapshot-safe boundary types
- `FileResource`, `FileStatResource`, `EnvResource`, and `DirectoryResource` for tracked external reads
- pull-based recomputation with revisions, dependency capture, red-green verification, and backdating (early cutoff)
- `strict`, `checked`, and `fast` execution modes with explicit boundary semantics
- optional bounded query memoization via `Database(max_query_nodes=...)`
- `Database.set_many(...)` for atomic batch invalidation of multiple inputs (single revision bump)
- `Database.dependency_graph()` for machine-readable graph export of all nodes and edges
- `Database.inspect(...)` for structured provenance and `Database.explain(...)` for human-readable formatting
- `Database.statistics()` for aggregate counters and `Database.query_profile()` for per-query timing

## Integrations

- `pyinc.integrations.python_source` — workspace-local module discovery, top-level imports/definitions, simple assignment tracking for export surfaces, conservative import resolution with `workspace`/`stdlib`/`installed`/`missing`/`ambiguous` outcomes (stdlib and installed classification via composition with `installed_packages`; installed imports' `resolved_path` is populated via `deep_module_resolution`).
- `pyinc.integrations.toml_config` — single-file TOML inspection: section/key extraction, dependency and optional-dependency discovery, tool config discovery.
- `pyinc.integrations.requirements_txt` — narrow requirements parsing: normalized requirement specs, file references, index directives, editable installs, URL requirements. Includes `deep_requirements_analysis` for recursive `-r`/`--requirement` file following with cycle detection.
- `pyinc.integrations.installed_packages` — installed package discovery via `importlib.metadata`-compatible `.dist-info` directories, stdlib module identification via `sys.stdlib_module_names`, and import name resolution (`stdlib`/`installed`/`unknown`).
- `pyinc.integrations.deep_module_resolution` — deep module path resolution: `sys.path` walking, `.pth` file processing with backdating on whitespace/comment edits, PEP 420 namespace package collection, and dotted-name → file resolution. Exposes `resolve_module_path` for per-module queries and `deep_module_resolution_analysis` for a workspace-wide snapshot.
- `pyinc.integrations.json_config` — single-file JSON inspection: section/key extraction with type detection, nested object traversal, parse error diagnostics.
- `pyinc.integrations.dependency_check` — cross-integration dependency validation: composes `installed_packages` and `python_source` to detect undeclared imports and missing packages.
- `pyinc.integrations.env_file` — `.env` file parsing: key-value extraction with quoted/unquoted values, `export` prefix handling, interpolation reference detection.
- `pyinc.integrations.xml_config` — XML file inspection via `xml.etree.ElementTree`: element/attribute extraction, dot-path traversal, namespace-aware tag normalization.
- `pyinc.integrations.csv_data` — CSV/TSV structural analysis via stdlib `csv`: header detection, column discovery, delimiter sniffing, row counting, inconsistent column diagnostics.

`pyinc.integrations` re-exports only the stable dataclass/result types and high-level entrypoints for these integrations. Low-level payload queries, decode helpers, and resource helpers remain experimental in their defining submodules.

## Verification

- The runtime contract is summarized in [docs/kernel-contract.md](docs/kernel-contract.md).
- The repo includes dedicated test modules for value semantics, runtime behavior, provenance/explanation formatting, property-based from-scratch consistency, and each shipped integration.
- The integration suites exercise `strict`, `checked`, and `fast` modes and compare incremental results against fresh recomputation over edit sequences.

The integration boundary is summarized in [docs/integration-contract.md](docs/integration-contract.md).

## Gotchas

- `Database.inspect(...)` is observational. It returns the last recorded provenance tree for that query key and does not force a fresh revalidation pass by itself.
- Query identity includes the function definition payload. If you capture ambient values, those captures are part of the query fingerprint, and mutable closure/global captures are rejected.
- `Database.report_untracked_read(...)` is an explicit impurity escape hatch. It marks that query as always re-executing and disables backdating for that node.
- The package ships inline typing metadata via `py.typed`.

## Not yet supported

- Marker expression evaluation, version constraint satisfaction
- Symbol/type resolution, LSP wiring, and file watchers

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
pytest -q
python3 -m mypy src tests
python3 -m ruff check src tests
```

The runtime contract is summarized in [docs/kernel-contract.md](docs/kernel-contract.md). Integration API boundaries are summarized in [docs/integration-contract.md](docs/integration-contract.md). A guide for building new integrations is at [docs/integration-authoring.md](docs/integration-authoring.md).
