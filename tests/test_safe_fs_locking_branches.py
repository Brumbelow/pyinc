from __future__ import annotations

import errno
import io
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyinc import _locking as locking
from pyinc import _safe_fs as safe_fs

locking_internals: Any = locking
safe_fs_internals: Any = safe_fs


class _TrackingStream(io.BytesIO):
    def __init__(self, initial: bytes = b"") -> None:
        super().__init__(initial)
        self.close_calls = 0
        self.flush_calls = 0
        self.final_bytes = b""

    def fileno(self) -> int:
        return 73

    def flush(self) -> None:
        self.flush_calls += 1
        super().flush()

    def close(self) -> None:
        self.close_calls += 1
        if not self.closed:
            self.final_bytes = self.getvalue()
        super().close()


class _DirectoryContext:
    def __init__(self) -> None:
        self.close_calls = 0
        self.enter_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def __enter__(self) -> _DirectoryContext:
        self.enter_calls += 1
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()


def _windows_error(code: int, message: str = "win32 failure") -> OSError:
    error = OSError(code, message)
    cast(Any, error).winerror = code
    return error


def _install_directory_context(
    monkeypatch: pytest.MonkeyPatch,
    context: _DirectoryContext | BaseException,
) -> None:
    def open_directories(
        _cls: type[safe_fs._WindowsDirectoryHandles],
        _api: object,
        _path: str,
        *,
        create: bool,
    ) -> _DirectoryContext:
        del create
        if isinstance(context, BaseException):
            raise context
        return context

    monkeypatch.setattr(
        safe_fs._WindowsDirectoryHandles,
        "open",
        classmethod(open_directories),
    )


@pytest.mark.parametrize("timeout", (True, "1", None, object()))
def test_lock_timeout_rejects_non_numeric_values(timeout: object) -> None:
    with pytest.raises(TypeError, match="real number"):
        locking._validate_lock_timeout(timeout)


def test_file_lock_retries_contention_then_acquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _TrackingStream()
    attempts = 0
    sleeps: list[float] = []

    def try_lock(_handle: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BlockingIOError(11, "busy")

    times = iter((10.0, 10.1, 10.2))
    monkeypatch.setattr(locking, "open_lock_file", lambda _path: stream)
    monkeypatch.setattr(locking, "_try_lock", try_lock)
    monkeypatch.setattr(locking, "_unlock", lambda _handle: None)
    monkeypatch.setattr(locking_internals.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(locking_internals.time, "sleep", sleeps.append)

    lock = locking.FileLock(tmp_path / "retry.lock", timeout=1)
    lock.acquire()

    assert attempts == 2
    assert sleeps == [0.05]
    assert lock._handle is stream
    lock.release()
    assert stream.closed


def test_file_lock_retries_simulated_darwin_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _TrackingStream()
    attempts = 0

    def try_lock(_handle: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(35, "resource temporarily unavailable")

    times = iter((10.0, 10.1, 10.2))
    monkeypatch.setattr(locking, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        locking,
        "errno",
        SimpleNamespace(EACCES=13, EAGAIN=35, EWOULDBLOCK=35),
    )
    monkeypatch.setattr(locking, "open_lock_file", lambda _path: stream)
    monkeypatch.setattr(locking, "_try_lock", try_lock)
    monkeypatch.setattr(locking, "_unlock", lambda _handle: None)
    monkeypatch.setattr(locking_internals.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(locking_internals.time, "sleep", lambda _duration: None)

    lock = locking.FileLock(tmp_path / "darwin.lock", timeout=1)
    lock.acquire()

    assert locking._is_lock_contention(OSError(35, "busy"))
    assert attempts == 2
    lock.release()
    assert stream.closed


def test_file_lock_closes_handle_for_non_contention_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _TrackingStream()
    monkeypatch.setattr(locking, "open_lock_file", lambda _path: stream)

    def fail(_handle: object) -> None:
        raise OSError(5, "not contention")

    monkeypatch.setattr(locking, "_try_lock", fail)

    with pytest.raises(OSError, match="not contention"):
        locking.FileLock(tmp_path / "failure.lock", timeout=1).acquire()
    assert stream.closed


def test_file_lock_release_is_idempotent_and_closes_after_unlock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = locking.FileLock(tmp_path / "release.lock", timeout=1)
    lock.release()

    stream = _TrackingStream()
    lock._handle = stream

    def fail_unlock(_handle: object) -> None:
        raise OSError("unlock failed")

    monkeypatch.setattr(locking, "_unlock", fail_unlock)
    with pytest.raises(OSError, match="unlock failed"):
        lock.release()

    assert lock._handle is None
    assert stream.closed


@pytest.mark.parametrize("initial", (b"", b"already initialized"))
def test_windows_file_lock_initializes_empty_lock_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: bytes,
) -> None:
    stream = _TrackingStream(initial)
    monkeypatch.setattr(locking, "os", SimpleNamespace(name="nt", SEEK_END=os.SEEK_END))
    monkeypatch.setattr(locking, "open_lock_file", lambda _path: stream)
    monkeypatch.setattr(locking, "_try_lock", lambda _handle: None)
    monkeypatch.setattr(locking, "_unlock", lambda _handle: None)

    lock = locking.FileLock(tmp_path / "windows.lock", timeout=0)
    lock.acquire()

    if initial:
        assert stream.getvalue() == initial
    else:
        assert stream.getvalue() == b"\0"
        assert stream.flush_calls == 1
    lock.release()


def test_windows_locking_calls_and_contention_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []

    class FakeMsvcrt:
        LK_NBLCK = 2
        LK_UNLCK = 3

        @staticmethod
        def locking(fd: int, mode: int, count: int) -> None:
            calls.append((fd, mode, count))
            if mode == FakeMsvcrt.LK_UNLCK:
                raise OSError("unlock races are harmless")

    stream = _TrackingStream(b"x")
    monkeypatch.setattr(locking, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(locking_internals.importlib, "import_module", lambda name: FakeMsvcrt)

    locking._try_lock(stream)
    locking._unlock(stream)

    assert calls == [(73, FakeMsvcrt.LK_NBLCK, 1), (73, FakeMsvcrt.LK_UNLCK, 1)]
    assert locking._is_lock_contention(OSError(13, "denied"))
    shared_violation = OSError(5, "sharing violation")
    cast(Any, shared_violation).winerror = 33
    assert locking._is_lock_contention(shared_violation)
    assert not locking._is_lock_contention(OSError(5, "other"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow behavior")
def test_posix_safe_fs_handles_missing_and_nonregular_targets(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "file.bin"
    assert safe_fs.read_regular_file(missing) is None
    assert not safe_fs.unlink_regular_file(missing)

    parent = tmp_path / "parent"
    parent.mkdir()
    assert safe_fs.read_regular_file(parent / "absent") is None
    assert not safe_fs.unlink_regular_file(parent / "absent")

    directory = parent / "directory"
    directory.mkdir()
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="regular file"):
        safe_fs.read_regular_file(directory)
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="non-regular"):
        safe_fs.unlink_regular_file(directory)
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="lock file"):
        safe_fs.open_lock_file(directory)


@pytest.mark.skipif(
    os.name == "nt" or not getattr(os, "O_NOFOLLOW", 0),
    reason="requires POSIX O_NOFOLLOW",
)
def test_posix_lock_file_rejects_a_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"outside")
    link = tmp_path / "link.lock"
    link.symlink_to(outside)

    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="safely open lock"):
        safe_fs.open_lock_file(link)
    assert outside.read_bytes() == b"outside"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor behavior")
def test_posix_lock_file_retries_a_transient_missing_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "retry.lock"
    real_open = os.open
    real_require_identity = safe_fs._require_directory_identity
    attempts = 0
    events: list[str] = []

    def transient_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal attempts
        if path == lock_path.name and dir_fd is not None and flags & os.O_CREAT:
            attempts += 1
            events.append("open")
            if attempts == 1:
                raise FileNotFoundError(errno.ENOENT, "transient lock create race", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def track_identity(descriptor: int, path: Path) -> None:
        if path == lock_path.parent:
            events.append("identity")
        real_require_identity(descriptor, path)

    monkeypatch.setattr(safe_fs_internals.os, "open", transient_open)
    monkeypatch.setattr(safe_fs, "_require_directory_identity", track_identity)

    handle = safe_fs.open_lock_file(lock_path)
    handle.close()

    assert attempts == 2
    assert events == ["identity", "open", "identity", "open", "identity"]
    assert lock_path.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor behavior")
def test_posix_lock_file_stops_after_a_second_missing_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "missing.lock"
    real_open = os.open
    attempts = 0

    def missing_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal attempts
        if path == lock_path.name and dir_fd is not None and flags & os.O_CREAT:
            attempts += 1
            raise FileNotFoundError(errno.ENOENT, f"missing lock leaf attempt {attempts}", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_fs_internals.os, "open", missing_open)

    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="safely open lock") as caught:
        safe_fs.open_lock_file(lock_path)

    assert attempts == 2
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert caught.value.__cause__.errno == errno.ENOENT
    assert "attempt 2" in str(caught.value.__cause__)
    assert not lock_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor behavior")
def test_posix_lock_file_revalidates_parent_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    moved = tmp_path / "moved"
    trusted.mkdir()
    lock_path = trusted / "retry.lock"
    real_open = os.open
    attempts = 0

    def replace_parent(
        path: str | bytes,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal attempts
        if path == lock_path.name and dir_fd is not None and flags & os.O_CREAT:
            attempts += 1
            if attempts == 1:
                trusted.rename(moved)
                trusted.mkdir()
                raise FileNotFoundError(errno.ENOENT, "parent moved during lock create", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_fs_internals.os, "open", replace_parent)

    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="changed identity"):
        safe_fs.open_lock_file(lock_path)

    assert attempts == 1
    assert not lock_path.exists()
    assert not (moved / lock_path.name).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_posix_directory_identity_detects_replacement(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    moved = tmp_path / "moved"
    trusted.mkdir()
    descriptor = safe_fs._open_directory(trusted, create=False)
    trusted.rename(moved)
    trusted.mkdir()
    try:
        with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="changed identity"):
            safe_fs._require_directory_identity(descriptor, trusted)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX temporary-file behavior")
def test_posix_atomic_write_reports_exhausted_temporary_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "output.bin"
    token = "0" * 16
    collision = tmp_path / f".tmp-{os.getpid()}-{token}"
    collision.write_bytes(b"occupied")
    monkeypatch.setattr(safe_fs_internals.secrets, "token_hex", lambda _length: token)

    with pytest.raises(OSError, match="allocate a temporary file"):
        safe_fs.atomic_write(target, b"new")

    assert not target.exists()
    assert collision.read_bytes() == b"occupied"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor behavior")
def test_posix_lock_file_rejects_nonregular_open_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "lock-directory"
    directory.mkdir()
    real_open = os.open

    def substitute_directory_descriptor(
        path: str | bytes,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == directory.name and dir_fd is not None and flags & os.O_RDWR:
            return real_open(directory, os.O_RDONLY)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_fs_internals.os, "open", substitute_directory_descriptor)
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="Lock path"):
        safe_fs.open_lock_file(directory)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory creation behavior")
def test_posix_directory_creation_rejects_component_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raced = tmp_path / "raced"
    real_open = os.open
    attempts = 0

    def race_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal attempts
        if path == raced.name and dir_fd is not None:
            attempts += 1
            if attempts == 1:
                raise FileNotFoundError(path)
            raise OSError("component replaced after creation")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_fs_internals.os, "open", race_open)
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="component is unsafe"):
        safe_fs._open_directory(raced, create=True)
    assert attempts == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX stat behavior")
def test_require_regular_rejects_a_directory(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    parent_fd = safe_fs._open_directory(tmp_path, create=False)
    try:
        with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="regular file"):
            safe_fs._require_regular_or_missing(parent_fd, directory.name, directory)
    finally:
        os.close(parent_fd)


def test_windows_path_helpers_cover_extended_and_32_bit_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert safe_fs._windows_extended_path(r"\\?\C:\store") == r"\\?\C:\store"
    assert safe_fs._windows_path_prefixes(r"\\?\C:\store\objects") == (
        "\\\\?\\C:\\",
        r"\\?\C:\store",
        r"\\?\C:\store\objects",
    )
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="normalized"):
        safe_fs._windows_path_prefixes(r"C:\store\bad:name")

    monkeypatch.setattr(safe_fs_internals.ctypes, "sizeof", lambda _value: 4)
    payload, filename_offset = safe_fs._windows_rename_information(r"C:\store\target")
    assert filename_offset == 12
    assert struct.unpack_from("<I", payload, 4) == (0,)
    assert payload[filename_offset:-2].decode("utf-16-le") == r"\\?\C:\store\target"


class _KernelCall:
    def __init__(self, result: int | None = 1) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> int | None:
        self.calls.append(args)
        return self.result


class _FakeKernel32:
    def __init__(self) -> None:
        self.CreateFileW = _KernelCall(41)
        self.CreateDirectoryW = _KernelCall(1)
        self.GetFileInformationByHandleEx = _KernelCall(1)
        self.SetFileInformationByHandle = _KernelCall(1)
        self.CloseHandle = _KernelCall(1)


def test_windows_api_initialization_and_lazy_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(safe_fs_internals.ctypes, "WinDLL", raising=False)
    with pytest.raises(RuntimeError, match="unavailable"):
        safe_fs._WindowsApi()

    kernel = _FakeKernel32()
    loader_calls: list[tuple[str, bool]] = []

    def load(name: str, *, use_last_error: bool) -> _FakeKernel32:
        loader_calls.append((name, use_last_error))
        return kernel

    monkeypatch.setattr(safe_fs_internals.ctypes, "WinDLL", load, raising=False)
    api = safe_fs._WindowsApi()
    assert loader_calls == [("kernel32", True)]
    assert api._create_file is kernel.CreateFileW
    assert kernel.CreateFileW.argtypes is not None
    assert kernel.CloseHandle.restype is safe_fs_internals.ctypes.c_int32

    sentinel = object()
    constructed = 0

    def construct() -> object:
        nonlocal constructed
        constructed += 1
        return sentinel

    monkeypatch.setattr(safe_fs, "_WINDOWS_API", None)
    monkeypatch.setattr(safe_fs, "_WindowsApi", construct)
    assert safe_fs._windows_api() is sentinel
    assert safe_fs._windows_api() is sentinel
    assert constructed == 1


def test_windows_api_file_and_directory_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = _FakeKernel32()
    monkeypatch.setattr(
        safe_fs_internals.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel,
        raising=False,
    )
    api = safe_fs._WindowsApi()

    assert api.open_handle("C:\\file", access=7, creation=3, flags=9) == 41
    create_call = kernel.CreateFileW.calls[-1]
    assert create_call[0] == r"\\?\C:\file"
    assert create_call[2] == safe_fs._WIN_STABLE_SHARE_MODE

    api.open_directory("C:\\directory")
    directory_call = kernel.CreateFileW.calls[-1]
    assert directory_call[1] == safe_fs._WIN_FILE_READ_ATTRIBUTES
    assert directory_call[4] == safe_fs._WIN_OPEN_EXISTING

    monkeypatch.setattr(safe_fs._WindowsApi, "_last_error", staticmethod(lambda: 5))
    kernel.CreateFileW.result = None
    with pytest.raises(OSError) as raised:
        api.open_handle("C:\\missing", access=1, creation=3, flags=0)
    assert cast(Any, raised.value).winerror == 5

    kernel.CreateDirectoryW.result = 1
    api.create_directory("C:\\new")
    kernel.CreateDirectoryW.result = 0
    monkeypatch.setattr(
        safe_fs._WindowsApi,
        "_last_error",
        staticmethod(lambda: safe_fs._WIN_ERROR_ALREADY_EXISTS),
    )
    api.create_directory("C:\\existing")
    monkeypatch.setattr(safe_fs._WindowsApi, "_last_error", staticmethod(lambda: 5))
    with pytest.raises(OSError, match="CreateDirectoryW"):
        api.create_directory("C:\\denied")


def test_windows_api_attribute_and_information_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    monkeypatch.setattr(
        safe_fs_internals.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel,
        raising=False,
    )
    api = safe_fs._WindowsApi()

    assert api.attributes(41, "C:\\file") == 0
    kernel.GetFileInformationByHandleEx.result = 0
    monkeypatch.setattr(safe_fs._WindowsApi, "_last_error", staticmethod(lambda: 87))
    with pytest.raises(OSError, match="GetFileInformationByHandleEx"):
        api.attributes(41, "C:\\file")

    monkeypatch.setattr(
        api, "attributes", lambda _handle, _path: safe_fs._WIN_FILE_ATTRIBUTE_DIRECTORY
    )
    api.require_directory(41, "C:\\directory")
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="regular file"):
        api.require_regular(41, "C:\\directory")

    monkeypatch.setattr(
        api,
        "attributes",
        lambda _handle, _path: safe_fs._WIN_FILE_ATTRIBUTE_REPARSE_POINT,
    )
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="reparse point"):
        api.require_directory(41, "C:\\junction")
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="regular file"):
        api.require_regular(41, "C:\\link")

    monkeypatch.setattr(api, "attributes", lambda _handle, _path: 0)
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="not a directory"):
        api.require_directory(41, "C:\\plain")
    api.require_regular(41, "C:\\plain")


def test_windows_api_rename_delete_and_close_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    monkeypatch.setattr(
        safe_fs_internals.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel,
        raising=False,
    )
    api = safe_fs._WindowsApi()

    api.rename_handle(41, "C:\\target")
    assert kernel.SetFileInformationByHandle.calls[-1][1] == safe_fs._WIN_FILE_RENAME_INFO_CLASS
    api.delete_handle(41, "C:\\target")
    assert (
        kernel.SetFileInformationByHandle.calls[-1][1] == safe_fs._WIN_FILE_DISPOSITION_INFO_CLASS
    )
    api.close(41)

    monkeypatch.setattr(safe_fs._WindowsApi, "_last_error", staticmethod(lambda: 5))
    kernel.SetFileInformationByHandle.result = 0
    with pytest.raises(OSError, match="FileRenameInfo"):
        api.rename_handle(41, "C:\\target")
    with pytest.raises(OSError, match="FileDispositionInfo"):
        api.delete_handle(41, "C:\\target")
    kernel.CloseHandle.result = 0
    with pytest.raises(OSError, match="CloseHandle"):
        api.close(41)


def test_windows_last_error_with_and_without_ctypes_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(safe_fs_internals.ctypes, "get_last_error", raising=False)
    assert safe_fs._WindowsApi._last_error() == 0
    monkeypatch.setattr(
        safe_fs_internals.ctypes,
        "get_last_error",
        lambda: 123,
        raising=False,
    )
    assert safe_fs._WindowsApi._last_error() == 123

    error = safe_fs._WindowsApi._error("operation", "C:\\file", 7)
    assert error.errno == 7
    assert cast(Any, error).winerror == 7
    assert safe_fs._windows_error_code(error) == 7
    assert safe_fs._windows_error_code(OSError(9, "fallback")) == 9


class _TreeWindowsApi:
    def __init__(self) -> None:
        self.next_handle = 1
        self.opened: list[str] = []
        self.created: list[str] = []
        self.closed: list[int] = []
        self.missing_once: set[str] = set()
        self.require_failure: int | None = None
        self.close_failures: set[int] = set()

    def open_directory(self, path: str) -> int:
        self.opened.append(path)
        if path in self.missing_once:
            self.missing_once.remove(path)
            raise _windows_error(safe_fs._WIN_ERROR_PATH_NOT_FOUND)
        handle = self.next_handle
        self.next_handle += 1
        return handle

    def create_directory(self, path: str) -> None:
        self.created.append(path)

    def require_directory(self, handle: int, _path: str) -> None:
        if handle == self.require_failure:
            raise safe_fs.UnsafeFilesystemPathError("unsafe directory")

    def close(self, handle: int) -> None:
        self.closed.append(handle)
        if handle in self.close_failures:
            raise OSError(handle, "close failed")


def test_windows_directory_handles_create_missing_components_and_context() -> None:
    api: Any = _TreeWindowsApi()
    missing = r"\\?\C:\store"
    api.missing_once.add(missing)

    with safe_fs._WindowsDirectoryHandles.open(api, r"C:\store\objects", create=True) as handles:
        assert handles.handles == (1, 2, 3)
        assert api.created == [missing]
        assert api.closed == []

    assert api.closed == [3, 2, 1]
    assert not handles.handles


def test_windows_directory_handles_translate_missing_and_cleanup_failures() -> None:
    missing_api: Any = _TreeWindowsApi()
    missing_api.missing_once.add(r"\\?\C:\missing")
    with pytest.raises(FileNotFoundError):
        safe_fs._WindowsDirectoryHandles.open(missing_api, r"C:\missing", create=False)
    assert missing_api.closed == [1]

    unsafe_api: Any = _TreeWindowsApi()
    unsafe_api.require_failure = 2
    with pytest.raises(safe_fs.UnsafeFilesystemPathError):
        safe_fs._WindowsDirectoryHandles.open(unsafe_api, r"C:\bad", create=False)
    assert unsafe_api.closed == [2, 1]

    closing_api: Any = _TreeWindowsApi()
    handles = safe_fs._WindowsDirectoryHandles(closing_api, (1, 2, 3))
    closing_api.close_failures = {1, 3}
    with pytest.raises(OSError) as raised:
        handles.close()
    assert raised.value.errno == 3
    assert closing_api.closed == [3, 2, 1]
    assert not handles.handles

    denied_api: Any = _TreeWindowsApi()

    def deny_open(_path: str) -> int:
        raise _windows_error(5)

    denied_api.open_directory = deny_open
    with pytest.raises(OSError) as denied:
        safe_fs._WindowsDirectoryHandles.open(denied_api, r"C:\denied", create=True)
    assert cast(Any, denied.value).winerror == 5


def test_windows_file_from_handle_uses_msvcrt_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _TrackingStream()
    calls: list[tuple[int, int]] = []

    class FakeMsvcrt:
        @staticmethod
        def open_osfhandle(handle: int, flags: int) -> int:
            calls.append((handle, flags))
            return 99

    monkeypatch.setattr(safe_fs_internals.importlib, "import_module", lambda name: FakeMsvcrt)
    monkeypatch.setattr(safe_fs_internals.os, "fdopen", lambda fd, mode: stream)

    assert safe_fs._windows_file_from_handle(41, os.O_RDONLY, "rb") is stream
    assert calls == [(41, os.O_RDONLY | getattr(os, "O_BINARY", 0))]


def test_public_safe_fs_functions_dispatch_to_windows_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target"
    stream = _TrackingStream()
    calls: list[tuple[str, object]] = []
    context = _DirectoryContext()
    fake_os = SimpleNamespace(name="nt", fspath=os.fspath)
    monkeypatch.setattr(safe_fs, "os", fake_os)

    def read_windows(candidate: Path) -> bytes:
        calls.append(("read", candidate))
        return b"data"

    def write_windows(candidate: Path, data: bytes) -> None:
        calls.append(("write", (candidate, data)))

    def unlink_windows(candidate: Path) -> bool:
        calls.append(("unlink", candidate))
        return True

    def remove_windows(candidate: Path) -> bool:
        calls.append(("remove", candidate))
        return True

    def lock_windows(candidate: Path) -> _TrackingStream:
        calls.append(("lock", candidate))
        return stream

    monkeypatch.setattr(
        safe_fs,
        "_read_regular_file_windows",
        read_windows,
    )
    monkeypatch.setattr(
        safe_fs,
        "_atomic_write_windows",
        write_windows,
    )
    monkeypatch.setattr(
        safe_fs,
        "_unlink_regular_file_windows",
        unlink_windows,
    )
    monkeypatch.setattr(
        safe_fs,
        "_remove_empty_directory_windows",
        remove_windows,
    )
    monkeypatch.setattr(
        safe_fs,
        "_open_lock_file_windows",
        lock_windows,
    )
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: object())
    _install_directory_context(monkeypatch, context)

    assert safe_fs.read_regular_file(path) == b"data"
    safe_fs.atomic_write(path, b"payload")
    assert safe_fs.unlink_regular_file(path)
    assert safe_fs.remove_empty_directory(path)
    assert safe_fs.open_lock_file(path) is stream
    safe_fs.ensure_directory(path)

    assert [call[0] for call in calls] == ["read", "write", "unlink", "remove", "lock"]
    assert context.enter_calls == 1
    assert context.close_calls == 1


class _ReadWindowsApi:
    def __init__(self, open_result: int | BaseException = 55) -> None:
        self.open_result = open_result
        self.opened: list[tuple[str, dict[str, object]]] = []
        self.closed: list[int] = []
        self.required: list[tuple[int, str]] = []

    def open_handle(self, path: str, **kwargs: object) -> int:
        self.opened.append((path, kwargs))
        if isinstance(self.open_result, BaseException):
            raise self.open_result
        return self.open_result

    def require_regular(self, handle: int, path: str) -> None:
        self.required.append((handle, path))

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_windows_read_regular_file_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing.bin"
    _install_directory_context(monkeypatch, FileNotFoundError())
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: _ReadWindowsApi())
    assert safe_fs._read_regular_file_windows(path) is None

    context = _DirectoryContext()
    _install_directory_context(monkeypatch, context)
    api = _ReadWindowsApi(_windows_error(safe_fs._WIN_ERROR_FILE_NOT_FOUND))
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: api)
    assert safe_fs._read_regular_file_windows(path) is None
    assert context.close_calls == 1

    api.open_result = _windows_error(5)
    with pytest.raises(OSError) as raised:
        safe_fs._read_regular_file_windows(path)
    assert cast(Any, raised.value).winerror == 5


def test_windows_read_regular_file_transfers_or_closes_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data.bin"
    context = _DirectoryContext()
    _install_directory_context(monkeypatch, context)
    api = _ReadWindowsApi()
    stream = _TrackingStream(b"contents")
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: api)
    monkeypatch.setattr(safe_fs, "_windows_file_from_handle", lambda *_args: stream)

    assert safe_fs._read_regular_file_windows(path) == b"contents"
    assert api.opened == [
        (
            os.fspath(path),
            {
                "access": safe_fs._WIN_GENERIC_READ | safe_fs._WIN_FILE_READ_ATTRIBUTES,
                "creation": safe_fs._WIN_OPEN_EXISTING,
                "flags": (
                    safe_fs._WIN_FILE_FLAG_OPEN_REPARSE_POINT
                    | safe_fs._WIN_FILE_FLAG_BACKUP_SEMANTICS
                ),
            },
        )
    ]
    assert api.required == [(55, os.fspath(path))]
    assert api.closed == []
    assert stream.closed

    failed_api = _ReadWindowsApi()
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: failed_api)

    def fail_conversion(*_args: object) -> _TrackingStream:
        raise OSError("descriptor conversion failed")

    monkeypatch.setattr(safe_fs, "_windows_file_from_handle", fail_conversion)
    with pytest.raises(OSError, match="conversion failed"):
        safe_fs._read_regular_file_windows(path)
    assert failed_api.closed == [55]


class _AtomicWindowsApi:
    def __init__(self) -> None:
        self.collisions = 0
        self.open_error: OSError | None = None
        self.required: list[int] = []
        self.renamed: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.rename_error: OSError | None = None
        self.require_error: OSError | None = None

    def open_handle(self, _path: str, **_kwargs: object) -> int:
        if self.collisions:
            self.collisions -= 1
            raise _windows_error(safe_fs._WIN_ERROR_FILE_EXISTS)
        if self.open_error is not None:
            raise self.open_error
        return 61

    def require_regular(self, handle: int, _path: str) -> None:
        self.required.append(handle)
        if self.require_error is not None:
            raise self.require_error

    def rename_handle(self, handle: int, target: str) -> None:
        self.renamed.append((handle, target))
        if self.rename_error is not None:
            raise self.rename_error

    def delete_handle(self, handle: int, path: str) -> None:
        self.deleted.append((handle, path))

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def _prepare_windows_atomic_test(
    monkeypatch: pytest.MonkeyPatch,
    api: _AtomicWindowsApi,
    stream: _TrackingStream,
) -> None:
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: api)
    _install_directory_context(monkeypatch, _DirectoryContext())
    monkeypatch.setattr(safe_fs, "_windows_require_regular_or_missing", lambda *_args: None)
    monkeypatch.setattr(safe_fs, "_windows_file_from_handle", lambda *_args: stream)
    monkeypatch.setattr(safe_fs_internals.os, "fsync", lambda _fd: None)


def test_windows_atomic_write_retries_collision_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _AtomicWindowsApi()
    api.collisions = 1
    stream = _TrackingStream()
    _prepare_windows_atomic_test(monkeypatch, api, stream)
    target = tmp_path / "target.bin"

    safe_fs._atomic_write_windows(target, b"payload")

    assert stream.final_bytes == b"payload"
    assert stream.flush_calls == 1
    assert stream.closed
    assert api.required == [61]
    assert api.renamed == [(61, os.fspath(target))]
    assert api.deleted == []
    assert api.closed == []


def test_windows_atomic_write_exhausts_collisions_or_propagates_open_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _AtomicWindowsApi()
    api.collisions = 100
    _prepare_windows_atomic_test(monkeypatch, api, _TrackingStream())
    with pytest.raises(OSError, match="allocate a temporary file"):
        safe_fs._atomic_write_windows(tmp_path / "target.bin", b"payload")

    api.collisions = 0
    api.open_error = _windows_error(5)
    with pytest.raises(OSError) as raised:
        safe_fs._atomic_write_windows(tmp_path / "target.bin", b"payload")
    assert cast(Any, raised.value).winerror == 5


@pytest.mark.parametrize("failure_stage", ("require", "rename"))
def test_windows_atomic_write_deletes_temporary_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    api = _AtomicWindowsApi()
    stream = _TrackingStream()
    if failure_stage == "require":
        api.require_error = OSError("not regular")
    else:
        api.rename_error = OSError("rename failed")
    _prepare_windows_atomic_test(monkeypatch, api, stream)

    with pytest.raises(OSError):
        safe_fs._atomic_write_windows(tmp_path / "target.bin", b"payload")

    assert len(api.deleted) == 1
    if failure_stage == "require":
        assert api.closed == [61]
        assert stream.close_calls == 0
    else:
        assert api.closed == []
        assert stream.closed


class _UnlinkWindowsApi:
    def __init__(self, open_result: int | BaseException = 71) -> None:
        self.open_result = open_result
        self.required: list[int] = []
        self.deleted: list[int] = []
        self.closed: list[int] = []

    def open_handle(self, _path: str, **_kwargs: object) -> int:
        if isinstance(self.open_result, BaseException):
            raise self.open_result
        return self.open_result

    def require_regular(self, handle: int, _path: str) -> None:
        self.required.append(handle)

    def delete_handle(self, handle: int, _path: str) -> None:
        self.deleted.append(handle)

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_windows_unlink_handles_missing_and_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target.bin"
    _install_directory_context(monkeypatch, FileNotFoundError())
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: _UnlinkWindowsApi())
    assert not safe_fs._unlink_regular_file_windows(path)

    context = _DirectoryContext()
    _install_directory_context(monkeypatch, context)
    missing_api = _UnlinkWindowsApi(_windows_error(safe_fs._WIN_ERROR_PATH_NOT_FOUND))
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: missing_api)
    assert not safe_fs._unlink_regular_file_windows(path)

    regular_api = _UnlinkWindowsApi()
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: regular_api)
    assert safe_fs._unlink_regular_file_windows(path)
    assert regular_api.required == [71]
    assert regular_api.deleted == [71]
    assert regular_api.closed == [71]

    failing_api = _UnlinkWindowsApi(_windows_error(5))
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: failing_api)
    with pytest.raises(OSError) as raised:
        safe_fs._unlink_regular_file_windows(path)
    assert cast(Any, raised.value).winerror == 5


class _RemoveDirectoryWindowsApi:
    def __init__(self, open_result: int | BaseException = 77) -> None:
        self.open_result = open_result
        self.required: list[int] = []
        self.deleted: list[int] = []
        self.closed: list[int] = []

    def open_handle(self, _path: str, **_kwargs: object) -> int:
        if isinstance(self.open_result, BaseException):
            raise self.open_result
        return self.open_result

    def require_directory(self, handle: int, _path: str) -> None:
        self.required.append(handle)

    def delete_handle(self, handle: int, _path: str) -> None:
        self.deleted.append(handle)

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_windows_remove_empty_directory_handles_missing_and_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "emptied"
    _install_directory_context(monkeypatch, FileNotFoundError())
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: _RemoveDirectoryWindowsApi())
    assert not safe_fs._remove_empty_directory_windows(path)

    context = _DirectoryContext()
    _install_directory_context(monkeypatch, context)
    missing_api = _RemoveDirectoryWindowsApi(_windows_error(safe_fs._WIN_ERROR_PATH_NOT_FOUND))
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: missing_api)
    assert not safe_fs._remove_empty_directory_windows(path)
    assert context.close_calls == 1

    directory_api = _RemoveDirectoryWindowsApi()
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: directory_api)
    assert safe_fs._remove_empty_directory_windows(path)
    assert directory_api.required == [77]
    assert directory_api.deleted == [77]
    assert directory_api.closed == [77]

    failing_api = _RemoveDirectoryWindowsApi(_windows_error(5))
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: failing_api)
    with pytest.raises(OSError) as raised:
        safe_fs._remove_empty_directory_windows(path)
    assert cast(Any, raised.value).winerror == 5


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_remove_empty_directory_branches_on_missing_nondirectory_and_populated(
    tmp_path: Path,
) -> None:
    assert not safe_fs.remove_empty_directory(tmp_path / "absent-parent" / "child")
    assert not safe_fs.remove_empty_directory(tmp_path / "missing")

    regular = tmp_path / "regular"
    regular.write_bytes(b"data")
    with pytest.raises(safe_fs.UnsafeFilesystemPathError, match="non-directory"):
        safe_fs.remove_empty_directory(regular)
    assert regular.read_bytes() == b"data"

    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "entry.txt").write_bytes(b"entry")
    with pytest.raises(OSError):
        safe_fs.remove_empty_directory(populated)
    assert (populated / "entry.txt").read_bytes() == b"entry"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert safe_fs.remove_empty_directory(empty)
    assert not empty.exists()


def test_windows_require_regular_or_missing_closes_observation_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target.bin"
    missing_api = _UnlinkWindowsApi(_windows_error(safe_fs._WIN_ERROR_FILE_NOT_FOUND))
    safe_fs._windows_require_regular_or_missing(missing_api, path)  # type: ignore[arg-type]

    failing_api = _UnlinkWindowsApi(_windows_error(5))
    with pytest.raises(OSError) as raised:
        safe_fs._windows_require_regular_or_missing(failing_api, path)  # type: ignore[arg-type]
    assert cast(Any, raised.value).winerror == 5

    regular_api = _UnlinkWindowsApi()
    safe_fs._windows_require_regular_or_missing(regular_api, path)  # type: ignore[arg-type]
    assert regular_api.required == [71]
    assert regular_api.closed == [71]

    def fail_requirement(_handle: int, _path: str) -> None:
        raise safe_fs.UnsafeFilesystemPathError("unsafe")

    monkeypatch.setattr(regular_api, "require_regular", fail_requirement)
    with pytest.raises(safe_fs.UnsafeFilesystemPathError):
        safe_fs._windows_require_regular_or_missing(regular_api, path)  # type: ignore[arg-type]
    assert regular_api.closed == [71, 71]


class _LockWindowsApi(_UnlinkWindowsApi):
    pass


def test_windows_lock_file_retains_directories_until_stream_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    context = _DirectoryContext()
    api = _LockWindowsApi(81)
    stream = _TrackingStream(b"x")
    _install_directory_context(monkeypatch, context)
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: api)
    monkeypatch.setattr(safe_fs, "_windows_file_from_handle", lambda *_args: stream)

    lock_stream = safe_fs._open_lock_file_windows(path)
    assert lock_stream.fileno() == 73
    assert lock_stream.seek(0, os.SEEK_END) == 1
    assert lock_stream.tell() == 1
    written = lock_stream.write(b"y")
    assert written == 1
    lock_stream.flush()
    assert context.close_calls == 0
    lock_stream.close()

    assert stream.closed
    assert context.close_calls == 1
    assert api.required == [81]
    assert api.closed == []


@pytest.mark.parametrize("failure_stage", ("open", "require", "convert"))
def test_windows_lock_file_closes_partial_resources_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    path = tmp_path / "lock"
    context = _DirectoryContext()
    api = _LockWindowsApi(_windows_error(5) if failure_stage == "open" else 81)
    _install_directory_context(monkeypatch, context)
    monkeypatch.setattr(safe_fs, "_windows_api", lambda: api)
    if failure_stage == "require":
        monkeypatch.setattr(
            api,
            "require_regular",
            lambda *_args: (_ for _ in ()).throw(safe_fs.UnsafeFilesystemPathError("unsafe")),
        )
    elif failure_stage == "convert":
        monkeypatch.setattr(
            safe_fs,
            "_windows_file_from_handle",
            lambda *_args: (_ for _ in ()).throw(OSError("conversion failed")),
        )

    with pytest.raises(OSError):
        safe_fs._open_lock_file_windows(path)

    assert context.close_calls == 1
    assert api.closed == ([] if failure_stage == "open" else [81])


def test_sharing_violation_classifies_as_transient_lock_open_failure(
    tmp_path: Path,
) -> None:
    error = _windows_error(32, "sharing violation")
    assert safe_fs.transient_lock_open_failure(error, tmp_path / "store.lock")


def test_access_denied_is_transient_only_while_the_lock_path_stays_regular_or_missing(
    tmp_path: Path,
) -> None:
    denied = _windows_error(5, "access denied")
    missing = tmp_path / "store.lock"
    assert safe_fs.transient_lock_open_failure(denied, missing)

    regular = tmp_path / "present.lock"
    regular.write_bytes(b"\0")
    assert safe_fs.transient_lock_open_failure(denied, regular)

    directory = tmp_path / "dir.lock"
    directory.mkdir()
    assert not safe_fs.transient_lock_open_failure(denied, directory)


def test_wrapped_posix_open_failures_classify_through_their_cause(tmp_path: Path) -> None:
    cause = _windows_error(32)
    wrapped = safe_fs.UnsafeFilesystemPathError("Cannot safely open lock file")
    wrapped.__cause__ = cause
    assert safe_fs.transient_lock_open_failure(wrapped, tmp_path / "store.lock")

    unwrapped_cause = safe_fs.UnsafeFilesystemPathError("no cause attached")
    assert not safe_fs.transient_lock_open_failure(
        unwrapped_cause, tmp_path / "store.lock"
    )


def test_ordinary_open_failures_stay_fatal(tmp_path: Path) -> None:
    assert not safe_fs.transient_lock_open_failure(
        OSError(13, "plain errno"), tmp_path / "x"
    )
    assert not safe_fs.transient_lock_open_failure(
        _windows_error(2, "file not found"), tmp_path / "x"
    )


def test_acquire_retries_transient_open_failures_until_the_open_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"count": 0}
    stream = _TrackingStream()

    def flaky_open(path: Path) -> _TrackingStream:
        calls["count"] += 1
        if calls["count"] < 3:
            raise _windows_error(32, "sharing violation")
        return stream

    monkeypatch.setattr(locking, "open_lock_file", flaky_open)
    monkeypatch.setattr(locking, "_try_lock", lambda handle: None)
    monkeypatch.setattr(locking, "_unlock", lambda handle: None)
    lock = locking.FileLock(tmp_path / "store.lock", timeout=5.0)
    lock.acquire()
    try:
        assert calls["count"] == 3
        assert lock._handle is stream
    finally:
        lock.release()


def test_acquire_raises_timeout_when_transient_open_failures_outlast_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def always_contended(path: Path) -> _TrackingStream:
        raise _windows_error(32, "sharing violation")

    monkeypatch.setattr(locking, "open_lock_file", always_contended)
    lock = locking.FileLock(tmp_path / "store.lock", timeout=0)
    with pytest.raises(TimeoutError, match="waiting for lock"):
        lock.acquire()


def test_acquire_propagates_nontransient_open_failures_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"count": 0}

    def broken_open(path: Path) -> _TrackingStream:
        calls["count"] += 1
        raise _windows_error(2, "file not found")

    monkeypatch.setattr(locking, "open_lock_file", broken_open)
    lock = locking.FileLock(tmp_path / "store.lock", timeout=5.0)
    with pytest.raises(OSError):
        lock.acquire()
    assert calls["count"] == 1
