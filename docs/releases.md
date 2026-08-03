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
same key individually, so an unsigned or foreign commit anywhere in the released
range fails the release instead of shipping inside it.

**Release metadata.**
[`scripts/verify_release_metadata.py`](../scripts/verify_release_metadata.py)
requires the tag name to equal the `pyproject.toml` version and requires exactly
one non-empty, correctly dated `CHANGELOG.md` section for that version.

**The gates.** The full test matrix — Python 3.11–3.14 on Linux, macOS, and
Windows — plus static analysis, branch coverage, the documentation check,
CodeQL, and a five-repetition run of the correctness and work-count benchmark
must all pass before anything is built.

**The artifacts.** The sdist and wheel are built once, installed into a clean
virtual environment, and exercised there by running the shipped examples against
the installed package. Those exact files, not a later rebuild, are what gets
published.

**Publication.** Upload to PyPI uses trusted publishing over OIDC. No API token
is stored in the repository or in Actions secrets.

**The GitHub Release.** The same sdist and wheel are attached to the release
alongside a `SHA256SUMS` file covering them. The workflow downloads its own
uploaded assets and compares their hashes against the local originals before the
release leaves draft, and it also confirms that PyPI is serving files with those
same hashes.

## After publication

[`.github/workflows/published-artifacts.yml`](../.github/workflows/published-artifacts.yml)
is dispatched with the published version. It downloads the distributions from
both PyPI and the GitHub Release, checks each against `SHA256SUMS` and against
the hashes the two services report, and then installs that exact version from
PyPI across all twelve supported operating-system and Python combinations.

## Verifying a download yourself

`SHA256SUMS` on each GitHub Release covers the same sdist and wheel that were
published to PyPI:

```console
gh release download v3.1.0 --repo Brumbelow/pyinc
sha256sum --check SHA256SUMS
```

To check the tag from a clone of the repository:

```console
gpg --import .github/release-signing-key.asc
git verify-tag v3.1.0
```

`git verify-tag` reports a good signature from the fingerprint above. `git
verify-commit v3.1.0^{commit}` checks the commit the tag names in the same way.
