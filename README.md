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
valid.

It is pure Python, stdlib-only, and has zero runtime dependencies. Python
3.11–3.14 are tested on Linux, macOS, and Windows.

```console
python -m pip install pyinc
```

## Why pyinc exists

`pyinc` applies the established red-green incremental query model — the
[Salsa](https://salsa-rs.github.io/salsa/) lineage that rust-analyzer is built
on — to Python. The model is not new, and incremental recomputation for Python
has a history of its own:
[IncPy](https://www.usenix.org/conference/tapp-10/towards-practical-incremental-recomputation-scientists-implementation-python)
explored it in 2010, [Adapton](https://matthewhammer.org/adapton/) listed a
Python implementation, and libraries such as
[Loman](https://pypi.org/project/loman/), [Darl](https://pypi.org/project/darl/),
and [Cascade Query](https://github.com/hmatt1/cascade-query) each cover parts
of this ground.

What `pyinc` adds is a soundness envelope for Python's mutable runtime, built
so the cache-invalidation bugs that usually come with a hand-rolled caching
layer — the editor still underlining an error you fixed a minute ago — have
somewhere to be caught: deep owned snapshots including shared and cyclic
graphs, guarded ambient reads with explicit resources, implementation-aware
query and resource identities, integrity-checked durable checkpoints,
declared-output reconciliation, and warm-versus-fresh differential testing
behind a documented consistency contract.
[The FAQ](https://github.com/Brumbelow/pyinc/blob/main/docs/faq.md)
covers how it compares with Salsa and with `functools.lru_cache`.

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

For files, environment variables, and directories, use a `Resource` rather
than reading ambient state directly inside a query. The
[getting-started guide](https://github.com/Brumbelow/pyinc/blob/main/docs/getting-started.md)
walks through inputs, resources, modes, inspection, and a first declared-output
action.

## See it on a real workspace

![Editing pytest under pyinc's watcher](https://raw.githubusercontent.com/Brumbelow/pyinc/main/docs/assets/demo.gif)

`pyinc-tools` was pointed at a pinned checkout of pytest — nothing in it adapted
for `pyinc` — and watched while single files were edited: in a single recorded
run, 109.08 s to analyze all 270 files from cold, then 632 ms to catch up after
an edit. Timings are machine-specific;
[the demo page](https://github.com/Brumbelow/pyinc/blob/main/docs/demo.md) has
the clips, the full provenance, and the deterministic work counts.

## Correctness contract

`pyinc` guarantees **from-scratch consistency**: incremental evaluation matches
a fresh evaluation on the same declared inputs and resources. The guarantee is
made provided three conditions hold; outside them, no guarantee is made:

1. **Owned value boundaries.** Query arguments, query results, and `Input`
   values are snapshot-safe or handled by a registered `ValueAdapter`.
2. **Tracked ambient reads.** External state read by a query goes through a
   `Resource`; reads the guard cannot intercept are declared with
   `db.report_untracked_read(reason)`.
3. **Deterministic queries.** The same tracked dependencies produce a
   semantically equal result, and custom `eq=`/`cutoff=` policies are
   substitutive for dependents — a coarser policy narrows the guarantee to
   consistency modulo the equivalence it declares.

The [kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md)
defines the exact value rules, intercepted operations, execution modes, durable
checkpoint trust boundary, and documented limitations.

Releases are cut from a signed tag whose whole commit range is signature-checked
and published through PyPI trusted publishing;
[releases and verification](https://github.com/Brumbelow/pyinc/blob/main/docs/releases.md)
shows how to verify a download.

## Packages

One distribution ships three top-level typed packages; the stable integration
surface is a subpackage of `pyinc`:

| Package | Purpose | Start here |
|---|---|---|
| `pyinc` | Stable query kernel, resources, snapshots, artifact stores, and declared-output actions. | [Kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md) |
| `pyinc.integrations` | Stable, frozen analysis results and high-level entrypoints for Python source, configuration, dependencies, symbols, and notebooks. | [Integration contract](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-contract.md) |
| `pyinc_tools` | `pyinc-tools analyze`, a polling watcher, `WorkspaceSession`, and a stdio LSP server built on the integration API. | [Tooling guide](https://github.com/Brumbelow/pyinc/blob/main/docs/pyinc-tools-guide.md) |
| `pyinc_codegen` | JSON Schema to typed Python generation through the public query and action APIs. | [Codegen guide](https://github.com/Brumbelow/pyinc/blob/main/docs/codegen-guide.md) |

Queries remain pure. Filesystem writes belong to the separate `@action` layer,
which reconciles a complete desired output set with atomic file replacement,
tamper repair, orphan cleanup, and dry-run planning. See the
[action contract](https://github.com/Brumbelow/pyinc/blob/main/docs/action-contract.md).

## Documentation

- [Getting started](https://github.com/Brumbelow/pyinc/blob/main/docs/getting-started.md) — build a small graph, add a tracked file, choose a mode, inspect work, and write a first action.
- [Demo](https://github.com/Brumbelow/pyinc/blob/main/docs/demo.md) — the watcher running on a real workspace.
- [FAQ](https://github.com/Brumbelow/pyinc/blob/main/docs/faq.md) — how this relates to Salsa, why not `lru_cache`, threading, and when not to use it.
- [Architecture](https://github.com/Brumbelow/pyinc/blob/main/docs/architecture.md) — package boundaries and how the kernel, integrations, tools, and codegen fit together.
- [Kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md) — the normative soundness envelope.
- [Action contract](https://github.com/Brumbelow/pyinc/blob/main/docs/action-contract.md) — declared-output reconciliation.
- [Integration contract](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-contract.md) — stable entrypoints, result types, supported shapes, and limits.
- [`pyinc-tools` guide](https://github.com/Brumbelow/pyinc/blob/main/docs/pyinc-tools-guide.md) and [LSP reference](https://github.com/Brumbelow/pyinc/blob/main/docs/lsp-reference.md) — CLI, editor setup, overlays, protocol methods, and user-visible limitations.
- [Integration authoring](https://github.com/Brumbelow/pyinc/blob/main/docs/integration-authoring.md) — the three-layer integration pattern.
- [Migrating from 2.x](https://github.com/Brumbelow/pyinc/blob/main/docs/migration-v3.md) — state cleanup and 3.0 API changes.
- [Releases and verification](https://github.com/Brumbelow/pyinc/blob/main/docs/releases.md) — signed tags, trusted publishing, and checking a download.

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

Run `python -m pyinc_tools --help` for the installed command-line tools. The
module form and the `pyinc-tools` console script are equivalent.
