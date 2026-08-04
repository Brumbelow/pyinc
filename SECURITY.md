# Security Policy

## Supported versions

Fixes land on the latest published minor version; the previous minor receives
security fixes only. `pyinc` follows semantic versioning; everything the `pyinc`
package exports, and nothing else, carries that contract.

| Version | Supported |
|---|---|
| 3.1.x | Yes |
| 3.0.x | Security fixes only |
| < 3.0 | No |

## Reporting a vulnerability

Please report privately through GitHub's
[private vulnerability reporting](https://github.com/Brumbelow/pyinc/security/advisories/new)
rather than opening a public issue.

Expect an acknowledgement within 5 business days and an assessment within 10. If
a fix is warranted, you will be credited in the release notes unless you prefer
otherwise.

## Reporting a soundness violation

A **from-scratch consistency violation** — an incremental result that differs
from a fresh evaluation on the same declared inputs and resources — is treated
with the same seriousness as a security issue, because downstream tools trust
that guarantee.

Before reporting, please confirm that the three conditions in
[the kernel contract](docs/kernel-contract.md#conditions-for-from-scratch-consistency)
hold for your reproducer: owned value boundaries, tracked ambient reads, and
deterministic queries. A violation that holds all three is a kernel bug and we
want it. One that does not is still worth an issue if the failure mode was hard
to diagnose — that usually means a guard or a diagnostic should be better.

Report a soundness violation through the same private channel if you believe the
impact is security-relevant, and as a public issue otherwise.

## Scope

`pyinc` is a library with no network surface and no runtime dependencies. The
security-relevant boundaries are:

- **The durable checkpoint trust boundary.** Checkpoint manifests and
  artifact-store bytes are validated before use, and the trust envelope is
  documented in the kernel contract. Validation is integrity relative to the
  checkpoint key, not provenance: loading a checkpoint key or store from an
  untrusted source is outside the supported envelope.
- **The action layer's ownership ledger.** Actions reconcile files on disk under
  a validated ledger. The ledger is not authenticated, so an external
  `state_dir` must be trusted at least as strongly as the output root, and
  sharing an output root with a non-cooperating process is outside the
  envelope.
- **`fast` mode**, which by documented design does not check in-query mutation.

Behavior documented as a limitation in
[the kernel contract](docs/kernel-contract.md#explicit-limitations) is not a
vulnerability, but a report that a limitation is understated or badly signposted
is welcome.

## Release integrity

Releases are published to PyPI from a tagged, signed commit through trusted
publishing, with no stored API token. The release workflow verifies the
annotated tag and every commit in the released range against the key in
`.github/release-signing-key.asc`, each GitHub Release carries a `SHA256SUMS`
file covering the exact distributions published to PyPI, and a separate workflow
is dispatched after publication to re-verify that the two match.
[`docs/releases.md`](docs/releases.md) describes the pipeline and how to check a
download.
