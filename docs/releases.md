# Releases and Verification

One maintainer publishes `pyinc`, so the release path is built for anyone to
check independently. Every file on PyPI traces to a signed commit at the tip of
`main`, and every check below runs in public CI.

## What the release workflow verifies

A release begins when a `v*` tag is pushed.
[`.github/workflows/release.yml`](../.github/workflows/release.yml) publishes
only when all of the following hold.

### The signing key

The workflow imports
[`.github/release-signing-key.asc`](../.github/release-signing-key.asc),
requires it to hold a single primary key, and asserts that its fingerprint is:

```
2B6DF408BD973740052925DC894C75E1B1D05EA2
```

### The tag and the history behind it

The tag must be annotated, carry a good signature from that key, and point at
the current tip of `main`.
[`scripts/verify_signed_history.py`](../scripts/verify_signed_history.py) then
verifies each commit from a pinned trusted baseline up to the tag against the
same key. An unsigned or foreign commit anywhere in the released range fails the
release.

The workflow pins one structural exception: the pull-request merge
`3cf59c6f0a2a24ef8306a8a1ded35ac482024dbc`, created by GitHub's merge button. It
is accepted only because all its parents verify against the release key and its
tree is byte-identical to a parent's tree. All of its content therefore arrived
maintainer-signed. Any other unsigned or foreign commit, merge or otherwise,
fails the release. The same commit-range check runs in CI on every push to
`main`, so a violation surfaces on the push that introduces it, before the next
release.

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
VERSION=4.0.0
gh release download "v$VERSION" --repo Brumbelow/pyinc
sha256sum --check SHA256SUMS
```

To check the tag from a clone of the repository:

```console
gpg --import .github/release-signing-key.asc
git verify-tag "v$VERSION"
```

`git verify-tag` reports a good signature from the fingerprint above, followed
by `WARNING: This key is not certified with a trusted signature!`. That warning
is expected after a bare `gpg --import`. It means you have not personally
certified the key. The signature itself verified.

A matching fingerprint establishes continuity with the key pinned in this
repository: every release is signed by the same key that signed the ones before
it. It does not establish an independently certified maintainer identity. GPG's
warning is correct that nothing outside this repository vouches for the key.
`git verify-commit "v$VERSION^{commit}"` checks the commit the tag names in the
same way.
