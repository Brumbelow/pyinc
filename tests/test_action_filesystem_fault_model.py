from __future__ import annotations

import importlib
import os
import socket
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pyinc import ActionManifestError, ActionPathError, Database
from pyinc.action import Output, _manifest_path, action

action_module: Any = importlib.import_module("pyinc.action")
safe_fs_module: Any = importlib.import_module("pyinc._safe_fs")

_MODES = ("strict", "checked", "fast")


def _filesystem_snapshot(root: Path) -> tuple[tuple[str, int, bytes | str | None], ...]:
    """Capture an output tree without opening FIFOs, sockets, or devices."""
    if not root.exists():
        return ()
    pending = [root]
    entries: list[tuple[str, int, bytes | str | None]] = []
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as listing:
            for entry in listing:
                path = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                relative = path.relative_to(root).as_posix()
                kind = stat.S_IFMT(metadata.st_mode)
                if stat.S_ISDIR(metadata.st_mode):
                    payload: bytes | str | None = None
                    pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    payload = path.read_bytes()
                elif stat.S_ISLNK(metadata.st_mode):
                    payload = os.readlink(path)
                else:
                    payload = None
                entries.append((relative, kind, payload))
    return tuple(sorted(entries))


def _without_delete_quarantines(
    snapshot: tuple[tuple[str, int, bytes | str | None], ...],
) -> tuple[tuple[str, int, bytes | str | None], ...]:
    return tuple(
        entry for entry in snapshot if not entry[0].split("/", 1)[0].startswith(".pyinc-delete-")
    )


def _require_unix_socket_paths(tmp_path: Path) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix-domain sockets are unavailable")
    probe = tmp_path / "unix-socket-capability"
    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        candidate.bind(os.fspath(probe))
    except OSError as exc:
        pytest.skip(f"Unix-domain socket paths are unavailable: {exc}")
    finally:
        candidate.close()
        probe.unlink(missing_ok=True)


def _public_files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.startswith(".pyinc-action.")
    }


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("seam", "error_type"),
    (
        ("manifest-read", ActionManifestError),
        ("root-stat", ActionPathError),
        ("target-lstat", ActionPathError),
        ("orphan-read", ActionPathError),
        ("desired-read", ActionPathError),
    ),
)
def test_preflight_faults_are_typed_and_leave_tree_and_ledger_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    seam: str,
    error_type: type[Exception],
) -> None:
    wanted = {"keep.txt": b"old", "orphan.txt": b"owned"}

    @action(tool=f"fault-preflight-{seam}-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output(path, content) for path, content in sorted(wanted.items())]

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    before = _filesystem_snapshot(root)
    ledger_before = manifest.read_bytes()
    wanted.clear()
    wanted.update({"keep.txt": b"new", "new.txt": b"new"})

    original_read: Callable[[Path], bytes | None] = action_module.read_regular_file
    original_lstat = Path.lstat
    original_stat = Path.stat

    if seam in {"manifest-read", "orphan-read", "desired-read"}:
        denied = {
            "manifest-read": manifest,
            "orphan-read": root / "orphan.txt",
            "desired-read": root / "keep.txt",
        }[seam]

        def fail_read(path: Path) -> bytes | None:
            if path == denied:
                raise PermissionError(f"injected {seam}")
            return original_read(path)

        monkeypatch.setattr(action_module, "read_regular_file", fail_read)
    elif seam == "target-lstat":

        def fail_lstat(path: Path) -> os.stat_result:
            if path == root / "orphan.txt":
                raise PermissionError("injected target lstat")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_lstat)
    else:

        def fail_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == root:
                raise PermissionError("injected root stat")
            return original_stat(path, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", fail_stat)

    with pytest.raises(error_type):
        emit.reconcile(Database(mode=mode), root=root)

    assert _filesystem_snapshot(root) == before
    assert manifest.read_bytes() == ledger_before


@pytest.mark.parametrize("mode", _MODES)
def test_deletion_staging_failure_is_typed_and_premutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    wanted = True

    @action(tool=f"fault-delete-stage-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output("owned.txt", b"owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    before = _filesystem_snapshot(root)
    ledger_before = manifest.read_bytes()
    wanted = False

    def fail_delete(_path: Path, *, expected_digest: str | None = None) -> bool:
        del expected_digest
        raise PermissionError("injected quarantine allocation failure")

    monkeypatch.setattr(action_module, "unlink_regular_file", fail_delete)
    with pytest.raises(ActionPathError, match="Cannot safely delete"):
        emit.reconcile(Database(mode=mode), root=root)

    assert _filesystem_snapshot(root) == before
    assert manifest.read_bytes() == ledger_before


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine race injection")
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("replacement_kind", ("directory", "fifo", "socket", "symlink"))
def test_nonregular_replacement_before_quarantine_is_restored_without_ledger_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    replacement_kind: str,
) -> None:
    if replacement_kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    if replacement_kind == "socket":
        _require_unix_socket_paths(tmp_path)

    wanted = True

    @action(tool=f"fault-nonregular-delete-{replacement_kind}-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output("owned.txt", b"owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    ledger_before = manifest.read_bytes()
    wanted = False
    target = root / "owned.txt"
    original_rename = safe_fs_module.os.rename
    raced_snapshot: tuple[tuple[str, int, bytes | str | None], ...] | None = None
    held_socket: socket.socket | None = None

    def replace_before_quarantine(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal raced_snapshot, held_socket
        if source == "owned.txt" and destination == "payload" and raced_snapshot is None:
            target.unlink()
            if replacement_kind == "directory":
                target.mkdir()
            elif replacement_kind == "fifo":
                os.mkfifo(target)
            elif replacement_kind == "socket":
                held_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                held_socket.bind(os.fspath(target))
            else:
                target.symlink_to("foreign-target")
            raced_snapshot = _filesystem_snapshot(root)
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(safe_fs_module.os, "rename", replace_before_quarantine)
    try:
        with pytest.raises(
            ActionPathError,
            match="Deletion target changed|Cannot safely delete",
        ):
            emit.reconcile(Database(mode=mode), root=root)
    finally:
        if held_socket is not None:
            held_socket.close()

    assert raced_snapshot is not None
    assert _without_delete_quarantines(_filesystem_snapshot(root)) == _without_delete_quarantines(
        raced_snapshot
    )
    assert manifest.read_bytes() == ledger_before
    assert not list(root.glob(".pyinc-delete-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine race injection")
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("replacement_kind", ("directory", "fifo", "socket", "symlink"))
def test_quarantine_recovery_never_clobbers_a_late_live_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    replacement_kind: str,
) -> None:
    if replacement_kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    if replacement_kind == "socket":
        _require_unix_socket_paths(tmp_path)

    wanted = True

    @action(tool=f"fault-late-delete-race-{replacement_kind}-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output("owned.txt", b"owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    ledger_before = manifest.read_bytes()
    wanted = False
    target = root / "owned.txt"
    original_rename = safe_fs_module.os.rename
    original_restore = safe_fs_module._rename_noreplace
    held_socket: socket.socket | None = None
    late_replacement_created = False

    def replace_before_quarantine(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal held_socket
        if source == "owned.txt" and destination == "payload":
            target.unlink()
            if replacement_kind == "directory":
                target.mkdir()
            elif replacement_kind == "fifo":
                os.mkfifo(target)
            elif replacement_kind == "socket":
                held_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                held_socket.bind(os.fspath(target))
            else:
                target.symlink_to("foreign-target")
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def create_late_replacement(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal late_replacement_created
        target.write_bytes(b"late replacement")
        late_replacement_created = True
        original_restore(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(safe_fs_module.os, "rename", replace_before_quarantine)
    monkeypatch.setattr(safe_fs_module, "_rename_noreplace", create_late_replacement)
    try:
        with pytest.raises(ActionPathError) as raised:
            emit.reconcile(Database(mode=mode), root=root)
    finally:
        if held_socket is not None:
            held_socket.close()

    if late_replacement_created:
        assert "Cannot restore an interrupted deletion" in str(raised.value)
        assert target.read_bytes() == b"late replacement"
        quarantine = list(root.glob(".pyinc-delete-*"))
        assert len(quarantine) == 1
        assert (quarantine[0] / "payload").exists() or (quarantine[0] / "payload").is_symlink()
    else:
        assert "Cannot safely delete" in str(raised.value)
        assert replacement_kind == "socket"
        assert stat.S_ISSOCK(target.lstat().st_mode)
        assert not list(root.glob(".pyinc-delete-*"))
    assert manifest.read_bytes() == ledger_before


@pytest.mark.parametrize("mode", _MODES)
def test_partial_output_staging_failure_cleans_up_and_is_premutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    content = b"old"

    @action(tool=f"fault-output-stage-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output("owned.txt", content)]

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    before = _filesystem_snapshot(root)
    ledger_before = manifest.read_bytes()
    content = b"new"
    original_fsync = safe_fs_module.os.fsync

    def fail_regular_file_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("injected partial staging write")
        original_fsync(descriptor)

    monkeypatch.setattr(safe_fs_module.os, "fsync", fail_regular_file_sync)
    with pytest.raises(ActionPathError, match="Cannot atomically publish"):
        emit.reconcile(Database(mode=mode), root=root)

    assert _filesystem_snapshot(root) == before
    assert manifest.read_bytes() == ledger_before
    assert not list(root.glob(".tmp-*"))


@pytest.mark.parametrize("mode", _MODES)
def test_manifest_publish_failure_keeps_old_ledger_and_next_run_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    content = b"old"

    @action(tool=f"fault-manifest-publish-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output("owned.txt", content)]

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    ledger_before = manifest.read_bytes()
    content = b"new"
    original_atomic_write: Callable[[Path, bytes], None] = action_module.atomic_write

    def fail_manifest(target: Path, data: bytes) -> None:
        if target == manifest:
            raise PermissionError("injected ledger publication failure")
        original_atomic_write(target, data)

    monkeypatch.setattr(action_module, "atomic_write", fail_manifest)
    with pytest.raises(ActionPathError, match="Cannot atomically publish"):
        emit.reconcile(Database(mode=mode), root=root)

    assert (root / "owned.txt").read_bytes() == b"new"
    assert manifest.read_bytes() == ledger_before

    monkeypatch.setattr(action_module, "atomic_write", original_atomic_write)
    emit.reconcile(Database(mode=mode), root=root)
    fresh = tmp_path / "fresh"
    emit.reconcile(Database(mode=mode), root=fresh)
    assert _public_files(root) == _public_files(fresh)


@pytest.mark.parametrize("mode", _MODES)
def test_prune_failure_keeps_old_ledger_and_next_run_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    nested = True

    @action(tool=f"fault-prune-{mode}")
    def emit(_db: Database) -> list[Output]:
        if nested:
            return [Output("pkg/model.py", b"nested")]
        return [Output("pkg", b"flat")]

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    ledger_before = manifest.read_bytes()
    nested = False
    original_remove = action_module.remove_empty_directory

    def fail_prune(_path: Path) -> bool:
        raise PermissionError("injected prune failure")

    monkeypatch.setattr(action_module, "remove_empty_directory", fail_prune)
    with pytest.raises(ActionPathError, match="Cannot prune directory"):
        emit.reconcile(Database(mode=mode), root=root)

    assert manifest.read_bytes() == ledger_before
    assert not (root / "pkg" / "model.py").exists()
    assert (root / "pkg").is_dir()

    monkeypatch.setattr(action_module, "remove_empty_directory", original_remove)
    emit.reconcile(Database(mode=mode), root=root)
    fresh = tmp_path / "fresh"
    emit.reconcile(Database(mode=mode), root=fresh)
    assert _public_files(root) == _public_files(fresh)


def _make_special_target(kind: str, target: Path, tmp_path: Path) -> socket.socket | None:
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        if os.name == "nt" or not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation is unavailable on this platform")
        os.mkfifo(target)
    elif kind == "socket":
        if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
            pytest.skip("Unix-domain sockets are unavailable on this platform")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(target))
        except OSError:
            listener.close()
            pytest.skip("Unix-domain socket paths are unavailable on this filesystem")
        return listener
    elif kind == "device":
        if os.name == "nt" or not hasattr(os, "mknod") or not hasattr(os, "makedev"):
            pytest.skip("device-node creation is unavailable on this platform")
        try:
            os.mknod(target, stat.S_IFCHR | 0o600, os.makedev(1, 3))
        except OSError:
            pytest.skip("device-node creation is not permitted in the test environment")
    elif kind == "symlink":
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"outside")
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("symbolic links are unavailable on this platform")
    else:
        target.write_bytes(b"old")
    return None


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("kind", ("regular", "directory", "fifo", "socket", "device", "symlink"))
def test_action_accepts_regular_targets_and_refuses_special_targets_without_mutation(
    tmp_path: Path, mode: str, kind: str
) -> None:
    # AF_UNIX paths are often limited to 108 bytes, including pytest's base.
    socket_root = tempfile.TemporaryDirectory(prefix="pyinc-sock-") if kind == "socket" else None
    root = Path(socket_root.name) if socket_root is not None else tmp_path / "r"
    if socket_root is None:
        root.mkdir()
    target = root / "o"
    listener = _make_special_target(kind, target, tmp_path)
    before = _filesystem_snapshot(root)

    @action(tool=f"fault-special-{kind}-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output("o", b"new")]

    try:
        if kind == "regular":
            emit.reconcile(Database(mode=mode), root=root)
            assert target.read_bytes() == b"new"
        else:
            with pytest.raises(ActionPathError, match="not a regular file|symbolic link"):
                emit.reconcile(Database(mode=mode), root=root)
            assert _filesystem_snapshot(root) == before
            assert not _manifest_path(root, emit.tool).exists()
    finally:
        if listener is not None:
            listener.close()
        if socket_root is not None:
            socket_root.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-identity fault injection")
@pytest.mark.parametrize("mode", _MODES)
def test_root_replacement_after_open_is_refused_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root = tmp_path / "root"
    moved = tmp_path / "moved-root"
    root.mkdir()

    @action(tool=f"fault-root-replacement-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output("owned.txt", b"owned")]

    original_check = safe_fs_module._require_regular_or_missing
    raced = False

    def replace_root(descriptor: int, name: str, path: Path) -> None:
        nonlocal raced
        original_check(descriptor, name, path)
        if path == root / "owned.txt" and not raced:
            raced = True
            root.rename(moved)
            root.mkdir()

    monkeypatch.setattr(safe_fs_module, "_require_regular_or_missing", replace_root)
    with pytest.raises(ActionPathError, match="trusted path|changed identity"):
        emit.reconcile(Database(mode=mode), root=root)

    assert raced
    assert _filesystem_snapshot(root) == ()
    assert _filesystem_snapshot(moved) == ()
