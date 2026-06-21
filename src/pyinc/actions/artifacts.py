"""Immutable desired-artifact model.

Queries compute *desired* artifacts: snapshot-safe, frozen descriptions of the
bytes that should exist at a relative output path. They never touch the
filesystem. The :mod:`pyinc.actions.reconciler` consumes a
:class:`DesiredArtifactSet` and reconciles it to disk *outside* query evaluation.

Every type here is a ``@dataclass(frozen=True)`` with snapshot-safe fields
(scalars, ``bytes``, tuples of scalars), so an integration may freely return them
from a ``@query`` and they cross cached boundaries unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .errors import DuplicateArtifactError
from .paths import normalize_relative_path


def digest_bytes(content: bytes) -> str:
    """Return the deterministic SHA-256 hex digest of ``content``.

    Used for every content comparison in the reconciler. Hashing the raw output
    bytes (not an mtime) is what lets the layer detect externally-tampered owned
    outputs and repair them even when query inputs are unchanged.
    """
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ToolIdentity:
    """Explicit, snapshot-safe identity of the tool that produced an artifact set.

    ``schema_version`` covers the action implementation / output schema so a
    generator change that alters output semantics changes identity even when the
    inputs are byte-identical. ``executable_digest`` / ``config_digest`` are
    optional, caller-supplied digests — the action layer never discovers tool
    versions by running hidden subprocesses.
    """

    name: str
    version: str
    schema_version: int = 1
    executable_digest: str | None = None
    config_digest: str | None = None


@dataclass(frozen=True)
class ActionIdentity:
    """Identity of one action: a stable id, its output root, and its tool."""

    action_id: str
    output_root: str
    tool: ToolIdentity


@dataclass(frozen=True)
class DesiredArtifact:
    """One immutable desired output: a relative path, its bytes, a content digest,
    and optional deterministic metadata.

    The path is normalized at construction (absolute / ``..`` / empty paths are
    rejected). ``digest`` is derived from ``content`` and is not an init argument.
    """

    path: str
    content: bytes
    metadata: tuple[tuple[str, str], ...] = ()
    digest: str = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_relative_path(self.path))
        object.__setattr__(self, "metadata", tuple(self.metadata))
        object.__setattr__(self, "digest", digest_bytes(self.content))


@dataclass(frozen=True)
class DesiredArtifactSet:
    """The complete set of artifacts an action declares for one output root.

    On construction the artifacts are normalized, checked for duplicate output
    paths (raising :class:`DuplicateArtifactError`), and sorted by path so the
    set — and every plan derived from it — is deterministic.
    """

    action: ActionIdentity
    artifacts: tuple[DesiredArtifact, ...]

    def __post_init__(self) -> None:
        seen: dict[str, str] = {}
        for artifact in self.artifacts:
            if artifact.path in seen:
                raise DuplicateArtifactError(
                    f"Two desired artifacts claim the same output path {artifact.path!r}."
                )
            seen[artifact.path] = artifact.digest
        ordered = tuple(sorted(self.artifacts, key=lambda a: a.path))
        object.__setattr__(self, "artifacts", ordered)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(a.path for a in self.artifacts)

    def digest_for(self, path: str) -> str | None:
        for artifact in self.artifacts:
            if artifact.path == path:
                return artifact.digest
        return None


@dataclass(frozen=True)
class ActionPlan:
    """A dry-run reconciliation plan. All tuples are sorted relative paths."""

    creates: tuple[str, ...]
    updates: tuple[str, ...]
    deletes: tuple[str, ...]
    unchanged: tuple[str, ...]

    @property
    def is_noop(self) -> bool:
        return not (self.creates or self.updates or self.deletes)


@dataclass(frozen=True)
class ActionResult:
    """The outcome of an applied reconciliation."""

    writes: tuple[str, ...]
    deletions: tuple[str, ...]
    unchanged: int
    digests: tuple[tuple[str, str], ...]
