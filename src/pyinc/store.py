"""Content-addressed artifact storage (Scope-A).

The kernel writes serialized snapshot bytes keyed on `fingerprint_snapshot`
digests so external tools can persist or share kernel-produced values across
runs. Scope-A only writes outbound; the kernel does not yet trust durable bytes
for from-scratch consistency. Scope-B (full node-record reuse) is deferred to
v2.1.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable


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
    common-filesystem directory-size limits. Writes are atomic via ``tempfile``
    in the same directory plus :func:`os.replace`."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        self._objects = self._root / "objects"
        self._objects.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, digest: str) -> Path:
        if len(digest) < 3:
            raise ValueError(f"Digest {digest!r} is too short for filesystem fan-out layout.")
        return self._objects / digest[:2] / digest[2:]

    def get(self, digest: str) -> bytes | None:
        path = self._path_for(digest)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def put(self, digest: str, payload: bytes) -> None:
        target = self._path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            existing = target.read_bytes()
            if existing != payload:
                raise ValueError(
                    f"Digest collision in FileSystemArtifactStore for {digest!r}: refusing to "
                    "overwrite existing payload with different bytes."
                )
            return

        # Atomic write: tmpfile in the same directory + os.replace.
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        replaced = False
        try:
            with os.fdopen(fd, "wb") as fp:
                fp.write(payload)
            os.replace(tmp_path, target)
            replaced = True
        finally:
            if not replaced:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_path)

    def contains(self, digest: str) -> bool:
        return self._path_for(digest).exists()
