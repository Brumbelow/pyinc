# Releases and Verification

Every file on PyPI is built in public CI from the commit a release tag names.

## Cutting a release

1. On a branch, set `version` in `pyproject.toml` to `X.Y.Z`.
2. In `CHANGELOG.md`, move the `[Unreleased]` entries under
   `## [X.Y.Z] - YYYY-MM-DD` and add the version's release link at the foot.
3. Open a pull request and merge it once CI passes.
4. Run `git fetch origin`, `git tag -a vX.Y.Z -m "pyinc X.Y.Z" origin/main` and
   `git push origin vX.Y.Z`.
5. The Release workflow runs the full matrix, CodeQL and the benchmark. It then
   builds the sdist and wheel, publishes them to PyPI by trusted publishing and
   creates the GitHub Release. Merge nothing until it ends, because the build
   job checks that the tag is still the tip of `main`.
6. Run `gh workflow run published-artifacts.yml -f version=X.Y.Z`.

If the run fails before it publishes, fix the cause by pull request, run
`git push --delete origin vX.Y.Z` and `git tag -d vX.Y.Z`, and repeat step 4.
After the upload, `gh run rerun <run-id> --failed` finishes a failed run: the
upload skips files PyPI already has, and the last job completes a draft release.

## What the release workflow checks

[`release.yml`](../.github/workflows/release.yml) publishes only when:

- the suite passes on Linux, macOS and Windows with Python 3.11 to 3.14 and the
  free-threaded 3.14t, along with the strict property tests and static checks;
- CodeQL and a five-repetition run of the correctness and work-count benchmark
  pass;
- the tag is annotated, names the tip of `main` and equals the `pyproject.toml`
  version;
- [`verify_release_metadata.py`](../scripts/verify_release_metadata.py) finds
  one dated, non-empty `CHANGELOG.md` section and a release link for it;
- the wheel, installed in a clean environment, passes
  [`validate_install.py`](../scripts/validate_install.py) and runs four shipped
  examples.

Before the GitHub Release leaves draft, its assets and the files PyPI serves
must match the `SHA256SUMS` the run wrote. Only accounts with write access can
push a `v*` tag, and PyPI's trusted publisher accepts uploads only from
`release.yml` in the `pypi` environment. No API token is stored anywhere.

[`published-artifacts.yml`](../.github/workflows/published-artifacts.yml),
started by hand, checks the PyPI and GitHub Release files against `SHA256SUMS`
and installs the version from PyPI on 15 operating-system and Python
combinations.

## Verifying a download yourself

`SHA256SUMS` on each GitHub Release covers the sdist and wheel published to
PyPI. Set `VERSION` to the version you are checking, without the leading `v`:

```console
VERSION=6.0.0
gh release download "v$VERSION" --repo Brumbelow/pyinc
sha256sum --check SHA256SUMS
```

## The weekly schedule

GitHub turns the weekly schedule off after 60 days without repository activity,
and `gh workflow enable weekly.yml` turns it back on.
