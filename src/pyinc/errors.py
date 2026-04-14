from __future__ import annotations


class PyIncError(Exception):
    """Base error for pyinc."""


class MutationError(PyIncError):
    """Raised when a query mutates one of its boundary inputs."""


class UntrackedReadError(PyIncError):
    """Raised when code performs an undeclared external read."""


class UnsupportedValueError(PyIncError):
    """Raised when a value cannot cross a cached boundary safely."""


class CycleError(PyIncError):
    """Raised when query evaluation encounters a dependency cycle."""
