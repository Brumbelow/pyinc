# Action Contract — Declared-Output Reconciliation

Queries derive desired artifacts without side effects. An `Action` is the
top-level boundary that reconciles those artifacts with a filesystem. It does
not run inside the query graph.

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
  filesystem. `plan(...)` runs the same preflight under the same lock but does
  not mutate the output root or ledger.
- `ReconcileResult` reports `created`, `updated`, `repaired`, `deleted`, and
  `unchanged` path tuples plus `dry_run`. There is no aggregate `written` field
  in v3.

For a completed `reconcile`, `deleted` names the orphans the run actually
removed: a last-moment re-check that leaves an entry in place — it vanished,
its bytes no longer match the recorded digest, or it is no longer the file
whose bytes were verified — leaves it out of `deleted`. Under `dry_run=True`
(`plan()`), `deleted` is the prediction.

`created` means the action claimed a previously absent output. `updated` means
an existing file was intentionally changed to a new desired value. `repaired`
means a previously owned output was missing or no longer matched its recorded
digest. These distinctions make tamper recovery observable without inspecting
the filesystem.

## Preflight and portable paths

The complete desired iterable is materialized and validated before any write.
Paths must be non-empty, normalized, relative POSIX file names. Absolute,
drive-qualified, UNC, backslash-containing, dot, traversal, duplicate, and
NUL-containing paths are rejected with `ActionPathError`.

The action rejects collisions after Unicode NFC normalization and case folding,
as well as a desired set that treats one output as both a file and a directory
(for example `pkg` and `pkg/model.py`). The ownership manifest receives the
same whole-set validation when it is read back. A manifest entry that conflicts
with the new desired layout — a file where the layout now needs a directory, or
the reverse — is not an error: it is an orphan of the previous layout, deleted
before the new set is published, so a reconcile converges across a layout
migration instead of wedging on its own ledger.

A case-only spelling change is the exception. A ledger entry whose portable key
matches a desired output is a collision rather than an orphan, because on a
case-insensitive filesystem deleting it would destroy the reconciled output. The
one shape that check does not see — an owned file replaced by outputs nested
under a case variant of its own name (`PKG` becoming `pkg/model.py`) — is
refused on a case-insensitive filesystem at target validation instead, since the
orphan still occupies the desired parent and a casefold twin does not lift that
validation. Both refusals apply to `plan` too; remove the stale path by hand to
converge.

The root is resolved once. Every owned target is checked during preflight and
again immediately before a write or deletion; a desired target still sitting
beneath a previous layout's orphan file at preflight is validated at write
time, after that orphan and any directories it emptied are removed. Existing path components may not
be symbolic links, all resolved parents must remain under the root, and an
owned target must be a regular file. An orphan that has become a directory,
device, or symbolic link is never deleted.

A recorded output that is now a directory is treated as already released —
never deleted, and not an error — only when the desired layout nests outputs
strictly beneath it and that directory holds nothing but regular files of the
desired set; any other entry, or any symbolic link, keeps the refusal. A
recorded output whose parent path is now a regular file is likewise already
released, because no file can exist there. These are the states a run leaves
when it stops between publication and the ledger write.

## Locking, publication, and recovery

The full preflight/write/delete/manifest sequence is protected by advisory
cross-process locks keyed by the resolved root, state directory, and full tool identity. The
default timeout is 30 seconds and can be configured on the decorator or each
call. A timeout raises `ActionLockTimeoutError`; a symlink, non-regular lock
target, or other unsafe lock-path failure raises `ActionPathError` before any
desired output is evaluated or mutated.

Changed files are flushed to same-directory temporary files and atomically
published. On POSIX, parent directories are traversed with no-follow directory
descriptors and publication/deletion is relative to the opened directory. On
Windows, every directory component is opened with
`FILE_FLAG_OPEN_REPARSE_POINT`, validated as a non-reparse directory, and held
without `FILE_SHARE_DELETE` until the operation completes. Temporary files are
published with `SetFileInformationByHandle`, and orphans are marked for deletion
through their already-validated handles. A concurrent symlink or junction swap
therefore cannot redirect either operation outside the root. POSIX additionally
reopens and compares an opened parent's filesystem identity immediately before
publication or deletion, rejecting a parent that was renamed after traversal.
POSIX has no portable mechanism that prevents a hostile process from renaming a
directory in the final interval between that identity check and the mutation;
action roots must therefore not be concurrently renamed by non-cooperating
processes. The same limit bounds orphan deletion: the unlink refuses an entry
that is no longer the file whose bytes were verified, but POSIX has no
unlink-by-inode, so the final instant between that identity re-check and the
unlink cannot be closed — files under an action root must likewise not be
concurrently replaced by non-cooperating processes. Validated orphans are
deleted first, directories that the previous layout's outputs left empty are
pruned second, desired files are published third, and the new ledger is
published last. Each file is atomic, but the set is deliberately not
transactional. If a process stops mid-run — after deletions, after a prune, or
after publication but before the ledger is written — the next locked reconcile
of the desired set that run was publishing converges it, recognizing the
recorded outputs the stopped run released. Recovery never deletes to repair:
files the stopped run published but did not record are unowned, so a desired
set that would have to remove them — a rollback to the recorded layout, or a
teardown — is refused under the tamper policy until a reconcile of the
published layout records them.

Rollback of already-published files and transactional directory swaps remain
out of scope. Directory pruning is limited to layout migration: only
directories that orphan deletion left empty are removed, and a directory still
holding an unowned entry is refused with `ActionPathError` rather than pruned.
That refusal is decided during preflight, so `plan()` reports it and a
reconcile refuses before deleting anything.

## Ownership manifest

Each tool owns one manifest under `state_dir` (the root by default):

```text
.pyinc-action.<sha256-of-full-tool-identity>.json
```

Schema v3 records exactly `root`, `root_incarnation`, `tool`, `version`, and
`outputs`. The root digest prevents one external state directory from being
reused across output roots, and the full tool string is verified on every
read. The root incarnation — the device and inode of the root directory at
write time — detects a root that was deleted and recreated at the same path:
the recorded claims name files in a directory that no longer exists, so they
are treated as void and the current directory is adopted fresh, deleting
nothing. Detection is best-effort — a filesystem can hand the recreated
directory its old inode straight back — which is why deletion additionally
requires that the file's current SHA-256 digest still matches the digest the
ledger recorded. Every output digest must be 64 lowercase hexadecimal
characters. Unknown fields, duplicate JSON keys, wrong types, foreign
identities, old schema versions, malformed paths, and malformed hashes raise
`ActionManifestError` before mutation. v1 and v2 manifests are intentionally
not compatible with v3's ledger semantics and may be discarded.

The ledger is validated, not authenticated: nothing in it proves who wrote
it. A forged manifest under a writable `state_dir` can claim a regular
root-relative file and — when the file's bytes match the digest the forgery
records — cause its deletion on the next reconcile, so an external
`state_dir` must be trusted at least as strongly as the output root itself.

An action deletes only files recorded by its own validated ledger, only while
they still carry the exact bytes the ledger recorded, and subject to the
regular-file-only constraint described in [Preflight and portable
paths](#preflight-and-portable-paths). An orphan whose content drifted from
its recorded digest is the user's file now: the claim is released and the file
survives. The same rule holds at the instant of deletion: the unlink is pinned
to the file identity the last-moment verification read, so an entry replaced
under the same name after verification — even by a byte-identical file — is a
file this action never wrote, and it survives (subject to the final-instant
limit stated in [Locking, publication, and
recovery](#locking-publication-and-recovery)). A drifted orphan standing where
the desired layout needs a parent directory is a refusal (`ActionPathError`)
rather than a deletion. The manifest is left byte-identical on a no-op
reconcile — except when the recorded root incarnation no longer matches the
root, in which case the voided claims are replaced by a fresh adoption of the
current directory even though no output changed. A `plan()` under a mismatched
incarnation reports the post-adoption prediction; the adoption itself is not
surfaced in the result.

## Soundness boundary

The kernel's from-scratch consistency guarantee lifts to owned output files,
under the conditions the rest of this contract establishes. For a reconcile
that completes successfully — over the paths this action declares and its
validated ledger records, with a trusted `state_dir`, no unowned or drifted
blocker refusing the run, and no non-cooperating process concurrently
writing, replacing, or renaming under the root — the owned output paths and
their bytes equal those a fresh reconcile of the same desired set into an
empty root produces. Files the action neither declares nor owns are never
touched, so the guarantee is scoped to the owned set: a root that holds
anything else is not, as a whole, equal to a fresh empty-root reconcile.
This does not imply rollback across a set or ownership coordination between
different tools that declare the same path.
