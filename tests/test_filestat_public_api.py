from __future__ import annotations

from pathlib import Path

import pytest

from pyinc import Database, FileStatResource, FileStatSnapshot, InMemoryArtifactStore, query


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_file_stat_resource_returns_its_documented_type_in_every_mode(
    tmp_path: Path, mode: str
) -> None:
    resource = FileStatResource()
    path = tmp_path / "value.bin"
    path.write_bytes(b"data")
    db = Database(mode=mode)

    present = resource.read(db, path)
    assert type(present) is FileStatSnapshot
    assert (present.exists, present.size, present.mtime_ns is not None) == (True, 4, True)

    missing = resource.read(db, tmp_path / "missing.bin")
    assert type(missing) is FileStatSnapshot
    assert (missing.exists, missing.size, missing.mtime_ns) == (False, None, None)


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_file_stat_attributes_remain_warm_fresh_and_checkpoint_equivalent(
    tmp_path: Path, mode: str
) -> None:
    resource = FileStatResource()
    path = tmp_path / "value.bin"
    path.write_bytes(b"one")

    @query(key=f"filestat-public-{mode}")
    def stat_shape(db: Database, filename: str) -> tuple[bool, int | None]:
        snapshot = resource.read(db, filename)
        return snapshot.exists, snapshot.size

    store = InMemoryArtifactStore()
    warm = Database(mode=mode, store=store)
    assert warm.get(stat_shape, str(path)) == (True, 3)
    checkpoint = warm.save_checkpoint()

    loaded = Database(mode=mode, store=store)
    loaded.load_checkpoint(checkpoint)
    restored = resource.read(loaded, path)
    assert type(restored) is FileStatSnapshot
    assert (restored.exists, restored.size) == (True, 3)
    assert loaded.get(stat_shape, str(path)) == (True, 3)

    path.write_bytes(b"longer")
    warm_value = warm.get(stat_shape, str(path))
    loaded_value = loaded.get(stat_shape, str(path))
    fresh_value = Database(mode=mode).get(stat_shape, str(path))
    assert warm_value == loaded_value == fresh_value == (True, 6)
