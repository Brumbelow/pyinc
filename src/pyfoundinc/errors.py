from __future__ import annotations


class PyFoundIncError(Exception):
    """Base error for pyfoundinc."""


class MutationError(PyFoundIncError):
    """Raised when a query mutates one of its boundary inputs."""


class UntrackedReadError(PyFoundIncError):
    """Raised when code performs an undeclared external read."""


class UnsupportedValueError(PyFoundIncError):
    """Raised when a value cannot cross a cached boundary safely."""


class CycleError(PyFoundIncError):
    """Raised when query evaluation encounters a dependency cycle."""
