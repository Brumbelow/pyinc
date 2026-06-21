## Action Contract — Declared Outputs and Safe Reconciliation

The action layer (`pyinc.actions`) turns the immutable values a query computes
into files on disk, safely and incrementally. It is **additive**: it consumes
query results and never changes kernel query-evaluation semantics. Queries
compute *desired artifacts*; the reconciler compares them with real filesystem
state and applies the difference **outside** any query.

This contract is versioned alongside the kernel contract. See
[`kernel-contract.md`](kernel-contract.md) for the query-evaluation guarantees
this layer builds on.

### The Model

| Type | Role |
|---|---|
| `DesiredArtifact(path, content, metadata)` | One immutable output: a normalized relative path, byte `content`, a derived SHA-256 `digest`, and optional deterministic `metadata`. |
| `ToolIdentity(name, version, schema_version, executable_digest?, config_digest?)` | Explicit, snapshot-safe identity of the producing tool. `schema_version` covers the output schema; the optional digests are caller-supplied. |
| `ActionIdentity(action_id, output_root, tool)` | Identity of one action and its declared output root. |
| `DesiredArtifactSet(action, artifacts)` | The complete set of outputs for one root. Normalizes, de-duplicates, and sorts on construction. |
| `ActionPlan(creates, updates, deletes, unchanged)` | A dry-run plan. Sorted tuples of relative paths. |
| `ActionResult(writes, deletions, unchanged, digests)` | The outcome of an applied reconciliation. |
| `ActionManifest(action_id, output_root, tool, entries)` | The durable ownership record, serialized as canonical JSON. |
| `FilesystemReconciler(output_root, *, state_dir)` | `plan()` (dry-run) and `apply()` (reconcile). |

Every artifact type is a `@dataclass(frozen=True)` with snapshot-safe fields.
Following the three-layer integration pattern, payload queries return plain tuple
payloads (e.g. `tuple[tuple[str, bytes], ...]`) and a non-`@query` entrypoint
decodes them into a `DesiredArtifactSet` — so the desired-artifact types keep
their concrete identity at the public boundary, exactly like other integration
result types. Reconciliation then consumes that set outside query evaluation.

### Guarantees

**1. Side-effect isolation.** Queries return desired values only; they never
create, modify, rename, or delete files. `FilesystemReconciler.apply()` raises
`ActionStateError` if called while a query is evaluating (it checks
`pyinc.is_query_active()`), so reconciliation from inside a query is structurally
rejected before any write. Filesystem inspection performed by `plan()`/`apply()`
happens outside query evaluation.

**2. Declared outputs and containment.** Every output is relative to one explicit
output root. The layer rejects, before touching disk: absolute paths, `..`
traversal, empty/whitespace paths, Windows drive/UNC prefixes
(`InvalidArtifactPathError`); duplicate normalized paths within a set
(`DuplicateArtifactError`); and any path that would be written *through* a symlink
escaping the root (`SymlinkEscapeError`). Generated identifiers used as filenames
are sanitized or rejected via `sanitize_component`.

**3. Ownership and stale deletion.** An action may delete only paths the previous
successful run recorded as owned in its manifest, that are absent from the new
declaration, that still exist as regular files, and whose real path resolves
inside the output root. Foreign/unowned files inside the root are never touched;
nothing outside the root is ever touched. Stale deletions run only **after** every
new/updated output is staged and committed.

**4. Atomicity and failure behavior.** Each output is written through a temporary
file in the destination directory and committed with `os.replace` (per-file
atomic). Temporary files are removed on failure. The manifest is published — also
atomically — only after reconciliation completes. A failed run publishes no
manifest and performs no deletion.

This layer provides **per-file atomicity and idempotent convergence**, not
whole-action transactional atomicity, and does not claim otherwise. A crash
between two file commits can leave a mix of old and new outputs while the manifest
still reflects the previous run. The next run **converges**: `plan()` compares the
SHA-256 of the *actual on-disk bytes* against the desired set, re-stages anything
that differs, completes pending deletions, then publishes the manifest. Because
convergence is content-driven (never mtime-driven), it is deterministic regardless
of where a previous run was interrupted.

**5. Hashing and no-op behavior.** Content comparison uses SHA-256 of the real
output bytes. Re-running with identical desired bytes and identical existing
output bytes performs zero writes and preserves output mtimes. A manually modified
or corrupted owned output is detected (its bytes no longer match) and repaired
even when query inputs are unchanged.

**6. Action identity.** `ToolIdentity` carries explicit, snapshot-safe tool and
schema-version data. The layer never discovers tool versions by running hidden
subprocesses. Configuration that changes output semantics should be reflected in
`schema_version` or `config_digest`.

**7. Dry-run.** `plan()` returns the exact create/update/delete/unchanged plan and
performs no write, delete, rename, manifest update, or mtime change.

**8. Determinism.** Artifacts are sorted by path; manifest serialization is
canonical (sorted keys, sorted entries, compact separators). The same desired set
and the same filesystem state always produce the same plan and the same manifest
bytes.

**9. Concurrency.** A single-writer contract is enforced by an exclusive lock file
created with `os.open(O_CREAT | O_EXCL)` in the state directory (portable across
POSIX and Windows). A concurrent `apply()` is rejected with `ActionLockError`
rather than corrupting the manifest. A lock left by a crashed process is stale and
must be removed to recover; the error message says so.

### State Layout

The ownership manifest (`manifest.json`) and the write lock (`lock`) live in a
`state_dir` **outside** the output root, so the output tree contains only
generated artifacts and the user's own files. `default_state_dir(output_root,
action_id)` returns the recommended location,
`<output_root>/../.pyinc-actions/<action_id>`.

### Explicit Limitations

- Not transactional across files (see guarantee 4). Convergence is the recovery
  mechanism, not rollback.
- The lock is advisory within this layer's contract: code that writes the output
  root without going through a reconciler is outside the model.
- Concurrency protection is per `(output_root, state_dir)`. Two reconcilers
  pointed at the same output root through *different* state directories are not
  coordinated.
- The layer reconciles bytes; it does not preserve or manage POSIX permissions,
  ownership, or extended attributes beyond what `os.replace` provides.
