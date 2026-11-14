# Contributing to pyinc

## Development setup

```console
git clone https://github.com/Brumbelow/pyinc.git
cd pyinc
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]' -c constraints.txt
```

Python 3.11 to 3.14 is required. `constraints.txt` pins the versions CI uses.

## Tests and static checks

```console
pytest -n auto -m 'not process' --ignore tests/test_properties.py --tb=short
python3 -m mypy src tests bench scripts examples
python3 -m mypy --platform win32 src tests bench scripts examples
python3 -m ruff check src tests bench scripts examples
python3 scripts/check_docs.py
```

`pyproject.toml` sets `--tb=no`, so pass `--tb=short` to see tracebacks. To run
one file or one test, name its path or its node id, as in
`pytest --tb=short tests/test_runtime.py::test_input_cutoff_suppresses_equal_updates`.
`check_docs.py` runs the Python examples in the docs and checks local links.

A pull request runs 21 CI jobs. The suite runs in four shards on each of Ubuntu
3.11, Ubuntu 3.14, Windows 3.14 and macOS 3.14. The strict property tests run in
three shards. One job runs the static checks and `pytest -m process`. One runs
the shipped examples against the built wheel. A release tag runs the suite on
3.11 to 3.14 and the free-threaded 3.14t on all three systems.

## Package boundaries

- `src/pyinc/`: the kernel, the integrations and the `@action` layer. Standard
  library only, with no runtime dependencies.
- `src/pyinc_tools/`: the CLI, the LSP server and the watcher, built on the
  public `pyinc.integrations` API only.
- `src/pyinc_codegen/`: JSON Schema to typed Python, built on pyinc's public
  API only.

A public API change updates its contract in `docs/` in the same pull request.

## Pull requests

Open a pull request against `main`. The maintainer reviews it, then merges or
closes it. Report a vulnerability privately, as [SECURITY.md](SECURITY.md)
describes.
