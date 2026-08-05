"""No-follow filesystem primitives used by durable stores and actions."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import importlib
import os
import secrets
import stat
import struct
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, cast


class UnsafeFilesystemPathError(OSError):
    """A path component or target is unsafe for a trusted filesystem write."""


# Win32 file access, sharing, creation, attribute, and information constants.
# They live here rather than behind an ``os.name`` branch so the security-
# relevant call boundary and buffer layouts can be tested on every platform.
_WIN_GENERIC_READ = 0x80000000
_WIN_GENERIC_WRITE = 0x40000000
_WIN_DELETE = 0x00010000
_WIN_FILE_READ_ATTRIBUTES = 0x00000080
_WIN_FILE_SHARE_READ = 0x00000001
_WIN_FILE_SHARE_WRITE = 0x00000002
_WIN_FILE_SHARE_DELETE = 0x00000004
_WIN_STABLE_SHARE_MODE = _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE
_WIN_CREATE_NEW = 1
_WIN_OPEN_EXISTING = 3
_WIN_OPEN_ALWAYS = 4
_WIN_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WIN_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WIN_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_WIN_FILE_RENAME_INFO_CLASS = 3
_WIN_FILE_DISPOSITION_INFO_CLASS = 4
_WIN_ERROR_FILE_NOT_FOUND = 2
_WIN_ERROR_PATH_NOT_FOUND = 3
_WIN_ERROR_FILE_EXISTS = 80
_WIN_ERROR_ALREADY_EXISTS = 183
_WIN_ERROR_ACCESS_DENIED = 5
_WIN_ERROR_SHARING_VIOLATION = 32
_WIN_MISSING_ERRORS = frozenset({_WIN_ERROR_FILE_NOT_FOUND, _WIN_ERROR_PATH_NOT_FOUND})
_WIN_EXISTS_ERRORS = frozenset({_WIN_ERROR_FILE_EXISTS, _WIN_ERROR_ALREADY_EXISTS})
_WIN_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _WindowsFileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("reparse_tag", ctypes.c_uint32),
    ]


class _WindowsFileDispositionInfo(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_int32)]


def _windows_path_prefixes(path: str) -> tuple[str, ...]:
    """Return extended Win32 prefixes from the volume root through ``path``.

    Each prefix is opened and held without ``FILE_SHARE_DELETE`` before the
    next one is used. Rejecting relative/device/traversal spellings here keeps
    every direct Win32 call on an ordinary drive or UNC filesystem path.
    """

    normalized = path.replace("/", "\\")
    if normalized.startswith("\\\\.\\"):
        raise UnsafeFilesystemPathError(
            f"Trusted filesystem paths cannot use a Win32 device namespace: {path}"
        )
    if normalized.startswith("\\\\?\\"):
        extended_tail = normalized[4:]
        is_drive_path = (
            len(extended_tail) >= 3 and extended_tail[0].isalpha() and extended_tail[1:3] == ":\\"
        )
        is_unc_path = extended_tail.upper().startswith("UNC\\")
        if not is_drive_path and not is_unc_path:
            raise UnsafeFilesystemPathError(
                f"Trusted filesystem paths require a drive or UNC namespace: {path}"
            )
    pure = PureWindowsPath(path)
    if not pure.is_absolute() or not pure.anchor:
        raise UnsafeFilesystemPathError(f"Trusted filesystem path must be absolute: {path}")
    components = pure.parts[1:]
    if any(part in {"", ".", ".."} or ":" in part for part in components):
        raise UnsafeFilesystemPathError(f"Trusted filesystem path is not normalized: {path}")

    current = PureWindowsPath(pure.anchor)
    prefixes = [_windows_extended_path(str(current))]
    for component in components:
        current /= component
        prefixes.append(_windows_extended_path(str(current)))
    return tuple(prefixes)


def _windows_extended_path(path: str) -> str:
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return f"\\\\?\\UNC\\{path[2:]}"
    return f"\\\\?\\{path}"


def _windows_rename_information(target: str) -> tuple[bytes, int]:
    """Build a ``FILE_RENAME_INFO`` buffer and return its filename offset."""

    encoded = _windows_extended_path(target).encode("utf-16-le")
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    root_offset = (4 + pointer_size - 1) & ~(pointer_size - 1)
    length_offset = root_offset + pointer_size
    filename_offset = length_offset + 4
    payload = bytearray(filename_offset + len(encoded) + 2)
    struct.pack_into("<I", payload, 0, 1)  # ReplaceIfExists = TRUE.
    if pointer_size == 8:
        struct.pack_into("<Q", payload, root_offset, 0)
    else:
        struct.pack_into("<I", payload, root_offset, 0)
    struct.pack_into("<I", payload, length_offset, len(encoded))
    payload[filename_offset : filename_offset + len(encoded)] = encoded
    return bytes(payload), filename_offset


class _WindowsApi:
    """Small, injectable boundary around the Win32 calls used below."""

    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise RuntimeError("Win32 filesystem APIs are unavailable")
        self._kernel32: Any = loader("kernel32", use_last_error=True)
        self._create_file: Any = self._kernel32.CreateFileW
        self._create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._create_file.restype = ctypes.c_void_p
        self._create_directory: Any = self._kernel32.CreateDirectoryW
        self._create_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        self._create_directory.restype = ctypes.c_int32
        self._get_information: Any = self._kernel32.GetFileInformationByHandleEx
        self._get_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._get_information.restype = ctypes.c_int32
        self._set_information: Any = self._kernel32.SetFileInformationByHandle
        self._set_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._set_information.restype = ctypes.c_int32
        self._close_handle: Any = self._kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int32

    @staticmethod
    def _last_error() -> int:
        getter = getattr(ctypes, "get_last_error", None)
        return int(getter()) if getter is not None else 0

    @staticmethod
    def _error(action: str, path: str, code: int) -> OSError:
        error = OSError(code, f"{action} failed with Windows error {code}", path)
        with contextlib.suppress(AttributeError):
            error.winerror = code  # type: ignore[attr-defined]
        return error

    def open_handle(
        self,
        path: str,
        *,
        access: int,
        creation: int,
        flags: int,
        share: int = _WIN_STABLE_SHARE_MODE,
    ) -> int:
        handle = self._create_file(
            _windows_extended_path(path),
            access,
            share,
            None,
            creation,
            flags,
            None,
        )
        if handle in {None, _WIN_INVALID_HANDLE}:
            code = self._last_error()
            raise self._error("CreateFileW", path, code)
        return int(handle)

    def open_directory(self, path: str) -> int:
        return self.open_handle(
            path,
            access=_WIN_FILE_READ_ATTRIBUTES,
            creation=_WIN_OPEN_EXISTING,
            flags=_WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        )

    def create_directory(self, path: str) -> None:
        if self._create_directory(_windows_extended_path(path), None):
            return
        code = self._last_error()
        if code not in _WIN_EXISTS_ERRORS:
            raise self._error("CreateDirectoryW", path, code)

    def attributes(self, handle: int, path: str) -> int:
        information = _WindowsFileAttributeTagInfo()
        if not self._get_information(
            handle,
            _WIN_FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            code = self._last_error()
            raise self._error("GetFileInformationByHandleEx", path, code)
        return int(information.file_attributes)

    def require_directory(self, handle: int, path: str) -> None:
        attributes = self.attributes(handle, path)
        if attributes & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafeFilesystemPathError(f"Directory component is a reparse point: {path}")
        if not attributes & _WIN_FILE_ATTRIBUTE_DIRECTORY:
            raise UnsafeFilesystemPathError(f"Directory component is not a directory: {path}")

    def require_regular(self, handle: int, path: str) -> None:
        attributes = self.attributes(handle, path)
        if attributes & (_WIN_FILE_ATTRIBUTE_REPARSE_POINT | _WIN_FILE_ATTRIBUTE_DIRECTORY):
            raise UnsafeFilesystemPathError(f"Path is not a regular file: {path}")

    def rename_handle(self, handle: int, target: str) -> None:
        payload, _filename_offset = _windows_rename_information(target)
        buffer = ctypes.create_string_buffer(payload)
        if not self._set_information(
            handle,
            _WIN_FILE_RENAME_INFO_CLASS,
            buffer,
            len(payload),
        ):
            code = self._last_error()
            raise self._error("SetFileInformationByHandle(FileRenameInfo)", target, code)

    def delete_handle(self, handle: int, path: str) -> None:
        information = _WindowsFileDispositionInfo(1)
        if not self._set_information(
            handle,
            _WIN_FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            code = self._last_error()
            raise self._error("SetFileInformationByHandle(FileDispositionInfo)", path, code)

    def close(self, handle: int) -> None:
        if not self._close_handle(handle):
            code = self._last_error()
            raise self._error("CloseHandle", "<handle>", code)


_WINDOWS_API: _WindowsApi | None = None


def _windows_api() -> _WindowsApi:
    global _WINDOWS_API
    if _WINDOWS_API is None:
        _WINDOWS_API = _WindowsApi()
    return _WINDOWS_API


def _windows_error_code(error: OSError) -> int | None:
    winerror = getattr(error, "winerror", None)
    return int(winerror) if isinstance(winerror, int) else error.errno


class _WindowsDirectoryHandles:
    """Held directory handles that pin every component against replacement."""

    def __init__(self, api: _WindowsApi, handles: tuple[int, ...]) -> None:
        self.api = api
        self.handles = handles

    @classmethod
    def open(cls, api: _WindowsApi, path: str, *, create: bool) -> _WindowsDirectoryHandles:
        handles: list[int] = []
        try:
            for index, prefix in enumerate(_windows_path_prefixes(path)):
                try:
                    handle = api.open_directory(prefix)
                except OSError as error:
                    if create and index > 0 and _windows_error_code(error) in _WIN_MISSING_ERRORS:
                        api.create_directory(prefix)
                        handle = api.open_directory(prefix)
                    elif _windows_error_code(error) in _WIN_MISSING_ERRORS:
                        raise FileNotFoundError(
                            errno.ENOENT, "Directory does not exist", path
                        ) from error
                    else:
                        raise
                try:
                    api.require_directory(handle, prefix)
                except BaseException:
                    api.close(handle)
                    raise
                handles.append(handle)
        except BaseException:
            for handle in reversed(handles):
                with contextlib.suppress(OSError):
                    api.close(handle)
            raise
        return cls(api, tuple(handles))

    def close(self) -> None:
        first_error: OSError | None = None
        for handle in reversed(self.handles):
            try:
                self.api.close(handle)
            except OSError as error:
                if first_error is None:
                    first_error = error
        self.handles = ()
        if first_error is not None:
            raise first_error

    def __enter__(self) -> _WindowsDirectoryHandles:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()


def _windows_file_from_handle(handle: int, flags: int, mode: str) -> BinaryIO:
    msvcrt: Any = importlib.import_module("msvcrt")
    descriptor = int(msvcrt.open_osfhandle(handle, flags | getattr(os, "O_BINARY", 0)))
    return cast(BinaryIO, os.fdopen(descriptor, mode))


class _WindowsLockFile:
    """Binary lock stream that retains its path's directory handles."""

    def __init__(self, stream: BinaryIO, directories: _WindowsDirectoryHandles) -> None:
        self._stream = stream
        self._directories = directories

    def fileno(self) -> int:
        return self._stream.fileno()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def write(self, data: bytes) -> int:
        return self._stream.write(data)

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._directories.close()


def read_regular_file(path: Path) -> bytes | None:
    """Read a regular file without following symbolic links; return None if missing."""
    if os.name == "nt":
        return _read_regular_file_windows(path)
    try:
        parent_fd = _open_directory(path.parent, create=False)
    except FileNotFoundError:
        return None
    try:
        _require_directory_identity(parent_fd, path.parent)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise UnsafeFilesystemPathError(f"Cannot safely open regular file: {path}") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafeFilesystemPathError(f"Path is not a regular file: {path}")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                return handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent_fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Flush and atomically publish ``data`` relative to a no-follow parent handle."""
    if os.name == "nt":
        _atomic_write_windows(path, data)
        return
    parent_fd = _open_directory(path.parent, create=True)
    temporary: str | None = None
    try:
        _require_regular_or_missing(parent_fd, path.name, path)
        _require_directory_identity(parent_fd, path.parent)
        for _attempt in range(100):
            candidate = f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(candidate, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise OSError(f"Could not allocate a temporary file beside {path}.")

        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _require_regular_or_missing(parent_fd, path.name, path)
        _require_directory_identity(parent_fd, path.parent)
        os.replace(
            temporary,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary = None
        os.fsync(parent_fd)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_fd)
        os.close(parent_fd)


def unlink_regular_file(path: Path, *, expected_digest: str | None = None) -> bool:
    """Unlink one regular-file identity, optionally only at an expected SHA-256.

    With ``expected_digest``, POSIX first renames the current leaf into a private
    same-directory quarantine and verifies the renamed object.  The digest and
    unlink therefore apply to one directory-entry identity even if another
    process replaces the original path. POSIX callers must exclude a
    non-cooperating process that retains and writes through an already-open file
    descriptor; no portable unlink protocol can prevent that final mutation.
    Windows holds a read/delete handle that excludes replacement and writers
    while it verifies and marks that same handle for deletion.
    """
    if os.name == "nt":
        return _unlink_regular_file_windows(path, expected_digest=expected_digest)
    if expected_digest is not None:
        return _unlink_regular_file_if_digest_posix(path, expected_digest)
    try:
        parent_fd = _open_directory(path.parent, create=False)
    except FileNotFoundError:
        return False
    try:
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeFilesystemPathError(f"Refusing to delete non-regular file: {path}")
        _require_directory_identity(parent_fd, path.parent)
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _rename_noreplace(
    source: str,
    destination: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically move one entry only while the destination remains absent."""

    function_name: str | None = None
    flags = 0
    if sys.platform.startswith("linux"):
        function_name = "renameat2"
        flags = 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin":
        function_name = "renameatx_np"
        flags = 0x00000004  # RENAME_EXCL
    if function_name is not None:
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, function_name, None)
        if function is not None:
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            function.restype = ctypes.c_int
            result = function(
                src_dir_fd,
                os.fsencode(source),
                dst_dir_fd,
                os.fsencode(destination),
                flags,
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
                raise OSError(error_number, os.strerror(error_number), destination)

    # A hard link plus unlink is the portable no-clobber move for non-directory
    # entries. Directories deliberately fail closed when the platform lacks an
    # atomic exclusive rename; preserving the quarantine is safer than a racy
    # check followed by a clobbering rename.
    os.link(
        source,
        destination,
        src_dir_fd=src_dir_fd,
        dst_dir_fd=dst_dir_fd,
        follow_symlinks=False,
    )
    os.unlink(source, dir_fd=src_dir_fd)


def _unlink_regular_file_if_digest_posix(path: Path, expected_digest: str) -> bool:
    """Quarantine, verify, and delete one POSIX directory entry identity."""
    try:
        parent_fd = _open_directory(path.parent, create=False)
    except FileNotFoundError:
        return False

    quarantine_fd = -1
    quarantine_name: str | None = None
    payload_present = False
    try:
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeFilesystemPathError(f"Refusing to delete non-regular file: {path}")

        _require_directory_identity(parent_fd, path.parent)
        for _attempt in range(100):
            candidate = f".pyinc-delete-{os.getpid()}-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            quarantine_name = candidate
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                quarantine_fd = os.open(candidate, flags, dir_fd=parent_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.rmdir(candidate, dir_fd=parent_fd)
                raise
            break
        else:
            raise OSError(f"Could not allocate a deletion quarantine beside {path}.")

        _require_directory_identity(parent_fd, path.parent)
        try:
            os.rename(
                path.name,
                "payload",
                src_dir_fd=parent_fd,
                dst_dir_fd=quarantine_fd,
            )
        except FileNotFoundError:
            return False
        payload_present = True
        os.fsync(parent_fd)

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open("payload", flags, dir_fd=quarantine_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafeFilesystemPathError(
                    "Deletion target changed to a non-regular object."
                )
            current_digest = hashlib.sha256(_read_descriptor(descriptor)).hexdigest()
        finally:
            os.close(descriptor)

        if current_digest != expected_digest:
            try:
                os.link(
                    "payload",
                    path.name,
                    src_dir_fd=quarantine_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise UnsafeFilesystemPathError(
                    f"Deletion target changed while it was quarantined; both objects were "
                    f"preserved, with the verified object at "
                    f"{path.parent / quarantine_name / 'payload'}"
                ) from error
            except OSError as error:
                raise UnsafeFilesystemPathError(
                    f"Cannot restore changed deletion target; it was preserved at "
                    f"{path.parent / quarantine_name / 'payload'}"
                ) from error
            os.unlink("payload", dir_fd=quarantine_fd)
            payload_present = False
            os.rmdir(quarantine_name, dir_fd=parent_fd)
            quarantine_name = None
            os.fsync(parent_fd)
            return False

        os.unlink("payload", dir_fd=quarantine_fd)
        payload_present = False
        os.rmdir(quarantine_name, dir_fd=parent_fd)
        quarantine_name = None
        os.fsync(parent_fd)
        return True
    except BaseException:
        if payload_present and quarantine_fd >= 0 and quarantine_name is not None:
            preserved_path = path.parent / quarantine_name / "payload"
            try:
                payload_metadata = os.stat("payload", dir_fd=quarantine_fd, follow_symlinks=False)
                try:
                    target_metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    target_metadata = None
                if target_metadata is None:
                    _rename_noreplace(
                        "payload",
                        path.name,
                        src_dir_fd=quarantine_fd,
                        dst_dir_fd=parent_fd,
                    )
                    payload_present = False
                elif (target_metadata.st_dev, target_metadata.st_ino) != (
                    payload_metadata.st_dev,
                    payload_metadata.st_ino,
                ):
                    raise UnsafeFilesystemPathError(
                        "Cannot restore interrupted deletion because the live path was "
                        "replaced; both objects were preserved, with the quarantined "
                        f"object at {preserved_path}"
                    )
                if payload_present:
                    os.unlink("payload", dir_fd=quarantine_fd)
                    payload_present = False
                os.rmdir(quarantine_name, dir_fd=parent_fd)
                quarantine_name = None
                os.fsync(parent_fd)
            except UnsafeFilesystemPathError:
                raise
            except OSError as restore_error:
                raise UnsafeFilesystemPathError(
                    "Cannot restore an interrupted deletion; the object was preserved "
                    f"at {preserved_path}"
                ) from restore_error
        raise
    finally:
        if quarantine_fd >= 0:
            os.close(quarantine_fd)
        if quarantine_name is not None and not payload_present:
            with contextlib.suppress(OSError):
                os.rmdir(quarantine_name, dir_fd=parent_fd)
        os.close(parent_fd)


def remove_empty_directory(path: Path) -> bool:
    """Remove an empty directory through a no-follow parent handle and sync the parent."""
    if os.name == "nt":
        return _remove_empty_directory_windows(path)
    try:
        parent_fd = _open_directory(path.parent, create=False)
    except FileNotFoundError:
        return False
    try:
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeFilesystemPathError(f"Refusing to remove a non-directory: {path}")
        _require_directory_identity(parent_fd, path.parent)
        os.rmdir(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def open_lock_file(path: Path) -> BinaryIO:
    """Open/create a regular lock file without following a path component."""

    if os.name == "nt":
        return _open_lock_file_windows(path)
    parent_fd = _open_directory(path.parent, create=True)
    descriptor = -1
    try:
        _require_directory_identity(parent_fd, path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        except FileNotFoundError:
            # Concurrent first creation can transiently report a missing leaf.
            # Revalidate the pinned parent before one fail-closed retry.
            _require_directory_identity(parent_fd, path.parent)
            descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UnsafeFilesystemPathError(f"Lock path is not a regular file: {path}")
        _require_directory_identity(parent_fd, path.parent)
        stream = cast(BinaryIO, os.fdopen(descriptor, "r+b"))
        descriptor = -1
        return stream
    except OSError as error:
        if isinstance(error, UnsafeFilesystemPathError):
            raise
        raise UnsafeFilesystemPathError(f"Cannot safely open lock file: {path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def transient_lock_open_failure(error: OSError, path: Path) -> bool:
    """Whether a failed lock-file open may be retried within the caller's deadline.

    A concurrent lock holder (or a scanner holding the file) surfaces as a
    sharing violation, or as access-denied while the file is briefly held.
    Access-denied is retryable only while the lock path itself is still a
    regular file or missing; a directory or other special file at the path is a
    real misconfiguration and stays fatal, as does every other failure.
    """
    if isinstance(error, UnsafeFilesystemPathError):
        cause = error.__cause__
        if not isinstance(cause, OSError):
            return False
        error = cause
    winerror = getattr(error, "winerror", None)
    if winerror == _WIN_ERROR_SHARING_VIOLATION:
        return True
    if winerror == _WIN_ERROR_ACCESS_DENIED:
        return _lock_path_regular_or_missing(path)
    return False


def _lock_path_regular_or_missing(path: Path) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(mode)


def ensure_directory(path: Path) -> None:
    """Create a directory tree without following any existing path component."""

    if os.name == "nt":
        with _WindowsDirectoryHandles.open(_windows_api(), os.fspath(path), create=True):
            return
    descriptor = _open_directory(path, create=True)
    try:
        _require_directory_identity(descriptor, path)
    finally:
        os.close(descriptor)


def _read_regular_file_windows(path: Path) -> bytes | None:
    api = _windows_api()
    try:
        directories = _WindowsDirectoryHandles.open(api, os.fspath(path.parent), create=False)
    except FileNotFoundError:
        return None
    with directories:
        try:
            handle = api.open_handle(
                os.fspath(path),
                access=_WIN_GENERIC_READ | _WIN_FILE_READ_ATTRIBUTES,
                creation=_WIN_OPEN_EXISTING,
                flags=_WIN_FILE_FLAG_OPEN_REPARSE_POINT | _WIN_FILE_FLAG_BACKUP_SEMANTICS,
            )
        except OSError as error:
            if _windows_error_code(error) in _WIN_MISSING_ERRORS:
                return None
            raise
        owned_by_stream = False
        try:
            api.require_regular(handle, os.fspath(path))
            stream = _windows_file_from_handle(handle, os.O_RDONLY, "rb")
            owned_by_stream = True
            with stream:
                return stream.read()
        finally:
            if not owned_by_stream:
                api.close(handle)


def _atomic_write_windows(path: Path, data: bytes) -> None:
    api = _windows_api()
    with _WindowsDirectoryHandles.open(api, os.fspath(path.parent), create=True):
        _windows_require_regular_or_missing(api, path)
        handle: int | None = None
        stream: BinaryIO | None = None
        temporary_path: Path | None = None
        for _attempt in range(100):
            candidate = f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
            temporary_path = path.parent / candidate
            try:
                handle = api.open_handle(
                    os.fspath(temporary_path),
                    access=_WIN_GENERIC_WRITE | _WIN_DELETE | _WIN_FILE_READ_ATTRIBUTES,
                    creation=_WIN_CREATE_NEW,
                    flags=_WIN_FILE_ATTRIBUTE_NORMAL | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
                )
            except OSError as error:
                if _windows_error_code(error) in _WIN_EXISTS_ERRORS:
                    continue
                raise
            break
        if handle is None or temporary_path is None:
            raise OSError(f"Could not allocate a temporary file beside {path}.")

        try:
            api.require_regular(handle, os.fspath(temporary_path))
            stream = _windows_file_from_handle(handle, os.O_WRONLY, "wb")
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            _windows_require_regular_or_missing(api, path)
            api.rename_handle(handle, os.fspath(path))
        except BaseException:
            with contextlib.suppress(OSError):
                api.delete_handle(handle, os.fspath(temporary_path))
            raise
        finally:
            if stream is not None:
                stream.close()
            else:
                api.close(handle)


def _unlink_regular_file_windows(path: Path, *, expected_digest: str | None = None) -> bool:
    api = _windows_api()
    try:
        directories = _WindowsDirectoryHandles.open(api, os.fspath(path.parent), create=False)
    except FileNotFoundError:
        return False
    with directories:
        try:
            handle = api.open_handle(
                os.fspath(path),
                access=(
                    _WIN_DELETE
                    | _WIN_FILE_READ_ATTRIBUTES
                    | (_WIN_GENERIC_READ if expected_digest is not None else 0)
                ),
                creation=_WIN_OPEN_EXISTING,
                flags=_WIN_FILE_FLAG_OPEN_REPARSE_POINT,
                share=(
                    _WIN_FILE_SHARE_READ if expected_digest is not None else _WIN_STABLE_SHARE_MODE
                ),
            )
        except OSError as error:
            if _windows_error_code(error) in _WIN_MISSING_ERRORS:
                return False
            raise
        stream: BinaryIO | None = None
        try:
            api.require_regular(handle, os.fspath(path))
            if expected_digest is not None:
                stream = _windows_file_from_handle(handle, os.O_RDONLY, "rb")
                if hashlib.sha256(stream.read()).hexdigest() != expected_digest:
                    return False
            api.delete_handle(handle, os.fspath(path))
        finally:
            if stream is None:
                api.close(handle)
            else:
                stream.close()
    return True


def _remove_empty_directory_windows(path: Path) -> bool:
    api = _windows_api()
    try:
        directories = _WindowsDirectoryHandles.open(api, os.fspath(path.parent), create=False)
    except FileNotFoundError:
        return False
    with directories:
        try:
            handle = api.open_handle(
                os.fspath(path),
                access=_WIN_DELETE | _WIN_FILE_READ_ATTRIBUTES,
                creation=_WIN_OPEN_EXISTING,
                flags=_WIN_FILE_FLAG_OPEN_REPARSE_POINT | _WIN_FILE_FLAG_BACKUP_SEMANTICS,
            )
        except OSError as error:
            if _windows_error_code(error) in _WIN_MISSING_ERRORS:
                return False
            raise
        try:
            api.require_directory(handle, os.fspath(path))
            api.delete_handle(handle, os.fspath(path))
        finally:
            api.close(handle)
    return True


def _open_lock_file_windows(path: Path) -> BinaryIO:
    api = _windows_api()
    directories = _WindowsDirectoryHandles.open(api, os.fspath(path.parent), create=True)
    handle: int | None = None
    try:
        handle = api.open_handle(
            os.fspath(path),
            access=_WIN_GENERIC_READ | _WIN_GENERIC_WRITE | _WIN_FILE_READ_ATTRIBUTES,
            creation=_WIN_OPEN_ALWAYS,
            flags=_WIN_FILE_ATTRIBUTE_NORMAL | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        )
        api.require_regular(handle, os.fspath(path))
        stream = _windows_file_from_handle(handle, os.O_RDWR, "r+b")
        handle = None
        return cast(BinaryIO, _WindowsLockFile(stream, directories))
    except BaseException:
        if handle is not None:
            api.close(handle)
        directories.close()
        raise


def _windows_require_regular_or_missing(api: _WindowsApi, path: Path) -> None:
    try:
        handle = api.open_handle(
            os.fspath(path),
            access=_WIN_FILE_READ_ATTRIBUTES,
            creation=_WIN_OPEN_EXISTING,
            flags=_WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        )
    except OSError as error:
        if _windows_error_code(error) in _WIN_MISSING_ERRORS:
            return
        raise
    try:
        api.require_regular(handle, os.fspath(path))
    finally:
        api.close(handle)


def _open_directory(path: Path, *, create: bool) -> int:
    absolute = path.absolute()
    if not absolute.is_absolute():
        raise UnsafeFilesystemPathError(f"Trusted filesystem path must be absolute: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.path.sep, flags)
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o777, dir_fd=descriptor)
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except OSError as error:
                    raise UnsafeFilesystemPathError(
                        f"Directory component is unsafe: {absolute}"
                    ) from error
            except OSError as error:
                raise UnsafeFilesystemPathError(
                    f"Directory component is unsafe: {absolute}"
                ) from error
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_regular_or_missing(parent_fd: int, name: str, path: Path) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeFilesystemPathError(f"Path is not a regular file: {path}")


def _require_directory_identity(descriptor: int, path: Path) -> None:
    """Reject an opened POSIX directory that is no longer at ``path``.

    POSIX directory descriptors remain usable after a rename. Reopening the
    expected absolute path and comparing identities closes the deterministic
    opened-parent escape window immediately before a mutation.
    """

    try:
        comparison = _open_directory(path, create=False)
    except OSError as error:
        raise UnsafeFilesystemPathError(
            f"Opened directory is no longer reachable at its trusted path: {path}"
        ) from error
    try:
        opened_metadata = os.fstat(descriptor)
        current_metadata = os.fstat(comparison)
        if (
            not stat.S_ISDIR(opened_metadata.st_mode)
            or not stat.S_ISDIR(current_metadata.st_mode)
            or not os.path.samestat(opened_metadata, current_metadata)
        ):
            raise UnsafeFilesystemPathError(
                f"Opened directory changed identity at its trusted path: {path}"
            )
    finally:
        os.close(comparison)


__all__ = [
    "UnsafeFilesystemPathError",
    "atomic_write",
    "ensure_directory",
    "open_lock_file",
    "read_regular_file",
    "remove_empty_directory",
    "unlink_regular_file",
]
