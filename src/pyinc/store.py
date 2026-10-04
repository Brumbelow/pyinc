"""Content-addressed artifact storage.

The kernel writes serialized snapshot bytes keyed on `fingerprint_snapshot`
digests so external tools can persist or share kernel-produced values across
runs. The durable checkpoint API (`Database.save_checkpoint` /
`Database.load_checkpoint`) extends this with full node-record reuse: a
fresh process can reload a checkpoint and skip re-executing queries whose
declared inputs and resource probes are unchanged.
"""

from __future__ import annotations

import os
import re
import stat
import threading
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ._locking import FileLock, _validate_lock_timeout
from ._safe_fs import (
    UnsafeFilesystemPathError,
    atomic_write,
    ensure_directory,
    read_regular_file,
)
from .errors import ArtifactStoreError, ArtifactStoreKeyError, ArtifactStoreLockError

_STORE_KEY = re.compile(r"(?:[0-9a-f]{64}|ck[0-9a-f]{64})\Z")


def _validate_store_key(key: str) -> str:
    if type(key) is not str or _STORE_KEY.fullmatch(key) is None:
        raise ArtifactStoreKeyError(
            "Artifact-store keys must be a 64-character lowercase hexadecimal digest "
            "or 'ck' followed by such a digest."
        )
    return key


@runtime_checkable
class ArtifactStore(Protocol):
    """Content-addressed key/value store for serialized snapshot bytes.

    Implementations must:
    * Return ``None`` from :meth:`get` for missing digests (never raise).
    * Make :meth:`put` idempotent for equal byte payloads on the same digest.
    * Raise :class:`ValueError` from :meth:`put` if a digest is rebound to
      different bytes — silently keeping either value violates the soundness
      model and would mask corruption.
    """

    def get(self, digest: str) -> bytes | None:
        """Return the bytes previously stored under ``digest``, or ``None``."""
        raise NotImplementedError("ArtifactStore implementations must define get().")

    def put(self, digest: str, payload: bytes) -> None:
        """Persist ``payload`` under ``digest``. Idempotent on equal bytes."""
        raise NotImplementedError("ArtifactStore implementations must define put().")

    def contains(self, digest: str) -> bool:
        """Return ``True`` if ``digest`` is present. Default: ``get(...) is not None``."""
        return self.get(digest) is not None


class InMemoryArtifactStore:
    """In-process dict-backed store. Useful for tests and for retaining values
    beyond `Database(max_query_nodes=...)` LRU eviction within a single run.

    Databases on several threads may share one. `put` checks and stores under
    the store's lock: unlocked, two puts of one digest could both find it
    absent and the second overwrote the first, so a digest rebound to
    different bytes went unrefused. `keys` copies under the same lock, so its
    snapshot can be iterated while other threads store. `get` and `contains`
    are one dict operation each and need no lock. Each process uses a lock of
    its own, so a child forked while another thread held the parent's lock
    can still store. A copy, shallow or deep, and a pickle round trip hold
    their own items under a lock of their own.
    """

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}
        # One lock per process id, made on first use there. A thread that held
        # the parent's lock at a fork does not exist in the child, so the child
        # needs a lock of its own. The table lives on the instance: a module
        # registry would be mutable state in the methods a query's fingerprint
        # folds when it captures this class.
        self._locks: dict[int, threading.Lock] = {}

    def _process_lock(self) -> threading.Lock:
        pid = os.getpid()
        lock = self._locks.get(pid)
        if lock is None:
            # setdefault keeps a single lock when two threads arrive together.
            lock = self._locks.setdefault(pid, threading.Lock())
        return lock

    def get(self, digest: str) -> bytes | None:
        return self._items.get(digest)

    def put(self, digest: str, payload: bytes) -> None:
        if type(payload) is not bytes:
            raise TypeError("Artifact payloads must be bytes.")
        with self._process_lock():
            existing = self._items.get(digest)
            if existing is None:
                self._items[digest] = payload
                return
        if existing != payload:
            raise ValueError(
                f"Digest collision in InMemoryArtifactStore for {digest!r}: refusing to "
                "overwrite existing payload with different bytes."
            )

    def contains(self, digest: str) -> bool:
        return digest in self._items

    def keys(self) -> Mapping[str, bytes]:
        """A read-only snapshot of the stored payloads, by digest, taken at the call.

        Call it again to see later puts.
        """

        with self._process_lock():
            return MappingProxyType(dict(self._items))

    def __getstate__(self) -> dict[str, Any]:
        # Locks cannot be pickled or copied, so the state leaves them out. The
        # items are copied under the lock, so a put on another thread cannot
        # change them mid-read, and a copy never shares them with this store.
        with self._process_lock():
            state = dict(self.__dict__)
            state["_items"] = dict(self._items)
        state.pop("_locks", None)
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self.__dict__.pop("_lock", None)
        self._locks = {}


class FileSystemArtifactStore:
    """Disk-backed content-addressed store. Layout: ``<root>/objects/<digest[:2]>/<digest[2:]>``,
    with two-character fan-out so a workspace's worth of digests stays under
    common-filesystem directory-size limits. Per-digest process locks and
    no-follow same-directory atomic publication reject symlink and observed
    parent-rename races. As on other POSIX filesystem APIs, callers must not let
    non-cooperating processes rename the store root during a mutation."""

    def __init__(self, root: str | os.PathLike[str], *, lock_timeout: float = 30.0) -> None:
        lock_timeout = _validate_lock_timeout(lock_timeout)
        try:
            root_text = os.fspath(root)
            if "\0" in root_text:
                raise ValueError("embedded null character in path")
            self._root = Path(root_text).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise ArtifactStoreError(f"Artifact-store root path is invalid: {error}") from error
        self._objects = self._root / "objects"
        self._locks = self._root / "locks"
        self._lock_timeout = lock_timeout
        self._ensure_directory(self._root, create=True)
        self._ensure_directory(self._objects, create=True)
        self._ensure_directory(self._locks, create=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, digest: str) -> Path:
        digest = _validate_store_key(digest)
        return self._objects / digest[:2] / digest[2:]

    def _lock_path_for(self, digest: str) -> Path:
        digest = _validate_store_key(digest)
        return self._locks / digest[:2] / f"{digest[2:]}.lock"

    def _ensure_directory(self, path: Path, *, create: bool) -> bool:
        if create:
            try:
                ensure_directory(path)
            except UnsafeFilesystemPathError as error:
                raise ArtifactStoreError(
                    f"Artifact-store path is not a directory: {path}"
                ) from error
            except OSError as error:
                raise ArtifactStoreError(
                    f"Cannot safely create artifact-store directory: {path}"
                ) from error
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactStoreError(f"Artifact-store path is not a directory: {path}")
        resolved = path.resolve(strict=True)
        try:
            common = os.path.commonpath((os.fspath(self._root), os.fspath(resolved)))
        except ValueError as error:
            raise ArtifactStoreError(f"Artifact-store path escapes its root: {path}") from error
        if common != os.fspath(self._root):
            raise ArtifactStoreError(f"Artifact-store path escapes its root: {path}")
        return True

    def _object_state(
        self, digest: str, *, create_parent: bool
    ) -> tuple[Path, os.stat_result | None]:
        target = self._path_for(digest)
        if not self._ensure_directory(self._objects, create=False):
            raise ArtifactStoreError("Artifact-store objects directory is missing.")
        if not self._ensure_directory(target.parent, create=create_parent):
            return target, None
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            return target, None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ArtifactStoreError(f"Artifact-store object is not a regular file: {target}")
        return target, metadata

    def _prepare_lock(self, digest: str) -> Path:
        lock_path = self._lock_path_for(digest)
        if not self._ensure_directory(self._locks, create=False):
            raise ArtifactStoreError("Artifact-store locks directory is missing.")
        self._ensure_directory(lock_path.parent, create=True)
        return lock_path

    def get(self, digest: str) -> bytes | None:
        path, metadata = self._object_state(digest, create_parent=False)
        if metadata is None:
            return None
        try:
            return read_regular_file(path)
        except FileNotFoundError:
            return None
        except UnsafeFilesystemPathError as error:
            raise ArtifactStoreError(str(error)) from error

    def put(self, digest: str, payload: bytes) -> None:
        if type(payload) is not bytes:
            raise TypeError("Artifact payloads must be bytes.")
        target = self._path_for(digest)
        lock = FileLock(self._prepare_lock(digest), timeout=self._lock_timeout)
        try:
            lock.acquire()
        except TimeoutError as error:
            raise ArtifactStoreLockError(
                f"Timed out waiting to store artifact {digest!r}."
            ) from error
        except OSError as error:
            raise ArtifactStoreError(
                f"Cannot safely acquire the artifact lock for {digest!r}: {error}"
            ) from error
        try:
            target, metadata = self._object_state(digest, create_parent=True)
            try:
                existing = read_regular_file(target) if metadata is not None else None
            except FileNotFoundError:
                existing = None
            except UnsafeFilesystemPathError as error:
                raise ArtifactStoreError(str(error)) from error
            if existing is not None:
                if existing != payload:
                    raise ValueError(
                        f"Digest collision in FileSystemArtifactStore for {digest!r}: "
                        "refusing to overwrite existing payload with different bytes."
                    )
                return

            try:
                atomic_write(target, payload)
            except UnsafeFilesystemPathError as error:
                raise ArtifactStoreError(str(error)) from error
        finally:
            lock.release()

    def contains(self, digest: str) -> bool:
        _path, metadata = self._object_state(digest, create_parent=False)
        return metadata is not None
