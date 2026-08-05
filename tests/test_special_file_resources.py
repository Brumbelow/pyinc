from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import pytest

import pyinc.resources as resources_module
from pyinc import (
    BinaryFileResource,
    Database,
    FileResource,
    InMemoryArtifactStore,
    query,
)
from pyinc.integrations.python_source import (
    directory_analysis,
    source_text,
    workspace_python_files,
)

_MODES = ("strict", "checked", "fast")
_TEXT_FILES = FileResource()
_BINARY_FILES = BinaryFileResource()


@query(key="tests.special-files.text-v1")
def _public_text(db: Database, path: str) -> str:
    return _TEXT_FILES.read(db, path)


@query(key="tests.special-files.binary-v1")
def _public_bytes(db: Database, path: str) -> bytes:
    return _BINARY_FILES.read(db, path)


def _make_fifo(path: Path) -> None:
    make_fifo = getattr(os, "mkfifo", None)
    if make_fifo is None:
        pytest.skip("FIFO creation is unavailable")
    make_fifo(path)


def _symlink_to(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target.name)
    except (NotImplementedError, OSError):
        pytest.skip("file symlinks are unavailable")


def _retarget(link: Path, target: Path) -> None:
    link.unlink()
    _symlink_to(link, target)


def _assert_public_file_refusal(db: Database, path: Path) -> None:
    assert _TEXT_FILES.probe(path) == ("missing",)
    assert _BINARY_FILES.probe(path) == ("missing",)
    with pytest.raises(FileNotFoundError):
        db.get(_public_text, str(path))
    with pytest.raises(FileNotFoundError):
        db.get(_public_bytes, str(path))


@pytest.mark.parametrize("mode", _MODES)
def test_regular_python_file_is_discovered_and_read_in_every_mode(
    mode: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    db = Database(mode=mode)

    assert db.get(workspace_python_files, str(tmp_path)) == (str(source),)
    assert directory_analysis(db, str(tmp_path))[0].path == str(source)
    assert db.get(source_text, str(source)) == "value = 1\n"
    assert db.get(_public_text, str(source)) == "value = 1\n"
    assert db.get(_public_bytes, str(source)) == b"value = 1\n"


@pytest.mark.parametrize("mode", _MODES)
def test_fifo_python_file_is_refused_without_entering_a_blocking_read(
    mode: str,
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "pipe.py"
    _make_fifo(fifo)
    db = Database(mode=mode)

    _assert_public_file_refusal(db, fifo)
    assert db.get(source_text, str(fifo)) == ""
    assert db.get(workspace_python_files, str(tmp_path)) == ()
    assert directory_analysis(db, str(tmp_path)) == ()


@pytest.mark.parametrize("mode", _MODES)
def test_unix_socket_python_file_is_refused_without_a_read(
    mode: str,
    tmp_path: Path,
) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix-domain sockets are unavailable")
    socket_path = tmp_path / "service.py"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            listener.bind(str(socket_path))
        except OSError as exc:
            pytest.skip(f"Unix-domain socket bind is unavailable: {exc}")
        db = Database(mode=mode)

        _assert_public_file_refusal(db, socket_path)
        assert db.get(source_text, str(socket_path)) == ""
        assert db.get(workspace_python_files, str(tmp_path)) == ()
        assert directory_analysis(db, str(tmp_path)) == ()
    finally:
        listener.close()


@pytest.mark.parametrize("mode", _MODES)
def test_character_device_is_refused_in_every_mode(mode: str) -> None:
    device = Path("/dev/null")
    if os.name != "posix" or not device.exists():
        pytest.skip("a safe character device is unavailable")
    db = Database(mode=mode)

    _assert_public_file_refusal(db, device)
    assert db.get(source_text, str(device)) == ""


def test_regular_file_open_is_nonblocking_and_validated_by_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not getattr(os, "O_NONBLOCK", 0):
        pytest.skip("O_NONBLOCK is unavailable")
    source = tmp_path / "module.py"
    source.write_bytes(b"value = 1\n")
    events: list[tuple[str, int]] = []
    original_open = os.open
    original_fstat = os.fstat
    original_stat = os.stat

    def tracked_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert flags & os.O_NONBLOCK
        descriptor = original_open(path, flags, *args, **kwargs)
        events.append(("open", descriptor))
        return descriptor

    def tracked_fstat(descriptor: int) -> os.stat_result:
        events.append(("fstat", descriptor))
        return original_fstat(descriptor)

    def tracked_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        events.append(("stat", -1))
        result = original_stat(path, *args, **kwargs)
        assert isinstance(result, os.stat_result)
        return result

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fstat", tracked_fstat)
    monkeypatch.setattr(os, "stat", tracked_stat)

    assert resources_module._read_file(str(source)) == b"value = 1\n"
    assert [event[0] for event in events] == ["open", "fstat"]
    assert events[0][1] == events[1][1]


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_and_warm_discovery_follow_regular_to_fifo_symlink_retarget(
    mode: str,
    tmp_path: Path,
) -> None:
    regular = tmp_path / "regular-target"
    regular.write_text("value = 1\n", encoding="utf-8")
    fifo = tmp_path / "fifo-target"
    _make_fifo(fifo)
    link = tmp_path / "module.py"
    _symlink_to(link, regular)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    expected_files = (str(link),)
    assert writer.get(workspace_python_files, str(tmp_path)) == expected_files
    assert writer.get(source_text, str(link)) == "value = 1\n"
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(source_text, str(link)) == "value = 1\n"
    assert reader.statistics().query_executions == 0
    assert reader.inspect(source_text, str(link)).last_decision == "reused"
    assert reader.get(workspace_python_files, str(tmp_path)) == expected_files

    _retarget(link, fifo)
    warm_files = reader.get(workspace_python_files, str(tmp_path))
    warm_source = reader.get(source_text, str(link))
    fresh_db = Database(mode=mode)
    assert warm_files == fresh_db.get(workspace_python_files, str(tmp_path)) == ()
    assert warm_source == fresh_db.get(source_text, str(link)) == ""
    assert reader.inspect(workspace_python_files, str(tmp_path)).last_recompute == "executed"
    assert reader.inspect(source_text, str(link)).last_recompute == "executed"

    _retarget(link, regular)
    restored_db = Database(mode=mode)
    assert reader.get(workspace_python_files, str(tmp_path)) == restored_db.get(
        workspace_python_files, str(tmp_path)
    )
    assert reader.get(source_text, str(link)) == restored_db.get(source_text, str(link))
