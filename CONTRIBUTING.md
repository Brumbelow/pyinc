# Contributing to pyinc

Thanks for your interest. This project keeps a narrow, well-defined kernel with
a soundness guarantee, so a bug report with a reproducer usually helps more than
a large feature branch.

## Before you open a pull request

For anything beyond a typo or a docs fix, please open an issue first. The kernel
carries a documented contract, and widening it is a trade-off decision to settle
on the issue before any patch.

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

Run these locally before pushing:

```console
pytest -q
python3 -m mypy src tests bench scripts examples
python3 -m mypy --platform win32 src tests bench scripts examples
python3 -m ruff check src tests bench scripts examples
python3 scripts/check_docs.py
```

`pytest` also takes a path or a node id to run one file or one test:
`pytest -q tests/test_runtime.py` or
`pytest -q tests/test_properties.py::test_incremental_results_match_fresh_recomputation`.

`check_docs.py` executes the Python examples embedded in the Markdown docs, so a
broken documented snippet fails the build. CI measures branch coverage and
prints the report for reading. Coverage has no floor and gates nothing.

The full matrix is Python 3.11–3.14, plus the free-threaded 3.14t build with the
GIL disabled, on Linux, macOS, and Windows. Windows is the platform most likely
to surface a path or file-locking difference. If you touch the artifact store,
the action layer, or the watcher, expect to iterate there.

## Architectural boundaries

The repository ships three packages as one wheel. Keep to these boundaries:

- `src/pyinc/`: the stable kernel, the shipped integrations, and the `@action`
  declared-output layer. Pure Python, stdlib only, zero runtime dependencies.
  Domain-agnostic.
- `src/pyinc_tools/`: CLI, LSP server, watcher, and `WorkspaceSession`. Builds
  only on the public `pyinc.integrations` surface.
- `src/pyinc_codegen/`: JSON Schema to typed Python. Builds only on pyinc's
  public API.

LSP and filesystem-watching concerns, and JSON Schema concepts, stay out of
`src/pyinc`. If a change seems to need a wider kernel, raise that as a question
on the issue and keep the kernel as it is in the pull request.

Queries stay pure. Filesystem writes belong only to the `@action` layer, which
reconciles a complete desired output set.

New public API needs a contract update in the same change:
[`docs/kernel-contract.md`](docs/kernel-contract.md),
[`docs/action-contract.md`](docs/action-contract.md), or
[`docs/integration-contract.md`](docs/integration-contract.md) as appropriate.

Public dataclasses are `@dataclass(frozen=True)` and use `tuple[T, ...]` for
collection fields (no `list`, `dict`, or `set`), because every value crossing a
cached boundary must be snapshot-safe.

## Adding an integration

Follow the three-layer pattern in
[`docs/integration-authoring.md`](docs/integration-authoring.md): payload
queries, composition queries, then high-level entrypoints that decode into
public frozen dataclasses. `examples/calc/` is the end-to-end example.

## Benchmarks

The reproducible benchmark and correctness harness lives in
[`bench/`](bench/README.md), outside the wheel. Correctness and deterministic
work counts are release gates. Wall-clock timings are environment-specific
diagnostics. `src/pyinc` and `src/pyinc_codegen` never import the harness's only
comparator dependency, `joblib`.

## Commits and releases

Write commit messages in the imperative mood, describing what changed and why.

The maintainer cuts releases. The tag name must equal the `pyproject.toml`
version, and a version bump lands in the same change as its `CHANGELOG.md`
section. The release workflow verifies every commit in the released range
against the release signing key. Those commits therefore reach `main` as a
fast-forward push of locally signed commits, bypassing the GitHub merge button.

The release workflow's structural allowlist pins one historical merge-button
commit and verifies it by shape (all parents signed by the release key, tree
identical to a parent). The workflow rejects any new merge-button commit, so
follow the fast-forward rule above. Ordinary pull requests are unaffected.
[`docs/releases.md`](docs/releases.md) describes the rest of the pipeline.

## Reporting a security issue

Report a vulnerability privately, as [SECURITY.md](SECURITY.md) describes,
and keep it out of public issues. A from-scratch consistency violation gets the
same seriousness, and that document says what to include.

---

The [documentation index](docs/README.md) maps each document to the one job it
does, and its [package map](docs/README.md#packages) is the shortest path to
how the packages fit together.
