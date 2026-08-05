from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from pyinc import Database, FileResource, FileSystemArtifactStore, InMemoryArtifactStore, query

_MODES = ("strict", "checked", "fast")
_FILES = FileResource()


@query(key="tests.platform-assurance.raw-file-v1")
def _raw_file(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@pytest.mark.parametrize("mode", _MODES)
def test_same_size_rewrite_with_coarse_timestamp_matches_warm_fresh_and_checkpoint(
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.txt"
    path.write_text("alpha", encoding="utf-8")
    original = path.stat()
    store = InMemoryArtifactStore()

    warm = Database(mode=mode, store=store)
    assert warm.get(_raw_file, str(path)) == "alpha"
    checkpoint = warm.save_checkpoint()

    path.write_text("bravo", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    rewritten = path.stat()
    assert (rewritten.st_size, rewritten.st_mtime_ns) == (
        original.st_size,
        original.st_mtime_ns,
    )

    fresh = Database(mode=mode)
    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(checkpoint)

    assert warm.get(_raw_file, str(path)) == "bravo"
    assert restored.get(_raw_file, str(path)) == "bravo"
    assert fresh.get(_raw_file, str(path)) == "bravo"


@pytest.mark.parametrize("mode", _MODES)
def test_filesystem_checkpoint_needs_no_symlink_creation_capability(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EPERM, "symlink creation disabled by test")

    monkeypatch.setattr(os, "symlink", unavailable, raising=False)
    with pytest.raises(OSError, match="symlink creation disabled by test"):
        (tmp_path / "unavailable-link").symlink_to(tmp_path / "target")

    source = tmp_path / "source.txt"
    source.write_text("regular-file", encoding="utf-8")
    store = FileSystemArtifactStore(tmp_path / "store")
    writer = Database(mode=mode, store=store)
    assert writer.get(_raw_file, str(source)) == "regular-file"
    checkpoint = writer.save_checkpoint()

    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)

    assert restored.get(_raw_file, str(source)) == "regular-file"
    assert fresh.get(_raw_file, str(source)) == "regular-file"
