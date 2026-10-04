from __future__ import annotations


class PyIncError(Exception):
    """Base error for pyinc."""


class MutationError(PyIncError):
    """Raised when a query mutates one of its boundary inputs."""


class UntrackedReadError(PyIncError):
    """Raised when code performs an undeclared external read."""


class UnsupportedValueError(PyIncError):
    """Raised when a value is unsafe to cross a cached boundary."""


class AdapterContractError(PyIncError):
    """Raised when a registered adapter's configuration changes after construction."""


class CycleError(PyIncError):
    """Raised when query evaluation encounters a dependency cycle."""


class ReentrantDatabaseError(PyIncError):
    """Raised when a call re-enters the database from inside its own execution."""


class CompositionError(PyIncError):
    """Raised when a high-level entrypoint is called from inside a query body."""


class InputKeyError(PyIncError, ValueError):
    """Raised when an input key is invalid or conflicts within a database."""


class CheckpointError(PyIncError):
    """Base error for durable-checkpoint failures."""


class CheckpointVersionError(CheckpointError, ValueError):
    """Raised when a checkpoint uses an unsupported manifest or kernel version."""


class CheckpointManifestError(CheckpointError, ValueError):
    """Raised when a checkpoint manifest is malformed or internally inconsistent."""


class CheckpointIntegrityError(CheckpointManifestError):
    """Raised when checkpoint bytes fail to match their content address."""


class CheckpointModeError(CheckpointError, ValueError):
    """Raised when a checkpoint saved in one database mode is loaded into another."""


class ActionError(PyIncError):
    """Base error for output reconciliation failures."""


class ActionPathError(ActionError, ValueError):
    """Raised when an action output path is unsafe or ambiguous."""


class ActionManifestError(ActionError, ValueError):
    """Raised when an action ownership manifest is malformed or untrusted."""


class ActionLockTimeoutError(ActionError, TimeoutError):
    """Raised when an action times out acquiring its filesystem lock."""


class ArtifactStoreError(PyIncError):
    """Base error for artifact-store failures."""


class ArtifactStoreKeyError(ArtifactStoreError, ValueError):
    """Raised when an artifact key is malformed or unsafe."""


class ArtifactStoreLockError(ArtifactStoreError, TimeoutError):
    """Raised when acquiring an artifact-store lock times out."""
