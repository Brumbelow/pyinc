# Releases and Verification

`pyinc` is published by a single maintainer, so the release path is built to be
checkable by someone who does not want to take that on trust. Every file on PyPI
is traceable to a signed commit at the tip of `main`, and every check below runs
in public CI.

## What the release workflow verifies

A release begins when a `v*` tag is pushed.
[`.github/workflows/release.yml`](../.github/workflows/release.yml) refuses to
publish unless all of the following hold.

**The signing key.** The workflow imports
[`.github/release-signing-key.asc`](../.github/release-signing-key.asc),
requires it to contain exactly one primary key, and asserts that its fingerprint
is:

```
2B6DF408BD973740052925DC894C75E1B1D05EA2
```

**The tag and the history behind it.** The tag must be annotated, must carry a
good signature from that key, and must point at the current tip of `main`. Every
commit from a pinned trusted baseline up to the tag is then verified against the
same key individually by
[`scripts/verify_signed_history.py`](../scripts/verify_signed_history.py), so an
unsigned or foreign commit anywhere in the released range fails the release
instead of shipping inside it. The workflow pins one structural exception: the
pull-request merge `3cf59c6f0a2a24ef8306a8a1ded35ac482024dbc`, created by
GitHub's merge button, is accepted only because it is a merge whose parents all
verify against the release key and whose tree is byte-identical to a parent's
tree — it can introduce no content that did not itself arrive maintainer-signed.
Any other unsigned or foreign commit, merge or not, still fails the release. The
same commit-range check runs in CI on every push to `main`, so a violation
surfaces on the push that introduces it rather than at the next release.

**Release metadata.**
[`scripts/verify_release_metadata.py`](../scripts/verify_release_metadata.py)
requires the tag name to equal the `pyproject.toml` version, requires
exactly one non-empty `CHANGELOG.md` section for that version whose heading
carries a real calendar date in `YYYY-MM-DD` form, and requires that version's
release link at the foot of the changelog.

**The gates.** The full test matrix — Python 3.11–3.14 on Linux, macOS, and
Windows — plus static analysis, branch coverage, the documentation check,
CodeQL, and a five-repetition run of the correctness and work-count benchmark
must all pass before anything is built.

**The artifacts.** The sdist and wheel are built once. The wheel is installed
into a clean virtual environment, where
[`scripts/validate_install.py`](../scripts/validate_install.py) requires the
installed distribution to report the expected version and every one of its
requirements to be gated behind an extra, so the runtime has no hard
dependencies; runs the `pyinc-tools` console script and requires it to print
that same version; imports every module of `pyinc`, `pyinc_codegen` and
`pyinc_tools`; starts a language server, sends it an `initialize` request and
checks the `serverInfo` it answers with; and generates a package from a schema
with a cyclic `$ref`, byte-compiles it, and imports it. Four shipped examples
then run against the installed package: `examples/correctness_demo.py`,
`examples/action_reconcile_demo.py`, `examples/calc_demo.py` and
`examples/codegen_demo.py`. The same script checks the sdist for fifteen
required paths, for the absence of compiled bytecode, and for carrying the
archive name the version calls for. Those exact files, not a later rebuild, are
what gets published.

**Publication.** Upload to PyPI uses trusted publishing over OIDC. No API token
is stored in the repository or in Actions secrets.

**The GitHub Release.** The same sdist and wheel are attached to the release
alongside a `SHA256SUMS` file covering them. The workflow downloads its own
uploaded assets and compares their hashes against the local originals before the
release leaves draft, and it also confirms that PyPI is serving files with those
same hashes.

## After publication

[`.github/workflows/published-artifacts.yml`](../.github/workflows/published-artifacts.yml)
is manually dispatched (`workflow_dispatch`) with the published version — it
does not trigger automatically. It downloads the distributions from
both PyPI and the GitHub Release, checks each against `SHA256SUMS` and against
the hashes the two services report, and then installs that exact version from
PyPI across all twelve supported operating-system and Python combinations.

## Verifying a download yourself

`SHA256SUMS` on each GitHub Release covers the same sdist and wheel that were
published to PyPI. The commands below take the release from `VERSION`, so set it
to the one you are checking, without the leading `v`:

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
is expected after a bare `gpg --import`: GPG is saying you have not personally
certified the key, not that the signature failed to verify. Be precise about
what a matching fingerprint establishes: continuity with the key pinned in
this repository — every release signed by the same key that signed the ones
before it — not an independently certified maintainer identity. GPG's warning
is correct that nothing outside this repository vouches for the key.
`git verify-commit "v$VERSION^{commit}"` checks the commit the tag names in the
same way.
