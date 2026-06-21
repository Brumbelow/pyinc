"""Errors raised by the action / reconciliation layer.

All subclass :class:`pyinc.PyIncError` so existing ``except PyIncError`` handlers
keep working. The action layer never raises bare exceptions for contract
violations — every rejection has a typed, documented error.
"""

from __future__ import annotations

from pyinc.errors import PyIncError


class ActionError(PyIncError):
    """Base error for the action / reconciliation layer."""


class InvalidArtifactPathError(ActionError):
    """Raised for an unsafe declared output path.

    Covers absolute paths, ``..`` traversal, empty/whitespace paths, and Windows
    drive/UNC prefixes. The path is rejected *before* any filesystem access.
    """


class DuplicateArtifactError(ActionError):
    """Raised when two desired artifacts normalize to the same output path."""


class SymlinkEscapeError(ActionError):
    """Raised when a declared output would be written through a symlink that
    escapes the output root, or a deletion target resolves outside the root."""


class ActionLockError(ActionError):
    """Raised when another writer already holds the action's exclusive lock."""


class ActionStateError(ActionError):
    """Raised for inconsistent or misused action state.

    Examples: invoking :meth:`FilesystemReconciler.apply` from inside an active
    query, or pointing a reconciler at a state directory that records a
    different action identity / output root.
    """
