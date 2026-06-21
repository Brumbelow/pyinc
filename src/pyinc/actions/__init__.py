"""Declared-output action / reconciliation layer.

Queries compute immutable *desired artifacts*; this layer compares them with real
filesystem state and safely applies the difference — outside query evaluation.
The contract is documented in ``docs/action-contract.md``. This package is
additive and does not change kernel query-evaluation semantics.
"""

from __future__ import annotations

from .artifacts import (
    ActionIdentity,
    ActionPlan,
    ActionResult,
    DesiredArtifact,
    DesiredArtifactSet,
    ToolIdentity,
    digest_bytes,
)
from .errors import (
    ActionError,
    ActionLockError,
    ActionStateError,
    DuplicateArtifactError,
    InvalidArtifactPathError,
    SymlinkEscapeError,
)
from .manifest import MANIFEST_VERSION, ActionManifest
from .paths import normalize_relative_path, sanitize_component
from .reconciler import FilesystemReconciler, default_state_dir

__all__ = [
    "MANIFEST_VERSION",
    "ActionError",
    "ActionIdentity",
    "ActionLockError",
    "ActionManifest",
    "ActionPlan",
    "ActionResult",
    "ActionStateError",
    "DesiredArtifact",
    "DesiredArtifactSet",
    "DuplicateArtifactError",
    "FilesystemReconciler",
    "InvalidArtifactPathError",
    "SymlinkEscapeError",
    "ToolIdentity",
    "default_state_dir",
    "digest_bytes",
    "normalize_relative_path",
    "sanitize_component",
]
