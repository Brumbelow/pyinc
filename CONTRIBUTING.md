# Contributing to pyinc

Thanks for your interest. This project optimizes for a narrow, well-defined
kernel with a soundness guarantee, so the most useful contributions are usually
bug reports with a reproducer rather than large feature branches.

## Before you open a pull request

For anything beyond a typo or a docs fix, please open an issue first. The kernel
carries a documented contract, and a change that widens it is a trade-off
decision rather than a patch.

## Development setup

```console
git clone https://github.com/Brumbelow/pyinc.git
cd pyinc
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

Python 3.11 or newer is required. `pyproject.toml` pins `target-version =
"py311"` for Ruff and `python_version = "3.11"` for mypy. The installed console
script is `pyinc-tools`, with `analyze` and `lsp` subcommands; `python3 -m
pyinc_tools` is equivalent.

## What CI will check

Run all four locally before pushing:

```console
pytest -q
python3 -m mypy src tests bench scripts
python3 -m ruff check src tests bench scripts
python3 scripts/check_docs.py
```

`pytest` also takes a path or a node id, so a single file or a single test is
`pytest -q tests/test_runtime.py` or
`pytest -q tests/test_properties.py::test_incremental_results_match_fresh_recomputation`.

`check_docs.py` executes the Python examples embedded in the Markdown docs, so a
documented snippet that no longer runs is a build failure. Branch coverage must
stay at or above 90%.

The full matrix is Python 3.11–3.14 on Linux, macOS, and Windows. Windows is the
platform most likely to surface a path or file-locking difference; if you touch
the artifact store, the action layer, or the watcher, expect to iterate there.

## Architectural boundaries

The repository ships three packages as one wheel, and the boundaries are
load-bearing:

- `src/pyinc/` — the stable kernel, the shipped integrations, and the `@action`
  declared-output layer. Pure Python, stdlib only, zero runtime dependencies.
  Domain-agnostic.
- `src/pyinc_tools/` — CLI, LSP server, watcher, and `WorkspaceSession`. Builds
  only on the public `pyinc.integrations` surface.
- `src/pyinc_codegen/` — JSON Schema to typed Python. Builds only on pyinc's
  public API.

LSP and filesystem-watching concerns do not land in `src/pyinc`. JSON Schema
concepts do not land in `src/pyinc`. If a change appears to require widening the
kernel, please raise that as a question on the issue rather than broadening the
kernel in the pull request.

Queries stay pure. Filesystem writes belong to the `@action` layer, which
reconciles a complete desired output set — never to a query.

New public API needs a contract update in the same change:
[`docs/kernel-contract.md`](docs/kernel-contract.md),
[`docs/action-contract.md`](docs/action-contract.md), or
[`docs/integration-contract.md`](docs/integration-contract.md) as appropriate.

Public dataclasses are `@dataclass(frozen=True)` with `tuple[T, ...]` fields —
never `list`, `dict`, or `set` — because every value crossing a cached boundary
must be snapshot-safe.

## Adding an integration

Follow the three-layer pattern in
[`docs/integration-authoring.md`](docs/integration-authoring.md): payload
queries, composition queries, then high-level entrypoints that decode into
public frozen dataclasses. `examples/calc/` is the end-to-end example.

## Benchmarks

The reproducible benchmark and correctness harness lives in
[`bench/`](bench/README.md) and is not shipped in the wheel. Correctness and
deterministic work counts are release gates; wall-clock timings are
environment-specific diagnostics. Its only comparator dependency, `joblib`,
is never imported by `src/pyinc` or `src/pyinc_codegen`.

## Commits and releases

Write commit messages in the imperative mood, describing what changed and why.

Releases are cut by the maintainer. The tag name must equal the
`pyproject.toml` version, and a version bump lands together with its
`CHANGELOG.md` section in the same change. The release workflow verifies every
commit in the released range against the release signing key, so those commits
reach `main` as a fast-forward push of locally signed commits rather than
through the GitHub merge button. One historical merge-button commit is pinned
in the release workflow's structural allowlist and verified by shape (all
parents signed, tree identical to a parent); new merge commits are still
rejected, so the fast-forward rule above is the one to follow. This does not
affect ordinary pull requests.
[`docs/releases.md`](docs/releases.md) describes the rest of the pipeline.

## Reporting a security issue

See [SECURITY.md](SECURITY.md). Please do not open a public issue for a
vulnerability. A from-scratch consistency violation is handled with the same
seriousness, and that document says what to include.

---

The [documentation index](docs/README.md) maps each document to the one job it
does; [`docs/architecture.md`](docs/architecture.md) is the shortest path to how
the packages fit together.
