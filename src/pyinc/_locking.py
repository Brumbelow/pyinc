"""Small cross-platform advisory file-lock helper."""

from __future__ import annotations

import contextlib
import errno
import importlib
import math
import os
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, cast

from ._safe_fs import open_lock_file


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, count: int) -> None: ...


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None: ...


class FileLock:
    """An exclusive, process-wide advisory lock backed by a lock file."""

    def __init__(self, path: Path, *, timeout: float) -> None:
        timeout = _validate_lock_timeout(timeout)
        self.path = path
        self.timeout = timeout
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        handle = open_lock_file(self.path)
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _try_lock(handle)
            except OSError as error:
                if not _is_lock_contention(error):
                    handle.close()
                    raise
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(
                        f"Timed out after {self.timeout:g}s waiting for lock {self.path}."
                    ) from error
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            else:
                self._handle = handle
                return

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _validate_lock_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("Lock timeout must be a real number.")
    normalized = float(timeout)
    if normalized < 0 or not math.isfinite(normalized):
        raise ValueError("Lock timeout must be finite and non-negative.")
    return normalized


def _try_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl = cast(_FcntlModule, importlib.import_module("fcntl"))
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
        handle.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl = cast(_FcntlModule, importlib.import_module("fcntl"))
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_lock_contention(error: OSError) -> bool:
    if isinstance(error, BlockingIOError):
        return True
    if os.name == "nt":
        return error.errno in {13, 36} or getattr(error, "winerror", None) in {33, 36}
    return error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}
