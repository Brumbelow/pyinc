from __future__ import annotations

import importlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyinc import Database
from pyinc._safe_fs import UnsafeFilesystemPathError
from pyinc.action import (
    Action,
    Output,
    _action_lock_directory,
    _holds_only_desired_outputs,
    _safe_target,
    _unprunable_entry,
    _validate_path_set,
    action,
)
from pyinc.errors import ActionPathError, ArtifactStoreError
from pyinc.store import FileSystemArtifactStore, InMemoryArtifactStore

action_module: Any = importlib.import_module("pyinc.action")
store_module: Any = importlib.import_module("pyinc.store")


def test_in_memory_store_rejects_nonbytes_and_exposes_keys() -> None:
    store = InMemoryArtifactStore()
    with pytest.raises(TypeError, match="must be bytes"):
        store.put("digest", cast(Any, bytearray(b"payload")))

    store.put("digest", b"payload")
    assert store.keys() == {"digest": b"payload"}


def test_filesystem_store_root_and_payload_type(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    assert store.root == (tmp_path / "store").resolve()
    with pytest.raises(TypeError, match="must be bytes"):
        store.put("a" * 64, cast(Any, memoryview(b"payload")))


def test_store_wraps_ordinary_directory_creation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")

    def fail_creation(_path: Path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store_module, "ensure_directory", fail_creation)
    with pytest.raises(ArtifactStoreError, match="safely create"):
        store._ensure_directory(tmp_path / "new", create=True)


def test_store_rejects_non_directory_during_observation(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    regular_file = store.root / "plain-file"
    regular_file.write_bytes(b"not a directory")

    with pytest.raises(ArtifactStoreError, match="not a directory"):
        store._ensure_directory(regular_file, create=False)


@pytest.mark.parametrize("result", ("raise", "outside"))
def test_store_rejects_commonpath_failure_or_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    observed = store.root / "observed"
    observed.mkdir()

    if result == "raise":

        def commonpath(_paths: object) -> str:
            raise ValueError("different drives")

    else:

        def commonpath(_paths: object) -> str:
            return os.fspath(tmp_path / "outside")

    monkeypatch.setattr(store_module.os.path, "commonpath", commonpath)
    with pytest.raises(ArtifactStoreError, match="escapes its root"):
        store._ensure_directory(observed, create=False)


def test_store_reports_missing_required_objects_directory(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    store._objects.rmdir()

    with pytest.raises(ArtifactStoreError, match="objects directory is missing"):
        store.get("a" * 64)


def test_store_reports_missing_required_locks_directory(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    store._locks.rmdir()

    with pytest.raises(ArtifactStoreError, match="locks directory is missing"):
        store.put("a" * 64, b"payload")


@pytest.mark.parametrize("failure", ("missing", "unsafe"))
def test_store_get_translates_read_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    digest = "a" * 64
    store.put(digest, b"payload")

    def fail_read(_path: Path) -> bytes:
        if failure == "missing":
            raise FileNotFoundError("removed after lstat")
        raise UnsafeFilesystemPathError("target changed type")

    monkeypatch.setattr(store_module, "read_regular_file", fail_read)
    if failure == "missing":
        assert store.get(digest) is None
    else:
        with pytest.raises(ArtifactStoreError, match="changed type"):
            store.get(digest)


@pytest.mark.parametrize("failure", ("missing", "unsafe"))
def test_store_put_handles_read_races_after_locking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    digest = "b" * 64
    target = store._path_for(digest)
    target.parent.mkdir()
    target.write_bytes(b"old")

    def fail_read(_path: Path) -> bytes:
        if failure == "missing":
            raise FileNotFoundError("removed after lstat")
        raise UnsafeFilesystemPathError("object changed type")

    monkeypatch.setattr(store_module, "read_regular_file", fail_read)
    if failure == "missing":
        store.put(digest, b"new")
        assert target.read_bytes() == b"new"
    else:
        with pytest.raises(ArtifactStoreError, match="changed type"):
            store.put(digest, b"new")
        assert target.read_bytes() == b"old"


def test_validate_path_set_detects_exact_duplicates_and_scans_nonprefixes() -> None:
    with pytest.raises(ActionPathError, match="Duplicate manifest path"):
        _validate_path_set(("same", "same"), source="manifest")

    assert _validate_path_set(("a", "a-", "b"), source="outputs") == {
        "a": "a",
        "a-": "a-",
        "b": "b",
    }


def test_action_lock_directory_without_numeric_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(action_module.tempfile, "gettempdir", lambda: os.fspath(tmp_path))
    monkeypatch.delattr(action_module.os, "getuid", raising=False)

    directory = _action_lock_directory()

    expected_identity = action_module.hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:16]
    assert directory == tmp_path.resolve() / f"pyinc-action-locks-{expected_identity}"
    assert directory.is_dir()


def test_action_lock_directory_resolves_a_symlinked_temporary_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_temporary_directory = tmp_path / "real"
    real_temporary_directory.mkdir()
    temporary_alias = tmp_path / "alias"
    try:
        temporary_alias.symlink_to(real_temporary_directory, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink support is unavailable")
    monkeypatch.setattr(action_module.tempfile, "gettempdir", lambda: os.fspath(temporary_alias))

    directory = _action_lock_directory()

    getuid = getattr(os, "getuid", None)
    uid = getuid() if getuid is not None else None
    identity = (
        str(uid)
        if uid is not None
        else action_module.hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:16]
    )
    assert directory == real_temporary_directory.resolve() / f"pyinc-action-locks-{identity}"
    assert directory.is_dir()


def test_action_lock_directory_rejects_a_non_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = os.getuid() if hasattr(os, "getuid") else None
    identity = (
        str(uid)
        if uid is not None
        else action_module.hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:16]
    )
    lock_path = tmp_path / f"pyinc-action-locks-{identity}"
    lock_path.write_bytes(b"hostile")
    monkeypatch.setattr(action_module.tempfile, "gettempdir", lambda: os.fspath(tmp_path))

    with pytest.raises(ActionPathError, match="not a directory"):
        _action_lock_directory()


def test_action_lock_directory_rejects_a_symlinked_private_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = os.getuid() if hasattr(os, "getuid") else None
    identity = (
        str(uid)
        if uid is not None
        else action_module.hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:16]
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    lock_path = tmp_path / f"pyinc-action-locks-{identity}"
    try:
        lock_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink support is unavailable")
    monkeypatch.setattr(action_module.tempfile, "gettempdir", lambda: os.fspath(tmp_path))

    with pytest.raises(ActionPathError, match="not a directory"):
        _action_lock_directory()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership metadata")
def test_action_lock_directory_rejects_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = os.getuid()
    directory = tmp_path / f"pyinc-action-locks-{uid}"
    directory.mkdir()
    real_lstat = Path.lstat

    def foreign_lstat(path: Path) -> object:
        metadata = real_lstat(path)
        return SimpleNamespace(st_mode=metadata.st_mode, st_uid=uid + 1)

    monkeypatch.setattr(action_module.tempfile, "gettempdir", lambda: os.fspath(tmp_path))
    monkeypatch.setattr(Path, "lstat", foreign_lstat)

    with pytest.raises(ActionPathError, match="owned by another user"):
        _action_lock_directory()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory modes")
def test_action_lock_directory_repairs_permissive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / f"pyinc-action-locks-{os.getuid()}"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    monkeypatch.setattr(action_module.tempfile, "gettempdir", lambda: os.fspath(tmp_path))

    assert _action_lock_directory() == directory
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_safe_target_rejects_a_regular_file_as_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "parent").write_bytes(b"not a directory")

    with pytest.raises(ActionPathError, match="parent is not a directory"):
        _safe_target(root, "parent/child")


@pytest.mark.parametrize("outcome", ("raise", "outside"))
def test_safe_target_rejects_lexical_commonpath_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    root = tmp_path / "root"

    if outcome == "raise":

        def commonpath(_paths: object) -> str:
            raise ValueError("different drives")

    else:

        def commonpath(_paths: object) -> str:
            return os.fspath(tmp_path / "outside")

    monkeypatch.setattr(action_module.os.path, "commonpath", commonpath)
    with pytest.raises(ActionPathError, match="escapes the action root"):
        _safe_target(root, "child")


@pytest.mark.parametrize("outcome", ("raise", "outside"))
def test_safe_target_rejects_resolved_parent_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    root = tmp_path / "root"
    calls = 0

    def commonpath(_paths: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return os.fspath(root)
        if outcome == "raise":
            raise ValueError("different drives")
        return os.fspath(tmp_path / "outside")

    monkeypatch.setattr(action_module.os.path, "commonpath", commonpath)
    with pytest.raises(ActionPathError, match="escapes the action root"):
        _safe_target(root, "parent/child")
    assert calls == 2


def test_action_releases_first_lock_if_second_lock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, Path]] = []

    class FakeLock:
        created = 0

        def __init__(self, path: Path, *, timeout: float) -> None:
            del timeout
            self.path = path
            type(self).created += 1
            self.number = type(self).created

        def acquire(self) -> None:
            events.append(("acquire", self.path))
            if self.number == 2:
                raise RuntimeError("unexpected lock backend failure")

        def release(self) -> None:
            events.append(("release", self.path))

    def lock_path(root: Path, _tool: str) -> Path:
        return root.with_name(f"{root.name}.lock")

    monkeypatch.setattr(action_module, "FileLock", FakeLock)
    monkeypatch.setattr(action_module, "_lock_path", lock_path)

    declared = Action(lambda _db: (), tool="lock-cleanup")
    with pytest.raises(RuntimeError, match="backend failure"):
        declared.reconcile(
            Database(),
            root=tmp_path / "root",
            state_dir=tmp_path / "state",
        )

    assert [name for name, _path in events] == ["acquire", "acquire", "release"]
    assert events[-1][1] == events[0][1]


def test_action_desired_map_rejects_invalid_values_and_duplicates() -> None:
    declared = Action(lambda _db: (), tool="invalid-outputs")
    with pytest.raises(TypeError, match="non-Output"):
        declared._desired_map([object()])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="must be bytes"):
        declared._desired_map([Output("path", cast(Any, bytearray(b"data")))])
    with pytest.raises(ActionPathError, match="Duplicate output path"):
        declared._desired_map([Output("path", b"one"), Output("path", b"two")])


def test_action_translates_unsafe_read_during_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "owned").write_bytes(b"current")
    declared = Action(lambda _db: (), tool="unsafe-read")
    monkeypatch.setattr(action_module, "_read_manifest", lambda *_args: (False, {}))

    def unsafe_read(_path: Path) -> bytes:
        raise UnsafeFilesystemPathError("target became unsafe")

    monkeypatch.setattr(action_module, "read_regular_file", unsafe_read)
    with pytest.raises(ActionPathError, match="target became unsafe"):
        declared._reconcile_locked(
            {"owned": b"desired"},
            root=root,
            state_dir=root,
            dry_run=True,
        )


def test_action_dry_run_ignores_already_missing_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "present").write_bytes(b"owned")
    digest = action_module._content_hash(b"owned")
    declared = Action(lambda _db: (), tool="missing-orphan")
    monkeypatch.setattr(
        action_module,
        "_read_manifest",
        lambda *_args: (True, {"missing": digest, "present": digest}),
    )

    result = declared._reconcile_locked({}, root=root, state_dir=root, dry_run=True)

    assert result.deleted == ("present",)


def test_action_rechecks_target_type_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    raced_directory = root / "owned"
    raced_directory.mkdir()
    metadata = raced_directory.stat()
    calls = 0

    def target_state(_root: Path, _relative: str) -> tuple[Path, os.stat_result | None]:
        nonlocal calls
        calls += 1
        return (raced_directory, None if calls == 1 else metadata)

    declared = Action(lambda _db: (), tool="write-race")
    monkeypatch.setattr(action_module, "_read_manifest", lambda *_args: (False, {}))
    monkeypatch.setattr(action_module, "_safe_target", target_state)

    with pytest.raises(ActionPathError, match="not a regular file"):
        declared._reconcile_locked(
            {"owned": b"payload"},
            root=root,
            state_dir=root,
            dry_run=False,
        )


@pytest.mark.parametrize("outcome", ("missing", "directory"))
def test_action_rechecks_orphan_before_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    regular = root / "owned"
    regular.write_bytes(b"owned")
    initial_metadata = regular.stat()
    directory = root / "replacement"
    directory.mkdir()
    replacement_metadata = directory.stat()
    calls = 0

    def target_state(_root: Path, _relative: str) -> tuple[Path, os.stat_result | None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return regular, initial_metadata
        if outcome == "missing":
            return regular, None
        return directory, replacement_metadata

    declared = Action(lambda _db: (), tool="delete-race")
    monkeypatch.setattr(
        action_module,
        "_read_manifest",
        lambda *_args: (True, {"owned": action_module._content_hash(b"owned")}),
    )
    monkeypatch.setattr(action_module, "_safe_target", target_state)

    if outcome == "missing":
        result = declared._reconcile_locked({}, root=root, state_dir=root, dry_run=False)
        assert result.deleted == ("owned",)
        assert regular.read_bytes() == b"owned"
    else:
        with pytest.raises(ActionPathError, match="non-regular owned target"):
            declared._reconcile_locked({}, root=root, state_dir=root, dry_run=False)
        assert directory.is_dir()


def test_unreadable_migration_directories_are_left_to_the_write_and_prune_steps(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"

    assert _holds_only_desired_outputs(missing, "missing", {"missing/model.py"}) is False
    assert _unprunable_entry(missing, "missing", set(), {}) is None


def test_action_supports_direct_decorator_form() -> None:
    def emit(_db: Database) -> tuple[Output, ...]:
        return (Output("result", b"payload"),)

    declared = action(emit, tool="direct-form")

    assert isinstance(declared, Action)
    assert declared.fn is emit
