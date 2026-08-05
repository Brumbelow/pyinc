# Releases and Verification

`pyinc` is published by a single maintainer, so the release path is built to be
checkable by someone who does not want to take that on trust. Every file on PyPI
is traceable to a signed commit at the tip of `main`, and every check below runs
in public CI.

## Required pre-tag candidate gate

Before creating a release tag, run
[`release-candidate.yml`](../.github/workflows/release-candidate.yml) from the
`main` ref with the exact version and full SHA of the proposed signed
release-closure commit. The selected workflow ref, the requested SHA, and the
remote tip of `main` must all name that same commit. The workflow then reuses
the complete CI, CodeQL, benchmark, and mutation workflows and checks the
maintainer-signed linear history, version and changelog metadata, hash-bound
independent-assurance record, candidate-to-closure diff, and public `P0`/`P1`
issue count before a tag exists.

```console
gh workflow run release-candidate.yml --ref main \
  --field version='<exact-version>' \
  --field release_commit='<40-character-release-closure-SHA>'
gh run watch '<run-id>' --exit-status
```

The second command must report a successful complete run before the signed tag
is created.

The tag-triggered release workflow queries Actions for a successful manual
candidate run with that exact commit SHA and refuses publication without one.
This is a mechanical publication precondition, not a way for repository CI to
prevent a maintainer from creating or pushing a Git ref: the signed tag does not
exist during preflight, so its object type, target, name, and signature are
checked only after it is pushed. PyPI and GitHub Release hash parity likewise
cannot run until publication and remains an automatic post-publication gate.
Enforcing the literal rule that no tag can be pushed before preflight requires
an external protected-tag/ruleset or maintainer process in addition to these
repository workflows.

## What the release workflow verifies

A release begins only after the pre-tag gate succeeds and a `v*` tag is pushed.
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
commit from the signed `v3.1.0` trust anchor
(`8db85ec7a647bd7b74bf04f07e94a9bd78675193`) up to the tag is then verified
against the same key individually, and the range must be linear. The anchor may
advance only to a release that passed this gate and was successfully published.
An unsigned or foreign commit anywhere in the released range fails the release
instead of shipping inside it.

GitHub's **Verified** badge is not this trust decision. A commit signed by
GitHub's web-flow key is cryptographically signed, but it is still foreign to
the maintainer-only release key above and is rejected. Pull requests remain the
review vehicle; release-line commits land as a fast-forward of the reviewed,
locally signed commits rather than as GitHub-generated merge commits.

**Release metadata.**
[`scripts/verify_release_metadata.py`](../scripts/verify_release_metadata.py)
requires the tag name to equal the `pyproject.toml` version and requires exactly
one non-empty `CHANGELOG.md` section for that version with a syntactically and
calendar-valid `YYYY-MM-DD` heading. It does not infer whether that date is the
maintainer's intended publication date.

**The gates.** Both the pre-tag check and tag-triggered release run the full test
matrix — Python 3.11–3.14 on explicit Linux, macOS,
and Windows runner-generation labels, each under two explicit hash seeds — plus
static analysis, branch coverage, the documentation check, bounded
soundness-critical mutation testing, CodeQL, and a five-repetition run of the
correctness and work-count benchmark must all pass before anything is built.
The scheduled external-link gate checks every public Markdown URL with live
`HEAD`/`GET` requests. Before the current version tag exists, a 404 from an
exact same-repository `v<project-version>` documentation or raw-asset URL is
accepted only when that URL maps to an existing regular file in the checkout;
all other URLs must be reachable. Tag, release, and uploaded-asset existence
remain post-publication checks.
Every directly invoked Python build, test, benchmark, analysis, and release
tool has one exact version in
[`requirements/toolchain.txt`](../requirements/toolchain.txt); project extras
and the build backend must match that manifest mechanically. These tools remain
separate from the wheel's zero runtime dependencies. The source-of-intent
manifest compiles to
[`requirements/toolchain.lock`](../requirements/toolchain.lock), a universal
transitive resolution with accepted SHA-256 artifact hashes. CI installs that
lock with pip's `--require-hashes --only-binary=:all:` so no package can open an
isolated sdist build path; the release provenance records the complete installed
resolution and binds both toolchain files by digest. The direct manifest also
pins the lock generator itself as `uv==0.11.21`.

GitHub-hosted runner labels select an operating-system generation, not an
immutable machine image. The release build therefore fails if GitHub does not
expose `ImageOS`, `ImageVersion`, `RUNNER_OS`, and `RUNNER_ARCH`, and records
those concrete values together with the selected `ubuntu-24.04` label in the
provenance. This makes the exact build image auditable after the run; it does
not turn GitHub's moving hosted-image label into an immutable image pin.

**The artifacts.** The sdist and wheel are built twice from separate clean
`git archive` source trees under the same pinned direct toolchain and
deterministic build environment. Both copies must be byte-identical; one
verified copy is then installed into a clean virtual environment and every
top-level `examples/*.py` program is
run against that installed package. The sdist is compared against every regular
file under its configured `src`, `tests`, `examples`, `docs`, `bench`, and
`release`, `requirements`, and `scripts` include roots plus the four included
root files; this is a complete configured-tree check, not a sentinel subset.
Those exact distributions, not a later rebuild, are what gets published.

**Independent assurance.** [`release/assurance.json`](../release/assurance.json)
binds two independent review records and the 30-day, three-project soak to the
exact candidate parent. The signed release commit may differ from that
candidate only by the assurance record; this metadata-only closure avoids a
commit-hash self-reference while keeping the reviewed code tree unchanged. Its
format and evidence requirements are defined in the
[release assurance record](release-assurance.md). The checked-in record is
deliberately blocked until those external events occur. Publication also fails
if a public GitHub issue labeled `P0` or `P1` remains open.

**Publication.** Upload to PyPI uses trusted publishing over OIDC. No API token
is stored in the repository or in Actions secrets.

**The GitHub Release.** The same sdist and wheel are attached to the release
alongside a `SHA256SUMS` file covering them. A versioned demo-evidence ZIP
records the exact release SHA and clean/dirty state, UTC capture time,
Python/OS and installed-distribution snapshot, every installed-wheel example's
command and raw stdout/stderr/exit status, source hashes, and hashes for every
bundle member. `DEMO-SHA256SUMS` binds that ZIP. Every release also carries a
versioned benchmark-evidence ZIP containing all 335 raw samples, both machine-
and human-readable summaries, environment and installed-distribution metadata,
the exact benchmark command, and hashes for every member;
`BENCHMARK-SHA256SUMS` binds it. The workflow downloads its own uploaded assets,
validates every checksum layer, and compares their bytes against the local
originals before the release leaves draft; it also confirms that PyPI is serving
distributions with the same hashes.

## Reproducible builds

[`scripts/reproducible_builds.py`](../scripts/reproducible_builds.py) requires a
clean checkout, derives `SOURCE_DATE_EPOCH` from the release commit, removes
uncontrolled Python and build-tool environment variables, builds two separate
source copies, and compares the complete wheel and sdist bytes. The proof is
scoped to two builds on the same concrete runner image with the same universal,
hash-locked tool resolution; it is not a claim that every platform or
independently provisioned machine will produce the same bytes.

The command also emits an SPDX 2.3 SBOM for the zero-runtime-dependency package
and an in-toto statement using the SLSA provenance v1 predicate. The statement
binds the source commit, direct-manifest and universal-lock digests, complete
installed-distribution snapshot, selected runner label, concrete hosted-runner
image identity, deterministic build parameters, and both artifact hashes.
`BUILD-METADATA-SHA256SUMS` covers the distributions and both metadata files.
These JSON files are transparent, unsigned provenance metadata produced inside
the release job; they are not a third-party or cryptographically signed build
attestation.

## After publication

[`.github/workflows/published-artifacts.yml`](../.github/workflows/published-artifacts.yml)
starts automatically when the GitHub Release is published. A manual
`workflow_dispatch` remains available for reruns of an exact version. The gate
downloads the distributions from both PyPI and the GitHub Release, checks each
against `SHA256SUMS` and against the hashes the two services report, and then
installs that exact version from PyPI across all twelve supported
operating-system and Python combinations.

## Verifying a download yourself

`SHA256SUMS` on each GitHub Release covers the same sdist and wheel that were
published to PyPI:

```console
tag="$(gh release view --repo Brumbelow/pyinc --json tagName --jq .tagName)"
gh release download "$tag" --repo Brumbelow/pyinc
sha256sum --check SHA256SUMS
sha256sum --check DEMO-SHA256SUMS
sha256sum --check BENCHMARK-SHA256SUMS
sha256sum --check BUILD-METADATA-SHA256SUMS
```

To check the tag from a clone of the repository:

```console
gpg --import .github/release-signing-key.asc
tag="$(gh release view --repo Brumbelow/pyinc --json tagName --jq .tagName)"
git fetch --tags origin "$tag"
git verify-tag "$tag"
```

`git verify-tag` reports a good signature from the fingerprint above, followed
by `WARNING: This key is not certified with a trusted signature!`. That warning
is expected after a bare `gpg --import`: GPG is saying you have not personally
certified the key, not that the signature failed to verify. Be precise about
what a matching fingerprint establishes: continuity with the key pinned in
this repository — every release signed by the same key that signed the ones
before it — not an independently certified maintainer identity. GPG's warning
is correct that nothing outside this repository vouches for the key.
`git verify-commit "$tag^{commit}"` checks the commit the tag names in the
same way.
