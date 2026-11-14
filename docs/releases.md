# Releases and Verification

One maintainer publishes `pyinc`, so the release path is built for anyone to
check independently. Every file on PyPI is built from the commit at the tip of
`main` that the release tag names, and every check below runs in public CI.

## What the release workflow verifies

A release begins when a `v*` tag is pushed.
[`.github/workflows/release.yml`](../.github/workflows/release.yml) publishes
only when all of the following hold.

### The tag

The tag must be annotated and point at the current tip of `main`.

### Release metadata

[`scripts/verify_release_metadata.py`](../scripts/verify_release_metadata.py)
requires:

- the tag name to equal the `pyproject.toml` version;
- a single non-empty `CHANGELOG.md` section for that version, whose heading
  carries a real calendar date in `YYYY-MM-DD` form;
- that version's release link at the foot of the changelog.

### The gates

All of these must pass before anything is built:

- the full test matrix: Python 3.11–3.14 and the free-threaded 3.14t build on
  Linux, macOS and Windows;
- static analysis;
- the full suite under branch coverage (reported, with no floor);
- the documentation check;
- CodeQL;
- a five-repetition run of the correctness and work-count benchmark.

### The artifacts

The sdist and wheel are built once. The wheel is installed into a clean virtual
environment, where [`scripts/validate_install.py`](../scripts/validate_install.py):

- requires the installed distribution to report the expected version;
- requires every one of its requirements to be gated behind an extra, so every
  runtime dependency is optional;
- runs the `pyinc-tools` console script and requires it to print that same
  version;
- imports every module of `pyinc`, `pyinc_codegen` and `pyinc_tools`;
- starts a language server, sends it an `initialize` request and checks the
  `serverInfo` in its reply;
- generates a package from a schema with a cyclic `$ref`, byte-compiles it, and
  imports it.

Four shipped examples then run against the installed package:
`examples/correctness_demo.py`, `examples/action_reconcile_demo.py`,
`examples/calc_demo.py` and `examples/codegen_demo.py`. The same script checks
that the sdist contains fifteen required paths, is free of compiled bytecode,
and carries the archive name the version calls for. The workflow publishes these
same validated files.

### Publication

Upload to PyPI uses trusted publishing over OIDC. No API token is stored in the
repository or in Actions secrets.

### The GitHub Release

The same sdist and wheel are attached to the release with a `SHA256SUMS` file
covering them. Before the release leaves draft, the workflow downloads its own
uploaded assets and compares their hashes with the local originals. It also
confirms that PyPI serves files with those same hashes.

## After publication

[`.github/workflows/published-artifacts.yml`](../.github/workflows/published-artifacts.yml)
runs only on manual dispatch (`workflow_dispatch`), with the published version
as input. It downloads the distributions from both PyPI and the GitHub Release
and checks each against `SHA256SUMS` and against the hashes the two services
report. It then installs that version from PyPI on all fifteen supported
operating-system and Python combinations, including the free-threaded 3.14t
build with the GIL disabled.

## Verifying a download yourself

`SHA256SUMS` on each GitHub Release covers the same sdist and wheel published
to PyPI. The commands below read the release from `VERSION`. Set it to the
version you are checking, without the leading `v`:

```console
VERSION=5.0.0
gh release download "v$VERSION" --repo Brumbelow/pyinc
sha256sum --check SHA256SUMS
```
