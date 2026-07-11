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
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

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

    def put(self, digest: str, payload: bytes) -> None:
        """Persist ``payload`` under ``digest``. Idempotent on equal bytes."""

    def contains(self, digest: str) -> bool:
        """Return ``True`` if ``digest`` is present. Default: ``get(...) is not None``."""


class InMemoryArtifactStore:
    """In-process dict-backed store. Useful for tests and for retaining values
    beyond `Database(max_query_nodes=...)` LRU eviction within a single run."""

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}

    def get(self, digest: str) -> bytes | None:
        return self._items.get(digest)

    def put(self, digest: str, payload: bytes) -> None:
        if type(payload) is not bytes:
            raise TypeError("Artifact payloads must be bytes.")
        existing = self._items.get(digest)
        if existing is not None:
            if existing != payload:
                raise ValueError(
                    f"Digest collision in InMemoryArtifactStore for {digest!r}: refusing to "
                    "overwrite existing payload with different bytes."
                )
            return
        self._items[digest] = payload

    def contains(self, digest: str) -> bool:
        return digest in self._items

    def keys(self) -> Mapping[str, bytes]:
        return self._items


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
        except (OSError, TypeError, ValueError) as error:
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
