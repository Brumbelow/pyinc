# pyfoundinc

`pyfoundinc` is a correctness-first incremental computation engine for Python: a Python-native query kernel in the design space of Salsa, Jane Street Incremental, and Bazel/Skyframe.

Current scope:

- `@query` for derived values
- `Input` for explicit base leaves
- optional `eq=` and `cutoff=` policies on `Input` and `@query`
- `ValueAdapter` for custom snapshot-safe boundary types
- `FileResource`, `FileStatResource`, `EnvResource`, and `DirectoryResource` for explicit external reads
- pull-based recomputation with revisions, dependency capture, red-green verification, and backdating
- `strict`, `checked`, and `fast` execution modes with explicit boundary semantics
- optional bounded query memoization via `Database(max_query_nodes=...)`
- `Database.inspect(...)` for structured provenance and `Database.explain(...)` for human-readable formatting

First supported integration:

- `pyfoundinc.integrations.python_source` formalizes the mini-analyzer example as a supported reference integration.
- `file_analysis(db, path)` and `directory_analysis(db, root)` return stable dataclass views over low-level query nodes such as `source_text`, `imports_for_file`, and `file_analysis_payload`.
- `module_analysis(db, root, path)` and `workspace_analysis(db, root)` add a recursive, workspace-local module graph over the same kernel.
- The integration is still intentionally narrow: workspace-local module discovery, top-level imports and definitions only, syntax diagnostics only, and dependency invalidation based on resolved module export surfaces.
- Full `sys.path` / installed-package resolution, symbol/type resolution, LSP wiring, and watchers remain out of scope.

The core contract is intentionally narrow: values crossing cached boundaries must be snapshot-safe, and hidden reads are treated as correctness violations rather than “best effort” cache misses.

Queries may capture immutable constants plus explicit `Input`, `@query`, and resource handles. Mutable global/nonlocal ambient state is rejected so stale reuse does not silently depend on untracked Python objects.

`cutoff=` is the low-level semantic cutoff hook. It maps a value to a snapshot-safe token used for equal-input suppression and query backdating. Use it when semantic equality is cheaper or more precise than comparing full output values directly. `eq=` and `cutoff=` are mutually exclusive.

Gotchas:

- `Database.inspect(...)` is observational. It returns the last recorded provenance tree for that query key and does not force a fresh revalidation pass by itself.
- Query identity includes the function definition payload. If you capture ambient values, those captures are part of the query fingerprint, and mutable closure/global captures are rejected.
- `Database.report_untracked_read(...)` is an explicit impurity escape hatch. It marks that query as always re-executing and disables backdating for that node.
- The package ships inline typing metadata via `py.typed`.

Development:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
python -m mypy src tests
python -m ruff check src tests
PYTHONPATH=src python benchmarks/run_microbench.py --samples 2 --warmup 0 --rounds 1 --payload-size 8
```

The runtime contract is summarized in [docs/kernel-contract.md](docs/kernel-contract.md).
