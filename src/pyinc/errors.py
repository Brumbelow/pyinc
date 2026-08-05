from __future__ import annotations


class PyIncError(Exception):
    """Base error for pyinc."""


class MutationError(PyIncError):
    """Raised when a query mutates one of its boundary inputs."""


class UntrackedReadError(PyIncError):
    """Raised when code performs an undeclared external read."""


class ResourceDependencyError(PyIncError, RuntimeError):
    """Raised when a Resource hook reads database-managed state."""


class QueryContextError(PyIncError, RuntimeError):
    """Raised for query-time administration, cross-database reads, or Layer-3 calls."""


class QueryConcurrencyError(PyIncError, RuntimeError):
    """Raised when query or Resource execution attempts concurrent launch."""


class UnsupportedValueError(PyIncError):
    """Raised when a value cannot cross a cached boundary safely."""


class CycleError(PyIncError):
    """Raised when query evaluation encounters a dependency cycle."""


class InputKeyError(PyIncError, ValueError):
    """Raised when an input key is invalid or conflicts within a database."""


class CheckpointError(PyIncError):
    """Base error for durable-checkpoint failures."""


class CheckpointVersionError(CheckpointError, ValueError):
    """Raised when a checkpoint uses an unsupported manifest or kernel version."""


class CheckpointModeError(CheckpointError, ValueError):
    """Raised when a checkpoint was saved under another execution mode."""


class CheckpointManifestError(CheckpointError, ValueError):
    """Raised when a checkpoint manifest is malformed or internally inconsistent."""


class CheckpointIntegrityError(CheckpointManifestError):
    """Raised when checkpoint bytes do not match their content address."""


class ActionError(PyIncError):
    """Base error for output reconciliation failures."""


class ActionPathError(ActionError, ValueError):
    """Raised when an action output path is unsafe or ambiguous."""


class ActionManifestError(ActionError, ValueError):
    """Raised when an action ownership manifest is malformed or untrusted."""


class ActionLockTimeoutError(ActionError, TimeoutError):
    """Raised when an action cannot acquire its filesystem lock in time."""


class ArtifactStoreError(PyIncError):
    """Base error for artifact-store failures."""


class ArtifactStoreKeyError(ArtifactStoreError, ValueError):
    """Raised when an artifact key is malformed or unsafe."""


class ArtifactStoreLockError(ArtifactStoreError, TimeoutError):
    """Raised when an artifact-store lock cannot be acquired."""
