"""Output-path containment, normalization, and identifier sanitization.

These helpers run *before* and *during* reconciliation to guarantee every write
and delete stays inside one explicit output root. They are pure functions over
strings/paths (plus existence/symlink checks against the real filesystem) and
perform no writes themselves.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from .errors import InvalidArtifactPathError, SymlinkEscapeError

# Characters that are unsafe in a single path component on common filesystems.
_UNSAFE_COMPONENT_CHARS = set('<>:"/\\|?*')


def normalize_relative_path(raw: str) -> str:
    """Return a canonical ``a/b/c`` relative path or raise.

    Rejects: non-strings, empty/whitespace-only, absolute paths, Windows drive
    (``C:``) / UNC prefixes, and any ``..`` traversal component. ``.`` segments
    and redundant separators are collapsed. Separators are normalized to ``/`` so
    the canonical form is identical across platforms (important for deterministic
    manifests and cross-platform from-scratch parity).
    """
    if not isinstance(raw, str):
        raise InvalidArtifactPathError(
            f"Output path must be a string, got {type(raw).__name__}."
        )
    if raw.strip() == "":
        raise InvalidArtifactPathError("Output path must not be empty or whitespace-only.")

    candidate = raw.replace("\\", "/")
    if candidate.startswith("/"):
        raise InvalidArtifactPathError(
            f"Output path must be relative to the output root, got absolute path {raw!r}."
        )
    # Windows drive letter (``C:...``) or UNC-ish prefix.
    if len(candidate) >= 2 and candidate[1] == ":":
        raise InvalidArtifactPathError(
            f"Output path must be relative, got drive-qualified path {raw!r}."
        )

    parts: list[str] = []
    for part in candidate.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise InvalidArtifactPathError(
                f"Output path must not contain a '..' traversal component: {raw!r}."
            )
        parts.append(part)

    if not parts:
        raise InvalidArtifactPathError(f"Output path normalizes to an empty path: {raw!r}.")
    return "/".join(parts)


def sanitize_component(name: str, *, replacement: str = "_") -> str:
    """Make ``name`` safe to use as a single filename component.

    Unsafe characters (path separators, reserved Windows characters, control
    characters) are replaced with ``replacement``. Leading/trailing dots and
    spaces are stripped. Raises :class:`InvalidArtifactPathError` if nothing
    usable remains (so a caller never silently produces an empty component).
    """
    cleaned = "".join(
        replacement if (ch in _UNSAFE_COMPONENT_CHARS or ord(ch) < 32) else ch
        for ch in name
    ).strip(" .")
    if not cleaned or cleaned in (".", ".."):
        raise InvalidArtifactPathError(
            f"Identifier {name!r} cannot be sanitized into a safe path component."
        )
    return cleaned


def _real_within(root_real: str, candidate_real: str) -> bool:
    if candidate_real == root_real:
        return True
    return candidate_real.startswith(root_real + os.sep)


def resolve_contained_target(output_root: Path, rel: str) -> Path:
    """Return the absolute target ``output_root / rel`` for an already-normalized
    relative path, after verifying no *existing* path component is a symlink that
    escapes the output root.

    The check walks each existing component (including the final one): if its
    real path lands outside the root, :class:`SymlinkEscapeError` is raised. This
    blocks writing *through* a directory symlink and clobbering an escaping
    symlink planted at an output path. Components that do not yet exist are safe —
    they will be created as real directories under the root.
    """
    root_real = os.path.realpath(output_root)
    current = Path(output_root)
    for part in PurePosixPath(rel).parts:
        current = current / part
        exists = current.is_symlink() or current.exists()
        if exists and not _real_within(root_real, os.path.realpath(current)):
            raise SymlinkEscapeError(
                f"Output path {rel!r} escapes the output root through a symlink."
            )
    return output_root / rel


def is_deletion_target_safe(output_root: Path, rel: str) -> bool:
    """Return ``True`` if ``rel`` is safe to delete: it resolves to a regular file
    whose real path stays within the output root. Anything else (missing,
    directory, or a symlink escaping the root) is left untouched."""
    target = output_root / rel
    if not target.is_file() or target.is_symlink():
        return False
    root_real = os.path.realpath(output_root)
    return _real_within(root_real, os.path.realpath(target))
