from __future__ import annotations

import pyfoundinc
from pyfoundinc.errors import (
    CycleError,
    MutationError,
    PyFoundIncError,
    UnsupportedValueError,
    UntrackedReadError,
)


def test_error_hierarchy_inherits_from_base() -> None:
    for cls in (MutationError, UntrackedReadError, UnsupportedValueError, CycleError):
        assert issubclass(cls, PyFoundIncError)


def test_base_error_inherits_from_exception() -> None:
    assert issubclass(PyFoundIncError, Exception)


def test_error_messages_are_preserved() -> None:
    msg = "something went wrong"
    for cls in (PyFoundIncError, MutationError, UntrackedReadError, UnsupportedValueError, CycleError):
        err = cls(msg)
        assert str(err) == msg


def test_errors_are_catchable_by_base_class() -> None:
    for cls in (MutationError, UntrackedReadError, UnsupportedValueError, CycleError):
        with_caught = False
        try:
            raise cls("test")
        except PyFoundIncError:
            with_caught = True
        assert with_caught


def test_error_types_are_exported_from_package() -> None:
    for name in ("PyFoundIncError", "MutationError", "UntrackedReadError",
                 "UnsupportedValueError", "CycleError"):
        assert name in pyfoundinc.__all__
        assert hasattr(pyfoundinc, name)
