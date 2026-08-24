from __future__ import annotations

import errno
import os

import pyinc
from pyinc.errors import (
    CycleError,
    MutationError,
    PyIncError,
    UnsupportedValueError,
    UntrackedReadError,
)


def test_error_hierarchy_inherits_from_base() -> None:
    for cls in (MutationError, UntrackedReadError, UnsupportedValueError, CycleError):
        assert issubclass(cls, PyIncError)


def test_an_unsafe_filesystem_path_is_both_a_pyinc_error_and_an_os_error() -> None:
    from pyinc._safe_fs import UnsafeFilesystemPathError

    assert issubclass(UnsafeFilesystemPathError, PyIncError)
    assert issubclass(UnsafeFilesystemPathError, OSError)
    # The OSError constructor still applies, so a refusal raised from a
    # failed system call keeps the code and the path it names.
    error = UnsafeFilesystemPathError(errno.EACCES, os.strerror(errno.EACCES), "/x")
    assert error.errno == errno.EACCES
    assert error.filename == "/x"
    # And a plain message still constructs, which is how the library
    # raises it today.
    assert str(UnsafeFilesystemPathError("Path is not a regular file: /x")).endswith("/x")


def test_base_error_inherits_from_exception() -> None:
    assert issubclass(PyIncError, Exception)


def test_error_messages_are_preserved() -> None:
    msg = "something went wrong"
    for cls in (
        PyIncError,
        MutationError,
        UntrackedReadError,
        UnsupportedValueError,
        CycleError,
    ):
        err = cls(msg)
        assert str(err) == msg


def test_errors_are_catchable_by_base_class() -> None:
    for cls in (MutationError, UntrackedReadError, UnsupportedValueError, CycleError):
        with_caught = False
        try:
            raise cls("test")
        except PyIncError:
            with_caught = True
        assert with_caught


def test_error_types_are_exported_from_package() -> None:
    for name in (
        "PyIncError",
        "MutationError",
        "UntrackedReadError",
        "UnsupportedValueError",
        "CycleError",
    ):
        assert name in pyinc.__all__
        assert hasattr(pyinc, name)
