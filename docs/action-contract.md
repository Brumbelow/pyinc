# Action Contract — Declared-Output Reconciliation

The kernel contract (`docs/kernel-contract.md`) governs *pure* incremental
evaluation: queries read tracked inputs and resources and return snapshot-safe
values. It says nothing about side effects, because queries have none. The
**action layer** is where derived results meet the filesystem.

## The split: queries derive, actions reconcile

- A **query** derives *desired* artifacts. It is pure and tracked: it reads
  inputs/resources, computes content, and returns it. Because `Output` is
  snapshot-safe (`path: str`, `content: bytes`), a `tuple[Output, ...]` may be
  the return value of a `@query` and participates in caching/backdating like
  any other value.
- An **action** (`@action(tool=...)`) reconciles a desired `Output` set against
  the filesystem. Reconciliation is the *only* place writes happen. Actions run
  at top level — never inside a query — so they do not pollute the dependency
  graph and the untracked-read guard (which only fires inside queries) is not
  involved.

This keeps the soundness envelope intact: nothing in the action layer changes
query semantics, the value membrane, untracked-read enforcement, or the
`strict`/`checked`/`fast` modes.

## Public surface

```python
from pyinc import Output, ReconcileResult, Action, action
```

- `Output(path: str, content: bytes)` — one declared output. `path` is relative
  to the reconcile root (POSIX). `Output.text(path, text, *, encoding="utf-8")`
  builds one from text.
- `@action(tool: str)` wraps `(db, *args) -> Iterable[Output]` into an `Action`.
- `Action.outputs(db, *args, **kwargs) -> tuple[Output, ...]` — the pure desired
  set (forwards to the wrapped function).
- `Action.reconcile(db, *args, root, dry_run=False, state_dir=None, **kwargs)
  -> ReconcileResult` — apply the desired set to `root`.
- `Action.plan(db, *args, root, state_dir=None, **kwargs) -> ReconcileResult` —
  dry-run reconcile (writes nothing).
- `ReconcileResult(written, deleted, unchanged, dry_run)` — root-relative POSIX
  path tuples describing the outcome.

`root`, `dry_run`, and `state_dir` are reserved keyword-only parameters of
`reconcile` / `plan`; the wrapped action's own positional `*args` and remaining
`**kwargs` are forwarded to it, so an action function must not declare
parameters named `root`, `dry_run`, or `state_dir`.

## The reconcile algorithm

For a desired set `{rel -> bytes}` against `root` (manifest under `state_dir`,
default `root`):

1. **Materialize** the desired set by calling the wrapped function. Duplicate
   relative paths are a `ValueError`; absolute paths or `..` escapes are a
   `ValueError`. The full set is computed *before* any write, so a failure in
   output computation writes nothing.
2. **Write what differs.** For each declared output, read the on-disk bytes and
   compare `sha256(on_disk)` to `sha256(desired)`. Equal → *unchanged* (no
   write). Missing or different → atomic rewrite (temp file in the target's
   directory, then `os.replace`). This single hash rule simultaneously gives:
   - unchanged output left untouched,
   - changed input rewrites only the affected outputs,
   - **out-of-band edits repaired** — a hand-edited or corrupted output hashes
     differently from the desired bytes and is rewritten on the next reconcile.
3. **Delete orphans.** Any path recorded in *our* manifest that the desired set
   no longer declares is deleted. We only ever delete paths we previously wrote;
   files the action does not own are never touched.
4. **Update the manifest** — but only if it changed. A no-op reconcile (nothing
   written, nothing deleted) leaves the manifest byte-identical and therefore
   performs **zero filesystem writes**.

## Ownership ledger (manifest) and tool identity

Orphan deletion needs to know what the action wrote last time. Each reconcile
reads/writes a deterministic JSON manifest `.pyinc-action.<tool-slug>.json`
under `state_dir`, namespaced by the action's **tool identity**:

```json
{ "tool": "pyinc-codegen", "version": 1, "outputs": { "a.py": "<sha256>", "docs/a.md": "<sha256>" } }
```

- **Tool identity is the ownership key.** Two different tools writing into the
  same root keep separate ledgers and never delete each other's files. Point two
  tools at overlapping outputs and they will fight — don't.
- **Tool *logic* sensitivity is free.** The kernel already fingerprints query
  function definitions, so changing the code that produces an output changes the
  output bytes and triggers a rewrite. The `tool` string is for *ownership*, not
  versioning; you do not need to bump it when logic changes.

## From-scratch consistency

The action layer preserves the kernel's headline guarantee, lifted to the
filesystem: **an incremental sequence of reconciles produces the same set of
output files (paths + bytes) as a single reconcile from a fresh `Database` into
an empty directory.** This is verified by edit-sequence tests that compare the
incremental output tree against a fresh run at every step
(`tests/test_action.py::test_action_incremental_matches_fresh_over_edits`, and
per-consumer tests in `tests/test_calc.py` / `tests/test_codegen.py`).

## Atomicity and failure behavior

- Writes are atomic per file (`tempfile.mkstemp` in the destination directory +
  `os.replace`), so a reader never observes a half-written output and a crash
  leaves either the old or the new bytes, never a truncated file.
- If output computation raises, nothing is written.
- If an individual write fails, its temp file is cleaned up; already-committed
  atomic writes remain (they are part of the desired set and self-heal on the
  next reconcile), and the manifest is left describing the prior ownership set so
  orphan tracking stays correct.

## Limitations / non-goals (v1 of the action layer)

- **No transactional rollback across outputs.** Each output is individually
  atomic; the set as a whole is not. A mid-reconcile failure may leave some
  outputs updated and others not — the next reconcile converges.
- **Empty directories are not pruned.** Deleting the last owned file in a
  generated subdirectory leaves the (now empty) directory in place. From-scratch
  consistency is therefore stated over *files*, which is the meaningful artifact
  equivalence.
- **Outputs are bytes.** Text producers encode to bytes (`Output.text`);
  hashing and atomic writes operate on bytes.
- **Reconciliation is top-level only.** Do not call `reconcile`/`plan` inside a
  query; actions are side effects, not derivations.
