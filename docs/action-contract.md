# Action Contract: Declared-Output Reconciliation

Queries derive desired artifacts without side effects. An `Action` is the
top-level boundary that reconciles those artifacts with a filesystem. It runs
outside the query graph.

## Public surface

```python
from pyinc import (
    Action,
    ActionLockTimeoutError,
    ActionManifestError,
    ActionPathError,
    Output,
    ReconcileResult,
    action,
)
```

- `Output(path, content)` declares exact bytes at a root-relative POSIX path.
  `Output.text(...)` encodes text explicitly.
- `@action(tool="stable identity", lock_timeout=30.0)` wraps a pure desired-set
  function.
- `reconcile(..., root=..., state_dir=None, lock_timeout=None)` converges the
  filesystem. `plan(...)` runs the same preflight under the same lock and
  leaves the output root and ledger unchanged.
- `ReconcileResult` reports `created`, `updated`, `repaired`, `deleted`, and
  `unchanged` path tuples plus `dry_run`. v3 has no aggregate `written` field.

For a completed `reconcile`, `deleted` names the orphans the run removed. A
last-moment re-check leaves an entry in place, and out of `deleted`, when the
entry:

- vanished;
- has bytes that differ from the recorded digest;
- is now a different file from the one whose bytes were verified.

Under `dry_run=True` (`plan()`), `deleted` is the prediction.

`created` means the action claimed a previously absent output. `updated` means
the action changed an existing file to a new desired value. `repaired` means a
previously owned output was missing or had drifted from its recorded digest.
These fields make tamper recovery visible from the result, without inspecting
the filesystem.

## Preflight and portable paths

The action materializes and validates the complete desired iterable before
any write. Paths must be non-empty, normalized, relative POSIX file names.
Absolute, drive-qualified, UNC, backslash-containing, dot, traversal,
duplicate, and NUL-containing paths are rejected with `ActionPathError`.

The action rejects paths that collide after Unicode NFC normalization and case
folding. It also rejects a desired set that treats one output as both a file
and a directory (for example `pkg` and `pkg/model.py`). The ownership manifest
gets the same whole-set validation when it is read back.

A manifest entry can conflict with the new desired layout: a file where the
layout now needs a directory, or the reverse. That entry is an orphan of the
previous layout. The action deletes it before publishing the new set, so a
reconcile converges across a layout migration and never wedges on its own
ledger.

A case-only spelling change is the exception. A ledger entry whose portable
key matches a desired output is a collision. On a case-insensitive
filesystem, deleting it as an orphan would destroy the reconciled output.
That check misses one shape: an owned file replaced by outputs nested under a
case variant of its own name (`PKG` becoming `pkg/model.py`). On a
case-insensitive filesystem, target validation refuses that shape, because
the orphan still occupies the desired parent. Being a casefold twin does not
lift that validation. `plan` applies both refusals too. To converge, remove
the stale path by hand.

The root is resolved once. Every owned target is checked during preflight and
again immediately before a write or deletion. A desired target that sits
beneath a previous layout's orphan file at preflight is validated at write
time, after that orphan and any directories it emptied are removed. Existing
path components must not be symbolic links. All resolved parents must stay
under the root. An owned target must be a regular file. The action leaves in
place any orphan that has become a directory, device, or symbolic link.

Two states count as already released. The action leaves them in place and
raises no error:

- A recorded output that is now a directory, only when the desired layout
  nests outputs strictly beneath it and the directory holds only regular
  files of the desired set. Any other entry, or any symbolic link, keeps the
  refusal.
- A recorded output whose parent path is now a regular file, because no file
  can exist there.

A run leaves these states when it stops between publication and the ledger
write.

## Locking, publication, and recovery

Advisory cross-process locks protect the full preflight, write, delete, and
manifest sequence. The locks are keyed by the resolved root, state directory,
and full tool identity. The default timeout is 30 seconds. Set it on the
decorator or on each call. A timeout raises `ActionLockTimeoutError`. A
symlink, non-regular lock target, or other unsafe lock path raises
`ActionPathError` before any desired output is evaluated or mutated.

Changed files are flushed to temporary files in the same directory and
published atomically.

- On POSIX, parent directories are traversed with no-follow directory
  descriptors. Publication and deletion are relative to the opened directory.
- On Windows, every directory component is opened with
  `FILE_FLAG_OPEN_REPARSE_POINT`, validated as a non-reparse directory, and
  held without `FILE_SHARE_DELETE` until the operation completes. Temporary
  files are published with `SetFileInformationByHandle`. Orphans are marked
  for deletion through their already-validated handles.

So a concurrent symlink or junction swap cannot redirect either operation
outside the root. Immediately before publication or deletion, POSIX also
reopens an opened parent and compares its filesystem identity. A parent
renamed after traversal is rejected.

POSIX has no portable way to stop a hostile process from renaming a directory
between that identity check and the mutation. Keep non-cooperating processes
from renaming action roots concurrently. Orphan deletion has the same limit.
The unlink refuses an entry that is now a different file from the one whose
bytes were verified. POSIX has no unlink-by-inode, though, so the final
instant between that identity re-check and the unlink stays open. Keep
non-cooperating processes from replacing files under an action root
concurrently.

A reconcile runs in this order:

1. Delete validated orphans.
2. Prune directories that the previous layout's outputs left empty.
3. Publish desired files.
4. Publish the new ledger.

Each file is atomic. The set as a whole is not transactional, by design. A
process can stop mid-run: after deletions, after a prune, or after
publication but before the ledger write. The next locked reconcile of the
desired set that run was publishing converges it, and recognizes the
recorded outputs the stopped run released.

Recovery never deletes to repair. Files the stopped run published but did not
record are unowned. The tamper policy refuses a desired set that would have to
remove them, such as a rollback to the recorded layout or a teardown. The
refusal holds until a reconcile of the published layout records them.

Rollback of already-published files and transactional directory swaps are out
of scope. Directory pruning serves layout migration only. It removes only
directories that orphan deletion left empty. A directory that still holds an
unowned entry is refused with `ActionPathError`, and stays. Preflight makes
that decision, so `plan()` reports it and a reconcile refuses before deleting
anything.

## Ownership manifest

Each tool owns one manifest under `state_dir` (the root by default):

```text
.pyinc-action.<sha256-of-full-tool-identity>.json
```

Schema v3 records only `root`, `root_incarnation`, `tool`, `version`, and
`outputs`. The root digest ties an external state directory to one output
root. The full tool string is verified on every read.

The root incarnation is the device and inode of the root directory at write
time. It detects a root that was deleted and recreated at the same path. In
that case the recorded claims name files in a directory that is gone, so they
are void. The action adopts the current directory fresh and deletes nothing.
Detection is best-effort, because a filesystem can give the recreated
directory its old inode. So deletion also requires the file's current SHA-256
digest to match the digest the ledger recorded.

Every output digest must be 64 lowercase hexadecimal characters. Unknown
fields, duplicate JSON keys, wrong types, foreign identities, old schema
versions, malformed paths, and malformed hashes raise `ActionManifestError`
before mutation. By design, v1 and v2 manifests are incompatible with v3's
ledger semantics and may be discarded.

The ledger is validated but unauthenticated. It carries no proof of who wrote
it. A forged manifest under a writable `state_dir` can claim a regular
root-relative file. If the file's bytes match the digest the forgery records,
the next reconcile deletes it. Trust an external `state_dir` at least as much
as the output root itself.

An action deletes a file only when all of these hold:

- its own validated ledger records the file;
- the file still carries the exact bytes the ledger recorded;
- the file meets the regular-file rule in [Preflight and portable
  paths](#preflight-and-portable-paths).

An orphan whose content drifted from its recorded digest now belongs to the
user. The action releases the claim and the file survives. The same rule
holds at the instant of deletion. The unlink is pinned to the file identity
the last-moment verification read. An entry replaced under the same name
after verification, even by a byte-identical file, is a file this action
never wrote, so it survives. The final-instant limit in [Locking, publication,
and recovery](#locking-publication-and-recovery) applies. A drifted orphan
that stands where the desired layout needs a parent directory is refused with
`ActionPathError` and stays in place.

A no-op reconcile leaves the manifest byte-identical, with one exception.
When the recorded root incarnation differs from the root's, the action
replaces the voided claims with a fresh adoption of the current directory,
even though no output changed. A `plan()` under a mismatched incarnation
reports the post-adoption prediction. The result omits the adoption itself.

## Soundness boundary

The kernel's from-scratch consistency guarantee extends to owned output
files, under the conditions the rest of this contract sets out. Take a
reconcile that completes successfully, where:

- it covers the paths this action declares and its validated ledger records;
- `state_dir` is trusted;
- no unowned or drifted blocker refuses the run;
- no non-cooperating process is writing, replacing, or renaming anything
  under the root at the same time.

Its owned output paths and their bytes equal those a fresh reconcile of the
same desired set into an empty root produces. The action leaves alone every
file it neither declares nor owns, so the guarantee covers the owned set
only. A root that holds anything else differs, as a whole, from a fresh
empty-root reconcile. The guarantee excludes rollback across a set and
ownership coordination between tools that declare the same path.
