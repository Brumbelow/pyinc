# pyinc

[![CI](https://github.com/Brumbelow/pyinc/actions/workflows/ci.yml/badge.svg)](https://github.com/Brumbelow/pyinc/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/pyinc)](https://pypi.org/project/pyinc/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyinc)](https://pypi.org/project/pyinc/)
[![PyPI license](https://img.shields.io/pypi/l/pyinc)](https://pypi.org/project/pyinc/)
[![Lint: Ruff](https://img.shields.io/badge/lint-Ruff-D7FF64.svg)](https://docs.astral.sh/ruff/)

*Salsa-style red-green queries, hardened for Python's mutable runtime.*

`pyinc` is a correctness-first incremental query engine for Python. Declare
keyed inputs and pure queries, and it records the dependency graph while your
code runs. On the next request it reuses unaffected work, recomputes affected
queries, and backdates semantically equal results so downstream work stays
valid. It is pure Python, stdlib-only, with zero runtime dependencies; Python
3.11–3.14 are tested on Linux, macOS, and Windows. It exists so that the
cache-invalidation bugs of a hand-rolled caching layer — the editor still
underlining an error you fixed a minute ago — have somewhere to be caught.

```console
python -m pip install pyinc
```

## Quick start

```python docs-check
from pyinc import Database, Input, query

NAMES = Input[tuple[str, ...]]("example.names")


@query
def normalized_names(db: Database) -> tuple[str, ...]:
    return tuple(sorted({name.strip() for name in NAMES.read(db)}))


db = Database(mode="strict")
db.set(NAMES, (" Grace ", "Ada", "Ada"))
assert db.get(normalized_names) == ("Ada", "Grace")

db.set(NAMES, ("Grace", "Ada"))
assert db.get(normalized_names) == ("Ada", "Grace")
assert db.inspect(normalized_names).last_decision == "backdated"
```

The first request computes the result. The second input is different, so the
query runs again, but its result is semantically equal. `pyinc` backdates that
node instead of invalidating anything downstream.

## See it on a real workspace

![Editing pytest under pyinc's watcher](https://raw.githubusercontent.com/Brumbelow/pyinc/main/docs/assets/demo.gif)

`pyinc-tools` was pointed at a pinned checkout of pytest — nothing in it adapted
for `pyinc` — and watched while single files were edited: in one recorded run,
109.08 s to analyze all 270 files from cold, then 632 ms to catch up after an
edit. Timings are machine-specific; [the demo page](https://github.com/Brumbelow/pyinc/blob/main/docs/demo.md)
has the clips, the full provenance, and the deterministic work counts.

## Documentation

- [Getting started](https://github.com/Brumbelow/pyinc/blob/main/docs/getting-started.md) — build a small graph, add a tracked file, choose a mode, inspect work, and write a first action.
- [Examples](https://github.com/Brumbelow/pyinc/tree/main/examples) — small runnable scripts, including the `calc` worked example.
- [FAQ](https://github.com/Brumbelow/pyinc/blob/main/docs/faq.md) — how this relates to Salsa, why not `lru_cache`, threading, scope, and when not to use it.
- [Kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md) — the from-scratch consistency guarantee, its three conditions, modes, checkpoints, and limitations; the [action contract](https://github.com/Brumbelow/pyinc/blob/main/docs/action-contract.md) covers declared-output reconciliation.
- [Integration contract](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-contract.md) — stable entrypoints, result types, supported shapes, and limits.
- [`pyinc-tools` guide](https://github.com/Brumbelow/pyinc/blob/main/docs/pyinc-tools-guide.md) and [LSP reference](https://github.com/Brumbelow/pyinc/blob/main/docs/lsp-reference.md) — CLI, editor setup, overlays, protocol methods, and user-visible limitations.
- [Documentation index](https://github.com/Brumbelow/pyinc/blob/main/docs/README.md) — every document, the package map, and the authoring and codegen guides.
- [Releases and verification](https://github.com/Brumbelow/pyinc/blob/main/docs/releases.md) — signed tags, trusted publishing, and checking a download.

One distribution ships three top-level typed packages:

- `pyinc` — the stable query kernel, resources, snapshots, artifact stores, and declared-output actions
- `pyinc.integrations` — stable analysis results and entrypoints for Python source, configuration, dependencies, symbols, and notebooks
- `pyinc_tools` — unstable: `pyinc-tools analyze`, a polling watcher, `WorkspaceSession`, and a stdio LSP server
- `pyinc_codegen` — unstable: JSON Schema to typed Python generation

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
