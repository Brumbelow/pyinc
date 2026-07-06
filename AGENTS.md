# AGENTS.md

This file provides guidance to coding agents working in this repository.

## Development commands

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -e '.[dev]'

pytest -q                          # full test suite (default addopts: -q --tb=no)
pytest -q tests/test_runtime.py    # a single test file
pytest -q tests/test_runtime.py::test_incremental_results_match_fresh_recomputation  # single test
python3 -m mypy src tests          # strict mypy (see [tool.mypy] in pyproject.toml)
python3 -m ruff check src tests    # lint (E,F,I,UP,B,SIM,TID; line-length 100; E501 ignored)
```

Python ≥3.11 (the matrix is 3.11 / 3.12 / 3.13). `pyproject.toml` pins `target-version = "py311"` and `python_version = "3.11"`.

The installed console script is `pyinc-tools` (→ `pyinc_tools.cli:main`), with subcommands `analyze` and `lsp`.

## Releasing

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the sdist + wheel and publishes to PyPI via trusted publishing (OIDC, no stored token). The tag name must equal the `pyproject.toml` `version` (e.g. `version = "2.6.0"` → `git tag v2.6.0`), and the version bump must land together with its `CHANGELOG.md` section cut in the same PR.

## Packages in this repo — and the boundaries between them

The repository ships **three** Python packages, built as a single wheel. Which one you're editing matters:

- **`src/pyinc/`** — the stable kernel + shipped integrations. Pure-Python, stdlib-only, zero runtime dependencies. Carries the semver contract documented in `docs/kernel-contract.md`, `docs/integration-contract.md`, and `docs/action-contract.md`. The kernel surface is the pure query runtime **plus** the `@action` declared-output reconciliation layer (`Output`, `ReconcileResult`, `Action.reconcile`/`plan`): queries derive *desired* artifacts (pure, tracked); a separate action reconciles them with the filesystem. Side effects live only in the action layer, never in a query.
- **`src/pyinc_tools/`** — the consumer tooling layer (CLI, LSP server, polling watcher, `WorkspaceSession` with overlay/mirror). Builds **only** on the stable `pyinc.integrations` public surface.
- **`src/pyinc_codegen/`** — a consumer compiler: JSON-Schema → typed Python models, emitted through the `@action` layer. Stdlib-only; builds **only** on pyinc's public API (`@query`, `FileResource`, `Output`/`@action`). See `docs/codegen-guide.md`.

**Architectural invariant (do not violate unless the user explicitly asks you to widen the kernel contract):** LSP wiring and filesystem watchers live in `pyinc_tools`; JSON-Schema concepts live in `pyinc_codegen`; neither ever lands in `src/pyinc`, which stays domain-agnostic. Consumers build on the stable kernel — they do not widen it. This is stated in `docs/architecture.md` and reiterated in `docs/pyinc-tools-guide.md` / `docs/codegen-guide.md`. If a feature seems to require a kernel change, surface that as a trade-off question instead of silently broadening `src/pyinc`.

A reproducible benchmark + correctness harness lives under `bench/` (not shipped in the wheel; run `PYTHONPATH=src python -m bench.run`). Its only comparison dependency, `joblib`, sits in the `bench` optional-dependency group and is never imported by `src/pyinc` or `src/pyinc_codegen`.

## Kernel contract in one page

`pyinc` guarantees **from-scratch consistency** (incremental result == fresh-database result) only when all three conditions hold. When recomputation yields a semantically equal value, the record is **backdated** (early cutoff) so downstream dependents stay green.

1. **Value boundary ownership.** Everything crossing a cached boundary (query args, query returns, `Input` values) must be snapshot-safe: scalars, tuples, or values that `freeze` can deep-convert (`list→tuple`, `dict→FrozenDict`, `set→frozenset`, dataclass→`FrozenRecord`), plus registered `ValueAdapter`s. Public dataclasses are `@dataclass(frozen=True)` with `tuple[T, ...]` fields — never `list`/`dict`/`set`.
2. **Tracked ambient reads.** Inside a query, the runtime intercepts `builtins.open` / `io.open`, `os.getenv`, `os.environ`, `os.listdir`, `os.scandir`, and `Path.iterdir` — any of these outside a `Resource`'s scope raises `UntrackedReadError`. For reads the guard can't see (`os.open`, C extensions, subprocess, network, time, random), the query must call `db.report_untracked_read(reason)`. The guard is installed **once globally** and dispatches per active `Database` via a `ContextVar` stack, so multiple `Database` instances across threads don't interfere.
3. **Deterministic queries.** Same tracked inputs ⇒ semantically equal return. Mutable closure/global captures in query definitions are rejected at decoration time; preview classification via `pyinc.explain_query_captures(fn)` before the first `db.get()`. Query identity includes the supported function-definition payload (including immutable captures and the Python implementation + version tuple), so kernel digests are stable across CPython minor versions.

Modes (`strict` / `checked` / `fast`) control only what the *caller* sees at the boundary and whether in-query mutation is detected. Untracked-read interception, mutable-capture rejection, semantic-equality cutoff, and backdating are on in **all** modes. See `docs/kernel-contract.md` for the full table and the limitations (e.g. `os.open` bypass, ambient module monkey-patching).

## Integration architecture

Integrations in `src/pyinc/integrations/*.py` follow a strict three-layer shape (see `docs/integration-authoring.md` for the full pattern with file:line references into `python_source.py`):

1. **Payload queries** — `@query` functions returning tuple-typed payloads. These are the kernel-cached nodes. They read via `Resource`s, parse, and return hashable tuples.
2. **Composition queries** — `@query` functions that call other queries and assemble composite tuple payloads. The kernel tracks cross-integration dependencies automatically; no wiring is needed. Example: `python_source` depends on `installed_packages.environment_index` to classify imports as `stdlib`/`installed`/`missing`.
3. **High-level entrypoints** — non-`@query` functions that call `db.get(...)` and decode tuples into the public frozen dataclasses.

Each payload shape has a `TypeAlias` matching a dataclass's field order and a `_decode_*` helper. Tuples are snapshot-safe by default; decoding happens only at the public boundary.

`pyinc.integrations`' `__init__.py` re-exports **only** the stable dataclass/result types and high-level entrypoints. Low-level payload queries, decode helpers, and resource helpers are experimental and stay module-local — do not re-export them from `pyinc.integrations` without an explicit contract decision. `docs/integration-contract.md` enumerates the stable surface per integration.

## Thread safety

`Database` is thread-safe for concurrent use both across instances and on a single shared instance (per-instance `threading.RLock` serializes `get`/`set`/`set_many`/`inspect`/`explain`). Separate instances run in parallel. `WorkspaceSession` in `pyinc_tools` holds its own `RLock` guarding its public mutators; after `close()`, mutators raise `RuntimeError` so a live watcher thread exits cleanly.

## Docs to consult before non-trivial changes

- `docs/kernel-contract.md` — soundness envelope, mode table, limitations, escape hatches.
- `docs/action-contract.md` — the `@action` declared-output reconciliation contract (atomic writes, ownership ledger, tamper repair, dry-run).
- `docs/integration-contract.md` — per-integration stable public surface.
- `docs/integration-authoring.md` — the three-layer integration pattern with file:line pointers (and `examples/calc/` as the canonical end-to-end example).
- `docs/codegen-guide.md` — the `pyinc_codegen` JSON-Schema → Python compiler (reference consumer; public-API-only boundary).
- `docs/architecture.md` — scope and the `src/pyinc` ↔ consumer (`pyinc_tools`, `pyinc_codegen`) boundaries.
- `docs/pyinc-tools-guide.md` — LSP capabilities, `initializationOptions`, overlay/mirror model, supported vs. unsupported features.
- `CHANGELOG.md` — what changed per release (project adheres to SemVer + Keep a Changelog).
