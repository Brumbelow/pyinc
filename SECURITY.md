# Security Policy

## Supported versions

Only the current release line is maintained. Every fix, security fixes
included, lands on that line alone. `pyinc` follows semantic versioning. That
contract covers what `pyinc` and `pyinc.integrations` export, and only that.
`pyinc_tools` and `pyinc_codegen` ship in the same distribution and are fully
documented, but they are **unstable**. Their exported names, the fields of
their result types, the `pyinc-tools` command-line surface and the LSP wire
behavior may change in any release, including a minor one.

| Version | Supported |
|---|---|
| 4.0.x | Yes |
| < 4.0 | No |

## Reporting a vulnerability

Please report privately through GitHub's
[private vulnerability reporting](https://github.com/Brumbelow/pyinc/security/advisories/new).
Keep vulnerability reports out of public issues.

Expect an acknowledgement within 5 business days and an assessment within 10.
If a fix is warranted, the release notes credit you, unless you prefer
otherwise.

## Reporting a soundness violation

A from-scratch consistency violation is an incremental result that differs
from a fresh evaluation on the same declared inputs and resources. We treat it
as seriously as a security issue, because downstream tools trust that
guarantee.

Before reporting, please confirm that your reproducer meets the three
conditions in
[the kernel contract](docs/kernel-contract.md#conditions-for-from-scratch-consistency):
owned value boundaries, tracked ambient reads, and deterministic queries. A
violation that meets all three is a kernel bug, and we want it. If your
reproducer misses a condition but the failure was hard to diagnose, please
still open an issue. That usually means a guard or a diagnostic should be
better.

Report a soundness violation through the same private channel if you believe
the impact is security-relevant, and as a public issue otherwise.

## Scope

`pyinc` is a library with no network surface and no runtime dependencies. The
security-relevant boundaries are:

- The durable checkpoint trust boundary. Checkpoint manifests and
  artifact-store bytes are validated before use. The kernel contract documents
  what is trusted. Validation checks integrity relative to the checkpoint key.
  It does not establish provenance, so loading a checkpoint key or store from
  an untrusted source is unsupported.
- The action layer's ownership ledger. Actions reconcile files on disk under a
  validated ledger. The ledger is unauthenticated, so trust an external
  `state_dir` at least as much as the output root. Sharing an output root with
  a non-cooperating process is unsupported.
- `fast` mode, which skips the in-query mutation check by documented design.

Behavior listed as a limitation in
[the kernel contract](docs/kernel-contract.md#explicit-limitations) is outside
vulnerability scope. Reports that a limitation is understated or poorly
signposted are welcome.

## Release integrity

Releases are published to PyPI from a tagged, signed commit through trusted
publishing, without a stored API token. The release workflow verifies the
annotated tag and every commit in the released range against the key in
`.github/release-signing-key.asc`.

The check has one pinned exception, the merge `3cf59c6`. It is accepted only
when every parent verifies against that key and its tree equals a parent's
tree, so every released byte still traces to maintainer-signed content.

Each GitHub Release carries a `SHA256SUMS` file covering the exact
distributions published to PyPI. After publication, a separate manually
dispatched workflow re-verifies that the two match.
[`docs/releases.md`](docs/releases.md) describes the pipeline and how to check
a download.
