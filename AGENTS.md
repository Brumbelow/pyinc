# AGENTS.md

This file provides guidance to coding agents working in this repository.

## Development commands

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -e '.[dev]'

pytest -q                          # full test suite (default addopts: -q --tb=no)
pytest -q tests/test_runtime.py    # a single test file
pytest -q tests/test_properties.py::test_incremental_results_match_fresh_recomputation  # single test
python3 -m mypy src tests bench scripts  # strict mypy (see [tool.mypy] in pyproject.toml)
python3 -m ruff check src tests bench scripts  # lint (E,F,I,UP,B,SIM,TID; E501 ignored)
python3 scripts/check_docs.py      # offline links, anchors, examples, CLI, public API
```

Python ≥3.11 (the matrix is 3.11 / 3.12 / 3.13 / 3.14). `pyproject.toml` pins `target-version = "py311"` and `python_version = "3.11"`.

The installed console script is `pyinc-tools` (→ `pyinc_tools.cli:main`), with subcommands `analyze` and `lsp`.

## Releasing

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which first requires
reusable CI, CodeQL, and five-run benchmark gates. It then builds the sdist and
wheel, validates the exact wheel in a clean environment, publishes to PyPI via
trusted publishing (OIDC, no stored token), and creates or repairs a GitHub
Release containing those exact distributions and `SHA256SUMS`. The tag name must
equal the `pyproject.toml` `version` (e.g. `version = "3.0.0rc1"` →
`git tag -s v3.0.0rc1`), and the version bump must land together with its
`CHANGELOG.md` section cut in the same PR. The release workflow verifies the
annotated tag, configured signing-key fingerprint, every commit since the
trusted baseline, and the exact project version before publishing.

The 3.0 release is promoted in two stages:

1. Commit `3.0.0rc1` and its changelog, verify the signed commit, create and
   verify the signed annotated `v3.0.0rc1` tag, then push the commit and tag.
2. After the RC is published, install its artifacts in clean environments and
   review the benchmark/correctness report. Do not prepare the final release
   until every pyinc result matches a fresh run.
3. Make one direct child commit of `v3.0.0rc1` that changes only
   `pyproject.toml` and `CHANGELOG.md`. The `3.0.0` changelog section must contain
   this validation record, substituting the RC tag's full commit SHA:

   ```markdown
   ### Release validation

   - RC candidate: `v3.0.0rc1` at `<40-character RC commit SHA>`
   - [x] Clean installations from the published RC artifacts passed.
   - [x] The benchmark/correctness report was reviewed; every pyinc result matched a fresh run.
   - [x] Final promotion approved.
   ```

   Append the matching release reference after the existing changelog links:

   ```markdown
   [3.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0
   ```

4. Verify the final signed commit, create and verify the signed annotated
   `v3.0.0` tag, then push the commit and tag. The release workflow rejects the
   final tag unless the RC tag is signed by the configured key, the final commit
   is the RC's direct child, only the two metadata files changed, and the
   validation record is complete and names the exact RC commit.

## Packages in this repo — and the boundaries between them

The repository ships **three** Python packages, built as a single wheel. Which one you're editing matters:

- **`src/pyinc/`** — the stable kernel + shipped integrations. Pure-Python, stdlib-only, zero runtime dependencies. Carries the semver contract documented in `docs/kernel-contract.md`, `docs/integration-contract.md`, and `docs/action-contract.md`. The kernel surface is the pure query runtime **plus** the `@action` declared-output reconciliation layer (`Output`, `ReconcileResult`, `Action.reconcile`/`plan`): queries derive *desired* artifacts (pure, tracked); a separate action reconciles them with the filesystem. Side effects live only in the action layer, never in a query.
- **`src/pyinc_tools/`** — the consumer tooling layer (CLI, LSP server, polling watcher, `WorkspaceSession` with overlay/mirror). Builds **only** on the stable `pyinc.integrations` public surface.
- **`src/pyinc_codegen/`** — a consumer compiler: JSON-Schema → typed Python models, emitted through the `@action` layer. Stdlib-only; builds **only** on pyinc's public API (`@query`, `FileResource`, `Output`/`@action`). See `docs/codegen-guide.md`.

**Architectural invariant (do not violate unless the user explicitly asks you to widen the kernel contract):** LSP wiring and filesystem watchers live in `pyinc_tools`; JSON-Schema concepts live in `pyinc_codegen`; neither ever lands in `src/pyinc`, which stays domain-agnostic. Consumers build on the stable kernel — they do not widen it. This is stated in `docs/architecture.md` and reiterated in `docs/pyinc-tools-guide.md` / `docs/codegen-guide.md`. If a feature seems to require a kernel change, surface that as a trade-off question instead of silently broadening `src/pyinc`.

A reproducible benchmark + correctness harness lives under `bench/` (not shipped in the wheel; run `PYTHONPATH=src python -m bench.run`). Its only comparison dependency, `joblib`, sits in the `bench` optional-dependency group and is never imported by `src/pyinc` or `src/pyinc_codegen`.

## Kernel contract in one page

`pyinc` guarantees **from-scratch consistency** (incremental result == fresh-database result) only when all three conditions hold. When recomputation yields a semantically equal value, the record is **backdated** (early cutoff) so downstream dependents stay green.

1. **Value boundary ownership.** Everything crossing a cached boundary (query args, query returns, `Input` values) must be snapshot-safe: scalars, tuples, or values that `freeze` can deep-convert (`list`→`FrozenList`, `dict`→`FrozenDict`, `set`/`frozenset`→`FrozenSet`, dataclass→`FrozenRecord`), plus registered `ValueAdapter`s. Public dataclasses are `@dataclass(frozen=True)` with `tuple[T, ...]` fields — never `list`/`dict`/`set`.
2. **Tracked ambient reads.** Inside a query, the runtime intercepts `builtins.open` / `io.open`, `os.getenv`, `os.environ`, `os.listdir`, `os.scandir`, and `Path.iterdir` — any of these outside a `Resource`'s scope raises `UntrackedReadError`. For reads the guard can't see (`os.open`, C extensions, subprocess, network, time, random), the query must call `db.report_untracked_read(reason)`. The guard is installed **once globally** and dispatches per active `Database` via a `ContextVar` stack, so multiple `Database` instances across threads don't interfere.
3. **Deterministic queries.** Same tracked inputs ⇒ semantically equal return. Mutable closure/global captures and local/dynamically unbound type objects in query definitions are rejected when identity is established (normally the first `db.get()`); equality/cutoff policy captures and callable state must likewise be snapshot-safe. Preview classification via `pyinc.explain_query_captures(fn)` is available beforehand. Query, policy, resource, and adapter trust identities include the relevant interpreter version and build flags. Interpreter/build changes intentionally move the identity so checkpoints miss safely; only the `K2` snapshot byte grammar is cross-minor stable.

Modes (`strict` / `checked` / `fast`) control only what the *caller* sees at the boundary and whether in-query mutation is detected. Untracked-read interception, mutable-capture rejection, semantic-equality cutoff, and backdating are on in **all** modes. See `docs/kernel-contract.md` for the full table and the limitations (e.g. `os.open` bypass, ambient module monkey-patching).

## Integration architecture

Integrations in `src/pyinc/integrations/*.py` follow a strict three-layer shape (see `docs/integration-authoring.md` for the full pattern):

1. **Payload queries** — `@query` functions returning tuple-typed payloads. These are the kernel-cached nodes. They read via `Resource`s, parse, and return hashable tuples.
2. **Composition queries** — `@query` functions that call other queries and assemble composite tuple payloads. The kernel tracks cross-integration dependencies automatically; no wiring is needed. Example: `python_source` depends on `installed_packages.environment_index` to classify imports as `stdlib`/`installed`/`missing`.
3. **High-level entrypoints** — non-`@query` functions that call `db.get(...)` and decode tuples into the public frozen dataclasses.

Each payload shape has a `TypeAlias` and a `_decode_*` helper. Payload fields are optimized for snapshot-safe caching and may encode public values differently—for example, internal coordinate tuples decode into `SourceRange`. Decoding happens only at the public boundary.

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
