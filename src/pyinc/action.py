"""Declared-output reconciliation for pure query results."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, overload

from ._locking import FileLock, _validate_lock_timeout
from ._safe_fs import (
    UnsafeFilesystemPathError,
    atomic_write,
    read_regular_file,
    remove_empty_directory,
    unlink_regular_file,
)
from .errors import ActionLockTimeoutError, ActionManifestError, ActionPathError

if TYPE_CHECKING:
    from .runtime import Database

_MANIFEST_VERSION = 2
_DEFAULT_LOCK_TIMEOUT = 30.0
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"com{number}" for number in "¹²³"),
    *(f"lpt{number}" for number in "¹²³"),
}
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')


@dataclass(frozen=True)
class Output:
    """A root-relative output path and its exact desired bytes."""

    path: str
    content: bytes

    @classmethod
    def text(cls, path: str, text: str, *, encoding: str = "utf-8") -> Output:
        return cls(path=path, content=text.encode(encoding))


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of :meth:`Action.reconcile`, grouped by reason."""

    created: tuple[str, ...]
    updated: tuple[str, ...]
    repaired: tuple[str, ...]
    deleted: tuple[str, ...]
    unchanged: tuple[str, ...]
    dry_run: bool


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_rel(path: str) -> str:
    """Validate a portable action path and return its POSIX spelling."""
    if type(path) is not str:
        raise ActionPathError("Output paths must be strings.")
    if (
        not path
        or path == "."
        or "\0" in path
        or any(0xD800 <= ord(character) <= 0xDFFF for character in path)
    ):
        raise ActionPathError("Output paths must name a non-empty relative file.")
    if "\\" in path:
        raise ActionPathError(f"Output paths must use portable POSIX separators, got: {path!r}")
    windows = PureWindowsPath(path)
    pure = PurePosixPath(path)
    raw_parts = path.split("/")
    if pure.is_absolute() or windows.is_absolute() or bool(windows.drive) or path.startswith("//"):
        raise ActionPathError(f"Output path must be relative, got: {path!r}")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ActionPathError(f"Output path is not normalized or escapes the root: {path!r}")
    for part in raw_parts:
        if (
            part[-1] in {" ", "."}
            or any(character in _WINDOWS_INVALID_CHARACTERS for character in part)
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        ):
            raise ActionPathError(f"Output path is not portable across filesystems: {path!r}")
    normalized = pure.as_posix()
    if normalized != path:
        raise ActionPathError(f"Output path is not normalized: {path!r}")
    return normalized


def _portable_path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _ancestors(relative: str) -> tuple[str, ...]:
    parts = relative.split("/")
    return tuple("/".join(parts[:depth]) for depth in range(1, len(parts)))


def _validate_path_set(paths: Iterable[str], *, source: str) -> dict[str, str]:
    validated: dict[str, str] = {}
    portable: dict[str, str] = {}
    for raw_path in paths:
        path = _normalize_rel(raw_path)
        if path in validated:
            raise ActionPathError(f"Duplicate {source} path: {path!r}")
        key = _portable_path_key(path)
        previous = portable.get(key)
        if previous is not None:
            raise ActionPathError(f"Portable-path collision in {source}: {previous!r} and {path!r}")
        validated[path] = path
        portable[key] = path

    ordered = sorted(portable.items())
    for index, (key, path) in enumerate(ordered):
        prefix = f"{key}/"
        for other_key, other_path in ordered[index + 1 :]:
            if other_key.startswith(prefix):
                raise ActionPathError(
                    f"Conflicting file and directory {source} paths: {path!r} and {other_path!r}"
                )
            if other_key > prefix and not other_key.startswith(prefix):
                break
    return validated


def _tool_digest(tool: str) -> str:
    return hashlib.sha256(tool.encode("utf-8")).hexdigest()


def _lock_root_digest(root: Path) -> str:
    portable_root = unicodedata.normalize("NFC", os.fspath(root)).casefold()
    return hashlib.sha256(os.fsencode(portable_root)).hexdigest()


def _manifest_root_digest(root: Path) -> str:
    exact_root = os.path.normcase(os.fspath(root))
    return hashlib.sha256(os.fsencode(exact_root)).hexdigest()


def _manifest_path(state_dir: Path, tool: str) -> Path:
    return state_dir / f".pyinc-action.{_tool_digest(tool)}.json"


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ActionManifestError(f"Duplicate manifest field: {key!r}")
        result[key] = value
    return result


def _read_manifest(state_dir: Path, tool: str, root_digest: str) -> tuple[bool, dict[str, str]]:
    path = _manifest_path(state_dir, tool)
    try:
        raw = read_regular_file(path)
        if raw is None:
            return False, {}
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except ActionManifestError:
        raise
    except (OSError, ValueError, RecursionError, OverflowError) as error:
        raise ActionManifestError(f"Cannot read action manifest {path}: {error}") from error

    if not isinstance(data, dict) or set(data) != {"root", "tool", "version", "outputs"}:
        raise ActionManifestError("Action manifest must contain root, tool, version, and outputs.")
    if data["version"] != _MANIFEST_VERSION or type(data["version"]) is not int:
        raise ActionManifestError(
            f"Unsupported action manifest version; expected {_MANIFEST_VERSION}."
        )
    if data["tool"] != tool or not isinstance(data["tool"], str):
        raise ActionManifestError("Action manifest tool identity does not match this action.")
    if data["root"] != root_digest or not isinstance(data["root"], str):
        raise ActionManifestError("Action manifest root identity does not match this action.")
    raw_outputs = data["outputs"]
    if not isinstance(raw_outputs, dict):
        raise ActionManifestError("Action manifest outputs must be an object.")

    try:
        _validate_path_set(raw_outputs, source="manifest")
    except ActionPathError as error:
        raise ActionManifestError(f"Action manifest contains an invalid path: {error}") from error
    outputs: dict[str, str] = {}
    for raw_path, raw_digest in raw_outputs.items():
        if not isinstance(raw_path, str):
            raise ActionManifestError("Action manifest output paths must be strings.")
        path_key = _normalize_rel(raw_path)
        if (
            not isinstance(raw_digest, str)
            or len(raw_digest) != 64
            or any(character not in "0123456789abcdef" for character in raw_digest)
        ):
            raise ActionManifestError(
                f"Action manifest contains an invalid SHA-256 digest for {path_key!r}."
            )
        outputs[path_key] = raw_digest
    return True, outputs


def _write_manifest(
    state_dir: Path,
    tool: str,
    root_digest: str,
    outputs: Mapping[str, str],
) -> None:
    payload = {
        "root": root_digest,
        "tool": tool,
        "version": _MANIFEST_VERSION,
        "outputs": dict(sorted(outputs.items())),
    }
    _atomic_write(
        _manifest_path(state_dir, tool),
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )


def _atomic_write(target: Path, data: bytes) -> None:
    try:
        atomic_write(target, data)
    except UnsafeFilesystemPathError as error:
        raise ActionPathError(str(error)) from error


def _lock_path(root: Path, tool: str) -> Path:
    identity = _lock_root_digest(root).encode("ascii") + b"\0" + tool.encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return _action_lock_directory() / f"{digest}.lock"


def _action_lock_directory() -> Path:
    getuid = getattr(os, "getuid", None)
    uid = getuid() if getuid is not None else None
    if uid is None:
        user_identity = hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:16]
    else:
        user_identity = str(uid)
    temp_directory = Path(tempfile.gettempdir()).resolve(strict=True)
    directory = temp_directory / f"pyinc-action-locks-{user_identity}"
    with contextlib.suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ActionPathError(f"Action lock path is not a directory: {directory}")
    if uid is not None and metadata.st_uid != uid:
        raise ActionPathError(f"Action lock path is owned by another user: {directory}")
    if uid is not None and stat.S_IMODE(metadata.st_mode) & 0o077:
        directory.chmod(0o700)
    return directory


def _safe_target(root: Path, relative: str) -> tuple[Path, os.stat_result | None]:
    target = root.joinpath(*relative.split("/"))
    try:
        common = os.path.commonpath((os.fspath(root), os.fspath(target)))
    except ValueError as error:
        raise ActionPathError(f"Output path escapes the action root: {relative!r}") from error
    if common != os.fspath(root):
        raise ActionPathError(f"Output path escapes the action root: {relative!r}")

    current = root
    target_metadata: os.stat_result | None = None
    for part in relative.split("/"):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise ActionPathError(
                f"Cannot safely inspect owned output path {relative!r}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ActionPathError(f"Owned output path contains a symbolic link: {relative!r}")
        if current == target:
            target_metadata = metadata
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ActionPathError(f"Owned output parent is not a directory: {relative!r}")

    resolved_parent = target.parent.resolve(strict=False)
    try:
        resolved_common = os.path.commonpath((os.fspath(root), os.fspath(resolved_parent)))
    except ValueError as error:
        raise ActionPathError(f"Output path escapes the action root: {relative!r}") from error
    if resolved_common != os.fspath(root):
        raise ActionPathError(f"Output path escapes the action root: {relative!r}")
    return target, target_metadata


class Action:
    """Reconcile a pure desired-output function against a filesystem root."""

    def __init__(
        self,
        fn: Callable[..., Iterable[Output]],
        *,
        tool: str,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if type(tool) is not str:
            raise TypeError("@action tool identity must be a string.")
        if not tool:
            raise ValueError("@action requires a non-empty tool identity.")
        try:
            tool.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("@action tool identity must be valid UTF-8.") from error
        lock_timeout = _validate_lock_timeout(lock_timeout)
        self.fn = fn
        self.tool = tool
        self.lock_timeout = lock_timeout
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__
        self.__module__ = fn.__module__
        self.__wrapped__ = fn

    def outputs(self, db: Database, *args: object, **kwargs: object) -> tuple[Output, ...]:
        return tuple(self.fn(db, *args, **kwargs))

    def reconcile(
        self,
        db: Database,
        *args: object,
        root: str | os.PathLike[str],
        dry_run: bool = False,
        state_dir: str | os.PathLike[str] | None = None,
        lock_timeout: float | None = None,
        **kwargs: object,
    ) -> ReconcileResult:
        """Validate, lock, and converge all owned outputs under ``root``."""
        try:
            root_text = os.fspath(root)
            state_text = os.fspath(state_dir) if state_dir is not None else root_text
            if "\0" in root_text or "\0" in state_text:
                raise ValueError("embedded null character in path")
            root_path = Path(root_text).resolve(strict=False)
            state_path = (
                Path(state_text).resolve(strict=False) if state_dir is not None else root_path
            )
        except (OSError, TypeError, ValueError) as error:
            raise ActionPathError(f"Action root or state directory is invalid: {error}") from error

        for path, label in ((root_path, "owned output path"), (state_path, "action state path")):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                raise ActionPathError(f"Cannot safely inspect {label}: {error}") from error
            if not stat.S_ISDIR(metadata.st_mode):
                raise ActionPathError(f"Cannot safely inspect {label}: not a directory: {path}")
        timeout = self.lock_timeout if lock_timeout is None else lock_timeout
        timeout = _validate_lock_timeout(timeout)
        lock_paths = sorted({_lock_path(root_path, self.tool), _lock_path(state_path, self.tool)})
        locks: list[FileLock] = []
        deadline = time.monotonic() + timeout
        try:
            for lock_path in lock_paths:
                lock = FileLock(
                    lock_path,
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                lock.acquire()
                locks.append(lock)
        except BaseException as error:
            for lock in reversed(locks):
                lock.release()
            if isinstance(error, TimeoutError):
                raise ActionLockTimeoutError(
                    f"Timed out after {timeout:g}s waiting to reconcile {self.tool!r} at "
                    f"{root_path}."
                ) from error
            if isinstance(error, OSError):
                raise ActionPathError(
                    f"Cannot safely acquire the reconciliation lock for {self.tool!r} "
                    f"at {root_path}: {error}"
                ) from error
            raise
        try:
            desired_map = self._desired_map(self.outputs(db, *args, **kwargs))
            return self._reconcile_locked(
                desired_map,
                root=root_path,
                state_dir=state_path,
                dry_run=dry_run,
            )
        finally:
            for lock in reversed(locks):
                lock.release()

    def _desired_map(self, outputs: Iterable[Output]) -> dict[str, bytes]:
        desired: dict[str, bytes] = {}
        raw_paths: list[str] = []
        for output in outputs:
            if not isinstance(output, Output):
                raise TypeError(f"Action {self.tool!r} returned a non-Output value.")
            if type(output.content) is not bytes:
                raise TypeError(f"Output content for {output.path!r} must be bytes.")
            path = _normalize_rel(output.path)
            raw_paths.append(path)
            if path in desired:
                raise ActionPathError(f"Duplicate output path from action {self.tool!r}: {path!r}")
            desired[path] = output.content
        _validate_path_set(raw_paths, source="desired output")
        return desired

    def _reconcile_locked(
        self,
        desired: Mapping[str, bytes],
        *,
        root: Path,
        state_dir: Path,
        dry_run: bool,
    ) -> ReconcileResult:
        root_identity = _manifest_root_digest(root)
        manifest_exists, previous = _read_manifest(state_dir, self.tool, root_identity)
        _validate_path_set(desired, source="owned output")

        # A ledger entry that conflicts with the new desired layout is just an
        # orphan of the previous layout; it is released below rather than
        # rejected, so an output can migrate between file and directory forms.
        previous_only = sorted(set(previous) - set(desired))
        desired_by_key = {_portable_path_key(path): path for path in desired}
        for relative in previous_only:
            twin = desired_by_key.get(_portable_path_key(relative))
            if twin is not None:
                # On a case-insensitive filesystem the orphan and the desired
                # output are one file; deleting the orphan would destroy the
                # reconciled output, so a spelling change stays rejected.
                raise ActionPathError(
                    f"Portable-path collision in owned output: {relative!r} and {twin!r}"
                )

        # Directories that stand where a desired file must go exist only
        # because the previous layout nested owned outputs there. Record them
        # so they can be pruned once their orphans are deleted.
        prune_map: dict[str, str] = {}
        for relative in previous_only:
            for ancestor in _ancestors(relative):
                ancestor_key = _portable_path_key(ancestor)
                if any(
                    ancestor_key == desired_key or ancestor_key.startswith(f"{desired_key}/")
                    for desired_key in desired_by_key
                ):
                    prune_map.setdefault(ancestor_key, ancestor)

        manifest = _manifest_path(state_dir, self.tool)
        try:
            manifest_relative = manifest.relative_to(root).as_posix()
        except ValueError:
            manifest_relative = None
        if manifest_relative is not None:
            manifest_key = _portable_path_key(manifest_relative)
            for relative in set(desired) | set(previous):
                relative_key = _portable_path_key(relative)
                if (
                    relative_key == manifest_key
                    or relative_key.startswith(f"{manifest_key}/")
                    or manifest_key.startswith(f"{relative_key}/")
                ):
                    raise ActionPathError(
                        f"Owned output path conflicts with the action manifest: {relative!r}"
                    )

        targets: dict[str, tuple[Path, os.stat_result | None]] = {}
        for relative in previous_only:
            target, metadata = _safe_target(root, relative)
            if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                raise ActionPathError(f"Owned output target is not a regular file: {relative!r}")
            targets[relative] = (target, metadata)

        orphan_file_paths = {
            relative for relative in previous_only if targets[relative][1] is not None
        }

        for relative in sorted(desired):
            if any(ancestor in orphan_file_paths for ancestor in _ancestors(relative)):
                # An orphan file from the previous layout occupies exactly this
                # parent path; a mere casefold twin of a parent frees nothing
                # when it is deleted, so it does not lift validation. The
                # orphan's components were validated when it was inspected, it
                # is deleted before this path is written, and the write
                # revalidates the target, so classify the target as absent
                # here.
                targets[relative] = (root.joinpath(*relative.split("/")), None)
                continue
            target, metadata = _safe_target(root, relative)
            if (
                metadata is not None
                and stat.S_ISDIR(metadata.st_mode)
                and _portable_path_key(relative) in prune_map
            ):
                # The previous layout nested owned outputs where this file now
                # belongs; the directory is pruned before the write.
                metadata = None
            if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                raise ActionPathError(f"Owned output target is not a regular file: {relative!r}")
            targets[relative] = (target, metadata)

        created: list[str] = []
        updated: list[str] = []
        repaired: list[str] = []
        unchanged: list[str] = []
        write_paths: list[str] = []
        desired_hashes = {path: _content_hash(content) for path, content in desired.items()}

        for relative in sorted(desired):
            target, metadata = targets[relative]
            try:
                current = read_regular_file(target) if metadata is not None else None
            except UnsafeFilesystemPathError as error:
                raise ActionPathError(str(error)) from error
            if current is not None and _content_hash(current) == desired_hashes[relative]:
                unchanged.append(relative)
                continue

            write_paths.append(relative)
            if current is None:
                (repaired if relative in previous else created).append(relative)
            elif relative in previous and _content_hash(current) != previous[relative]:
                repaired.append(relative)
            else:
                updated.append(relative)

        deleted: list[str] = []
        delete_paths: list[str] = []
        for relative in previous_only:
            _, metadata = targets[relative]
            if metadata is not None:
                deleted.append(relative)
                delete_paths.append(relative)

        if not dry_run:
            # Orphans are deleted and their emptied directories pruned before
            # any write so a path can change between file and directory forms.
            # Each step is individually atomic; if the run stops early, the
            # prior ledger lets the next locked reconcile finish the set.
            for relative in delete_paths:
                target, metadata = _safe_target(root, relative)
                if metadata is None:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ActionPathError(
                        f"Refusing to delete a non-regular owned target: {relative!r}"
                    )
                try:
                    unlink_regular_file(target)
                except UnsafeFilesystemPathError as error:
                    raise ActionPathError(str(error)) from error

            for relative in sorted(
                prune_map.values(), key=lambda path: path.count("/"), reverse=True
            ):
                target, metadata = _safe_target(root, relative)
                if metadata is None or not stat.S_ISDIR(metadata.st_mode):
                    continue
                try:
                    remove_empty_directory(target)
                except UnsafeFilesystemPathError as error:
                    raise ActionPathError(str(error)) from error
                except OSError as error:
                    raise ActionPathError(
                        f"Cannot prune directory {relative!r} left by the previous layout: {error}"
                    ) from error

            for relative in write_paths:
                target, metadata = _safe_target(root, relative)
                if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                    raise ActionPathError(
                        f"Owned output target is not a regular file: {relative!r}"
                    )
                _atomic_write(target, desired[relative])

            if desired_hashes != previous or (desired_hashes and not manifest_exists):
                _write_manifest(state_dir, self.tool, root_identity, desired_hashes)

        return ReconcileResult(
            created=tuple(created),
            updated=tuple(updated),
            repaired=tuple(repaired),
            deleted=tuple(deleted),
            unchanged=tuple(unchanged),
            dry_run=dry_run,
        )

    def plan(
        self,
        db: Database,
        *args: object,
        root: str | os.PathLike[str],
        state_dir: str | os.PathLike[str] | None = None,
        lock_timeout: float | None = None,
        **kwargs: object,
    ) -> ReconcileResult:
        return self.reconcile(
            db,
            *args,
            root=root,
            dry_run=True,
            state_dir=state_dir,
            lock_timeout=lock_timeout,
            **kwargs,
        )


@overload
def action(
    fn: Callable[..., Iterable[Output]],
    *,
    tool: str,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
) -> Action: ...


@overload
def action(
    fn: None = None,
    *,
    tool: str,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
) -> Callable[[Callable[..., Iterable[Output]]], Action]: ...


def action(
    fn: Callable[..., Iterable[Output]] | None = None,
    *,
    tool: str,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
) -> Action | Callable[[Callable[..., Iterable[Output]]], Action]:
    """Declare a stable tool identity and its pure desired-output function."""

    def decorate(wrapped: Callable[..., Iterable[Output]]) -> Action:
        return Action(wrapped, tool=tool, lock_timeout=lock_timeout)

    if fn is None:
        return decorate
    return decorate(fn)
