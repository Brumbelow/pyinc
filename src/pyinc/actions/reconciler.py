"""Filesystem reconciliation: compare desired artifacts with real state and apply.

This is the only component in the layer that writes to disk, and it runs strictly
*outside* query evaluation. The reconciliation is content-driven (SHA-256 of the
actual on-disk bytes), per-file atomic (``tmp`` + :func:`os.replace`), and
ownership-aware (stale deletion is limited to files a previous successful run
recorded as owned). See ``docs/action-contract.md`` for the full contract.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from pyinc.runtime import is_query_active

from .artifacts import (
    ActionPlan,
    ActionResult,
    DesiredArtifactSet,
    digest_bytes,
)
from .errors import ActionLockError, ActionStateError
from .manifest import ActionManifest
from .paths import (
    is_deletion_target_safe,
    resolve_contained_target,
    sanitize_component,
)


def default_state_dir(output_root: str | os.PathLike[str], action_id: str) -> Path:
    """Recommended state-dir location: ``<output_root>/../.pyinc-actions/<action_id>``.

    The state directory holds the ownership manifest and the write lock; it lives
    *outside* the output root so the output tree contains only generated artifacts
    and the user's own files.
    """
    root = Path(output_root)
    return root.parent / ".pyinc-actions" / sanitize_component(action_id)


class FilesystemReconciler:
    """Reconciles a :class:`DesiredArtifactSet` to one output root.

    Bound to ``output_root`` (where artifacts are written) and ``state_dir``
    (where the manifest + lock live). A single reconciler models one action's
    state; do not point two actions at the same ``state_dir``.
    """

    def __init__(
        self,
        output_root: str | os.PathLike[str],
        *,
        state_dir: str | os.PathLike[str],
    ) -> None:
        self._root = Path(output_root)
        self._state_dir = Path(state_dir)
        self._manifest_path = self._state_dir / "manifest.json"
        self._lock_path = self._state_dir / "lock"

    # -- public API ---------------------------------------------------------

    def plan(self, desired: DesiredArtifactSet) -> ActionPlan:
        """Return the create/update/delete/unchanged plan without writing,
        deleting, renaming, updating the manifest, or touching any mtime."""
        self._validate_targets(desired)
        previous = self._load_manifest(desired)
        return self._compute_plan(desired, previous)

    def apply(self, desired: DesiredArtifactSet) -> ActionResult:
        """Reconcile ``desired`` to disk under a single-writer lock.

        Rejected with :class:`ActionStateError` if invoked during query
        evaluation. Order: stage+commit all creates/updates atomically, then
        perform stale deletions, then publish the manifest atomically. A failure
        before the manifest is published leaves the manifest unchanged and
        performs no deletions; the next run converges from on-disk content.
        """
        if is_query_active():
            raise ActionStateError(
                "FilesystemReconciler.apply() must not be called during query "
                "evaluation. Queries compute desired artifacts; reconcile them "
                "outside any db.get(...) call."
            )
        self._validate_targets(desired)
        with self._exclusive_lock():
            previous = self._load_manifest(desired)
            plan = self._compute_plan(desired, previous)
            written = self._stage_and_commit(desired, plan)
            deletions = self._delete_stale(plan)
            self._publish_manifest(desired)
            digests = tuple(sorted((a.path, a.digest) for a in desired.artifacts))
            return ActionResult(
                writes=tuple(written),
                deletions=tuple(deletions),
                unchanged=len(plan.unchanged),
                digests=digests,
            )

    # -- planning -----------------------------------------------------------

    def _compute_plan(
        self, desired: DesiredArtifactSet, previous: ActionManifest | None
    ) -> ActionPlan:
        creates: list[str] = []
        updates: list[str] = []
        unchanged: list[str] = []
        for artifact in desired.artifacts:
            existing = self._read_existing(self._root / artifact.path)
            if existing is None:
                creates.append(artifact.path)
            elif digest_bytes(existing) == artifact.digest:
                unchanged.append(artifact.path)
            else:
                updates.append(artifact.path)

        desired_paths = set(desired.paths)
        deletes: list[str] = []
        if previous is not None:
            for path in sorted(previous.owned_paths):
                if path in desired_paths:
                    continue
                if is_deletion_target_safe(self._root, path):
                    deletes.append(path)

        return ActionPlan(
            creates=tuple(sorted(creates)),
            updates=tuple(sorted(updates)),
            deletes=tuple(sorted(deletes)),
            unchanged=tuple(sorted(unchanged)),
        )

    def _read_existing(self, target: Path) -> bytes | None:
        """Return the bytes of an existing regular file, else ``None``.

        Symlinks and non-regular files at an output path are treated as absent so
        they are replaced (tamper repair). Reads actual bytes — never mtimes — so
        a manually corrupted owned output is detected and repaired even when query
        inputs are unchanged."""
        if target.is_symlink() or not target.is_file():
            return None
        try:
            return target.read_bytes()
        except OSError:
            return None

    # -- applying -----------------------------------------------------------

    def _stage_and_commit(self, desired: DesiredArtifactSet, plan: ActionPlan) -> list[str]:
        to_write = set(plan.creates) | set(plan.updates)
        written: list[str] = []
        for artifact in desired.artifacts:
            if artifact.path not in to_write:
                continue
            target = resolve_contained_target(self._root, artifact.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(target, artifact.content)
            written.append(artifact.path)
        return sorted(written)

    def _delete_stale(self, plan: ActionPlan) -> list[str]:
        deleted: list[str] = []
        for path in plan.deletes:
            if not is_deletion_target_safe(self._root, path):
                continue
            target = self._root / path
            with contextlib.suppress(FileNotFoundError):
                os.unlink(target)
            deleted.append(path)
            self._prune_empty_dirs(target.parent)
        return sorted(deleted)

    def _publish_manifest(self, desired: DesiredArtifactSet) -> None:
        manifest = ActionManifest(
            action_id=desired.action.action_id,
            output_root=os.fspath(self._root),
            tool=desired.action.tool,
            entries=tuple(sorted((a.path, a.digest) for a in desired.artifacts)),
        )
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._manifest_path, manifest.to_json_bytes())

    # -- state + io helpers -------------------------------------------------

    def _load_manifest(self, desired: DesiredArtifactSet) -> ActionManifest | None:
        try:
            payload = self._manifest_path.read_bytes()
        except FileNotFoundError:
            return None
        manifest = ActionManifest.from_json_bytes(payload)
        if os.path.abspath(manifest.output_root) != os.path.abspath(self._root):
            raise ActionStateError(
                f"Action manifest at {self._manifest_path} records output root "
                f"{manifest.output_root!r}, not {os.fspath(self._root)!r}. Refusing "
                "to reconcile against another root's ownership record."
            )
        if manifest.action_id != desired.action.action_id:
            raise ActionStateError(
                f"State dir {self._state_dir} belongs to action "
                f"{manifest.action_id!r}, not {desired.action.action_id!r}. Two "
                "actions must not share a state directory."
            )
        return manifest

    def _validate_targets(self, desired: DesiredArtifactSet) -> None:
        for artifact in desired.artifacts:
            # Raises SymlinkEscapeError for an escaping component; no writes.
            resolve_contained_target(self._root, artifact.path)

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".pyinc-tmp-")
        committed = False
        try:
            with os.fdopen(fd, "wb") as fp:
                fp.write(payload)
            os.replace(tmp_path, target)
            committed = True
        finally:
            if not committed:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_path)

    def _prune_empty_dirs(self, start: Path) -> None:
        root_real = os.path.realpath(self._root)
        current = start
        while True:
            current_real = os.path.realpath(current)
            if current_real == root_real or not current_real.startswith(root_real + os.sep):
                return
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    @contextlib.contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            raise ActionLockError(
                f"Another writer holds the action lock at {self._lock_path}. If no "
                "writer is running, the lock is stale — remove the file to recover."
            ) from None
        try:
            with os.fdopen(fd, "w") as fp:
                fp.write(f"pid={os.getpid()}\n")
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._lock_path)
