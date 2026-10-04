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
import weakref
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
    are one dict operation each and need no lock. A child forked while another
    thread held the lock gets a new one: see `_new_store_locks_in_child`. A
    copy, shallow or deep, and a pickle round trip hold their own items under
    a lock of their own.
    """

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}
        self._lock = threading.Lock()
        _LIVE_STORES.add(self)

    def get(self, digest: str) -> bytes | None:
        return self._items.get(digest)

    def put(self, digest: str, payload: bytes) -> None:
        if type(payload) is not bytes:
            raise TypeError("Artifact payloads must be bytes.")
        with self._lock:
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

        with self._lock:
            return MappingProxyType(dict(self._items))

    def __getstate__(self) -> dict[str, Any]:
        # A lock cannot be pickled or copied, so the state leaves it out. The
        # items are copied under it, so a put on another thread cannot change
        # them while they are read, and a copy never shares them with this
        # store.
        with self._lock:
            state = dict(self.__dict__)
            state["_items"] = dict(self._items)
        del state["_lock"]
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()
        _LIVE_STORES.add(self)


# Every store still alive, so a forked child can reach each one's lock. Weak, so
# being listed here keeps no store alive.
_LIVE_STORES: weakref.WeakSet[InMemoryArtifactStore] = weakref.WeakSet()


def _new_store_locks_in_child() -> None:
    """Give every store a forked child inherits a lock of its own.

    A thread that held a store's lock when another thread forked does not exist
    in the child, so the child would wait on that lock forever in `put` or
    `keys`. The items are whole at the fork: each step taken under the lock
    leaves them consistent.
    """

    for store in list(_LIVE_STORES):
        store._lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_new_store_locks_in_child)


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
