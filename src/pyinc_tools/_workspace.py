from __future__ import annotations

import contextlib
import errno
import fnmatch
import hashlib
import io
import os
import re
import shutil
import stat
import sys
import threading
import time
import tokenize
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

DEFAULT_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "dist",
        "node_modules",
        "venv",
        ".tox",
        ".nox",
        ".eggs",
        "site-packages",
        "__pypackages__",
    }
)

# The session reads Python sources anywhere in the root, and pyproject.toml and
# requirements.txt only at the root. The mirror copies those files and the root
# requirements file's include closure, and nothing else.
_ROOT_MANIFEST_NAMES = frozenset({"pyproject.toml", "requirements.txt"})
# A session refuses a workspace with more files than this to copy. Past it the
# root is most likely a parent of several projects or holds vendored trees.
DEFAULT_MAX_WORKSPACE_FILES = 1_000
# Every poll walks the workspace and stats each file it copies.
_DEFAULT_POLL_INTERVAL_S = 0.5
# The signal a closed session raises to its watcher. A driver that reports no
# closed state is recognized by this message alone.
SESSION_CLOSED_MESSAGE = "WorkspaceSession is closed."
# What traversing to a path reports once the path is gone. A parent that became
# a regular file raises ENOTDIR on POSIX. Windows answers the same shape with
# ERROR_PATH_NOT_FOUND or ERROR_DIRECTORY, which arrive as FileNotFoundError
# and NotADirectoryError. Both mean absence on both platforms, so neither may
# be reported as an unsafe path.
_MISSING_PATH_ERRORS = (FileNotFoundError, NotADirectoryError)
# pip reads an include option with optparse, which takes the target after
# whitespace or "=", or attached to a short option, as in "-rbase.txt". The
# requirements integration reads the same forms.
_REQUIREMENTS_REFERENCE = re.compile(r"^(?:-[rc]\s*|--(?:requirement|constraint)(?:\s+|=))(.+)$")


class WorkspaceTooLargeError(ValueError):
    """The workspace holds more files than the session accepts."""

    def __init__(self, root: str, limit: int) -> None:
        super().__init__(
            f"The workspace {root} holds more than {limit:,} Python and manifest files. "
            "Exclude directories with pyinc.workspace.exclude, or raise the limit with "
            "pyinc.workspace.maxFiles."
        )
        self.root = root
        self.limit = limit


def _encode_python_text(source: str) -> bytes:
    """Encode an editor buffer according to its PEP 263 declaration."""

    probe = io.BytesIO(source.encode("utf-8"))
    try:
        encoding, _ = tokenize.detect_encoding(probe.readline)
    except SyntaxError:
        encoding = "utf-8"
    try:
        if encoding == "utf-8-sig" and source.startswith("\ufeff"):
            return source.removeprefix("\ufeff").encode("utf-8-sig")
        return source.encode(encoding)
    except UnicodeEncodeError:
        lines = source.splitlines(keepends=True)
        cookie = re.compile(r"^([ \t\f]*#.*?coding[:=][ \t]*)([-\w.]+)")
        for index in range(min(2, len(lines))):
            match = cookie.match(lines[index])
            if match is None:
                continue
            replacement = "utf-8".ljust(len(match.group(2)))
            lines[index] = (
                lines[index][: match.start(2)] + replacement + lines[index][match.end(2) :]
            )
            break
        return "".join(lines).encode("utf-8")


def _logical_requirement_lines(text: str) -> tuple[str, ...]:
    physical = text.splitlines()
    logical: list[str] = []
    index = 0
    while index < len(physical):
        parts = [physical[index]]
        while parts[-1].endswith("\\") and index + 1 < len(physical):
            parts[-1] = parts[-1][:-1]
            index += 1
            parts.append(physical[index])
        logical.append("".join(parts))
        index += 1
    return tuple(logical)


def _strip_requirement_inline_comment(line: str) -> str:
    in_quote: str | None = None
    for index, character in enumerate(line):
        if in_quote is not None:
            if character == in_quote:
                in_quote = None
            continue
        if character in {"'", '"'}:
            in_quote = character
        elif character == "#" and index > 0 and line[index - 1].isspace():
            return line[:index].rstrip()
    return line


def _workspace_path_allowed(
    path: Path,
    root: Path,
    ignored_dir_names: frozenset[str],
    exclude_globs: tuple[str, ...],
) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if any(part in ignored_dir_names for part in relative.parts):
        return False
    directory = root
    for part in relative.parts[:-1]:
        directory /= part
        if _is_ignored_directory(directory, ignored_dir_names):
            return False
    return not _is_excluded(path, root, exclude_globs)


def _is_ignored_directory(path: Path, ignored_dir_names: frozenset[str]) -> bool:
    """Whether the walk leaves out the directory at `path`.

    Packaging metadata directories end in `.egg-info`. A virtual environment
    can have any name, and the `pyvenv.cfg` at its top marks it.
    """

    if path.name in ignored_dir_names or path.name.endswith(".egg-info"):
        return True
    try:
        return (path / "pyvenv.cfg").is_file()
    except OSError:
        return False


def _is_workspace_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except _MISSING_PATH_ERRORS:
        # An entry the probe cannot reach is absent, and absent is not a link.
        return False
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_point)


def _first_link_component(path: Path, root: Path) -> Path | None:
    """The first component of `path` below `root` that is a link, if any.

    `_validate_symlink` checks that component, so a link out of the root raises.
    """

    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if _is_workspace_link(current):
            _validate_symlink(current, root)
            return current
    return None


def _reject_symlink_components(path: Path, root: Path) -> None:
    link = _first_link_component(path, root)
    if link is not None:
        raise ValueError(f"workspace symlinks are not supported: {link!s}")


def _require_workspace_parent_identity(
    descriptor: int,
    root: Path,
    parent_parts: tuple[str, ...],
    path: Path,
    directory_flags: int,
) -> None:
    """Reject a traversed POSIX parent that moved away from its workspace path."""

    comparison = -1
    try:
        try:
            comparison = os.open(root, directory_flags)
            for component in parent_parts:
                previous = comparison
                comparison = os.open(
                    component,
                    directory_flags,
                    dir_fd=previous,
                )
                os.close(previous)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"workspace file parent is not safe to read: {path!s}") from exc

        opened = os.fstat(descriptor)
        current = os.fstat(comparison)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or not os.path.samestat(opened, current)
        ):
            raise ValueError(f"workspace file parent changed identity while opening: {path!s}")
    finally:
        if comparison >= 0:
            os.close(comparison)


def _is_link_entry(component: str, dir_fd: int) -> bool:
    try:
        metadata = os.lstat(component, dir_fd=dir_fd)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode)


def _read_workspace_file(path: Path, root: Path) -> bytes:
    """Read one in-root regular file without following a path component."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"workspace file is outside the root: {path!s}") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"workspace file path is not normalized: {path!s}")

    if os.name == "nt":
        return _read_workspace_file_windows(path, root)

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            try:
                next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                raise
            except NotADirectoryError as exc:
                # A parent that became a plain file means the path is gone. A
                # symlinked parent reports the same errno under O_NOFOLLOW, and
                # it is the escape this walk exists to reject.
                if _is_link_entry(component, descriptor):
                    raise ValueError(
                        f"workspace file contains an unsafe path component: {path!s}"
                    ) from exc
                raise
            except OSError as exc:
                raise ValueError(
                    f"workspace file contains an unsafe path component: {path!s}"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor

        _require_workspace_parent_identity(
            descriptor,
            root,
            relative.parts[:-1],
            path,
            directory_flags,
        )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            file_descriptor = os.open(relative.parts[-1], flags, dir_fd=descriptor)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError(f"workspace file symlinks are not supported: {path!s}") from exc
            raise ValueError(f"workspace file is not safe to read: {path!s}") from exc
        try:
            metadata = os.fstat(file_descriptor)
            if stat.S_ISDIR(metadata.st_mode):
                raise IsADirectoryError(path)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"workspace path is not a regular file: {path!s}")
            with os.fdopen(file_descriptor, "rb") as handle:
                file_descriptor = -1
                return handle.read()
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _read_workspace_file_windows(path: Path, root: Path) -> bytes:
    """Best-effort no-link read on Windows with pre/post handle identity checks."""

    _reject_symlink_components(path, root)
    try:
        before = path.lstat()
    except _MISSING_PATH_ERRORS:
        raise
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"workspace file symlinks are not supported: {path!s}")
    if stat.S_ISDIR(before.st_mode):
        raise IsADirectoryError(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"workspace path is not a regular file: {path!s}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except _MISSING_PATH_ERRORS:
        # The path went away between the stat and the open. The mirror settles
        # that by dropping its copy. An unsafe-read report would fail the
        # refresh and be retried on every later tick.
        raise
    except OSError as exc:
        raise ValueError(f"workspace file is not safe to read: {path!s}") from exc
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
        _reject_symlink_components(path, root)
        identities = {
            (before.st_dev, before.st_ino),
            (opened.st_dev, opened.st_ino),
            (after.st_dev, after.st_ino),
        }
        if (
            len(identities) != 1
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after.st_mode)
        ):
            raise ValueError(f"workspace file changed identity while opening: {path!s}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _requirements_reference_paths(
    root: Path,
    ignored_dir_names: frozenset[str],
    exclude_globs: tuple[str, ...],
) -> set[Path]:
    """Return the in-root recursive ``-r``/``-c`` path closure."""

    entrypoint = root / "requirements.txt"
    if not _workspace_path_allowed(entrypoint, root, ignored_dir_names, exclude_globs):
        return set()
    pending = [entrypoint]
    visited: set[Path] = set()
    referenced: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        if _first_link_component(current, root) is not None:
            # A linked requirements file is skipped as the walk skips a linked
            # file, and its includes are not followed.
            continue
        try:
            text = _read_workspace_file(current, root).decode("utf-8")
        except (FileNotFoundError, IsADirectoryError):
            continue
        except (OSError, UnicodeError):
            continue
        for line in _logical_requirement_lines(text):
            match = _REQUIREMENTS_REFERENCE.match(_strip_requirement_inline_comment(line.strip()))
            if match is None:
                continue
            raw_target_text = match.group(1).strip()
            if "\0" in raw_target_text:
                continue
            raw_target = Path(raw_target_text)
            if raw_target.is_absolute():
                target = Path(os.path.abspath(raw_target))
            else:
                target = Path(os.path.abspath(current.parent / raw_target))
            if not _workspace_path_allowed(target, root, ignored_dir_names, exclude_globs):
                continue
            if _first_link_component(target, root) is not None:
                continue
            try:
                metadata = target.lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if _is_workspace_link(target):
                _validate_symlink(target, root)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            referenced.add(target)
            pending.append(target)
    referenced.discard(entrypoint)
    return referenced


def _workspace_files(
    root: str,
    ignored_dir_names: frozenset[str],
    exclude_globs: tuple[str, ...],
    max_files: int | None = None,
) -> tuple[set[Path], set[Path]]:
    """Return the files the mirror copies, and the requirements includes among them.

    With `max_files`, the walk stops and raises `WorkspaceTooLargeError` as soon
    as it has found more files than that.
    """

    root_path = Path(root).resolve(strict=False)
    files: set[Path] = set()
    for current_root, dirnames, filenames in os.walk(root_path):
        current_path = Path(current_root)
        kept_dirs: list[str] = []
        for name in dirnames:
            path = current_path / name
            if _is_ignored_directory(path, ignored_dir_names) or _is_excluded(
                path, root_path, exclude_globs
            ):
                continue
            if _is_workspace_link(path):
                _validate_symlink(path, root_path)
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for filename in filenames:
            source_path = current_path / filename
            if not _is_relevant_file(source_path, root_path) or _is_excluded(
                source_path, root_path, exclude_globs
            ):
                continue
            if _is_workspace_link(source_path):
                # Skipped as a directory link is. A link out of the root still
                # refuses the workspace.
                _validate_symlink(source_path, root_path)
                continue
            files.add(source_path)
            if max_files is not None and len(files) > max_files:
                raise WorkspaceTooLargeError(str(root_path), max_files)
    referenced = _requirements_reference_paths(root_path, ignored_dir_names, exclude_globs)
    files.update(path for path in referenced if path.is_file())
    if max_files is not None and len(files) > max_files:
        raise WorkspaceTooLargeError(str(root_path), max_files)
    return files, referenced


def _collect_filesystem_snapshot(
    root: str,
    ignored_dir_names: frozenset[str],
    exclude_globs: tuple[str, ...] = (),
    stat_cache: dict[str, tuple[int, int, str]] | None = None,
) -> dict[str, str]:
    """Sha256 per path of every file the mirror copies.

    `stat_cache` maps a path to its modification time in nanoseconds, its size
    and its hash. A file whose time and size match its entry keeps that hash
    and is not read. The cache is left holding an entry for each file hashed
    here. The stat is taken before the read, so an edit that lands during the
    read changes the stat the next snapshot sees.
    """

    root_path = Path(root).resolve(strict=False)
    previous = dict(stat_cache) if stat_cache is not None else {}
    current: dict[str, tuple[int, int, str]] = {}
    snapshot: dict[str, str] = {}
    files, _referenced = _workspace_files(root, ignored_dir_names, exclude_globs)
    for source_path in files:
        key = str(source_path)
        try:
            metadata = source_path.lstat()
        except _MISSING_PATH_ERRORS:
            continue
        cached = previous.get(key)
        if cached is not None and cached[:2] == (metadata.st_mtime_ns, metadata.st_size):
            digest = cached[2]
        else:
            try:
                content = _read_workspace_file(source_path, root_path)
            except _MISSING_PATH_ERRORS:
                continue
            digest = hashlib.sha256(content).hexdigest()
        snapshot[key] = digest
        current[key] = (metadata.st_mtime_ns, metadata.st_size, digest)
    if stat_cache is not None:
        stat_cache.clear()
        stat_cache.update(current)
    return snapshot


def _is_relevant_file(path: Path, root: Path) -> bool:
    if path.suffix == ".py":
        return True
    return path.name in _ROOT_MANIFEST_NAMES and path.parent == root


def _is_excluded(path: Path, root: Path, patterns: tuple[str, ...]) -> bool:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return True
    return any(
        fnmatch.fnmatch(relative, pattern) or Path(relative).match(pattern) for pattern in patterns
    )


def _validate_symlink(path: Path, root: Path) -> None:
    """Refuse the workspace when the link at `path` points out of `root`.

    The target is resolved without requiring it to exist. A link whose target
    lies inside the root is left to the caller, dangling or not: the walk and
    the requirements closure skip it, and a read refuses it. Emacs keeps a
    dangling `.#name.py` link beside every buffer with unsaved edits. A link
    whose target lies outside the root raises, dangling or not.
    `os.path.realpath` resolves a link loop without raising on every supported
    version, where `Path.resolve` raises on 3.11 and 3.12.
    """

    try:
        Path(os.path.realpath(path)).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"workspace symlink escapes the root: {path!s}") from exc


class WorkspaceDriver(Protocol):
    root: str
    _ignored_dir_names: frozenset[str]
    _exclude_globs: tuple[str, ...]

    def refresh_paths(self, paths: Sequence[str | os.PathLike[str]]) -> tuple[str, ...]: ...


class WorkspaceMirror:
    """Filesystem mirror operations used under the session lock."""

    def __init__(
        self,
        root: str,
        mirror_root: str,
        ignored_dir_names: frozenset[str],
        exclude_globs: tuple[str, ...],
        *,
        max_files: int | None = None,
    ) -> None:
        self.root_path = Path(root).resolve(strict=False)
        self.root = str(self.root_path)
        self.mirror_root_path = Path(mirror_root).resolve(strict=False)
        self.ignored_dir_names = ignored_dir_names
        self.exclude_globs = exclude_globs
        self.max_files = max_files
        self._referenced_paths: set[Path] = set()
        self._content_hashes: dict[str, str] = {}

    def copy_workspace(self) -> None:
        files, referenced = _workspace_files(
            self.root, self.ignored_dir_names, self.exclude_globs, self.max_files
        )
        hashes: dict[str, str] = {}
        for source_path in files:
            relative = source_path.relative_to(self.root_path)
            target_path = self.mirror_root_path / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                content = _read_workspace_file(source_path, self.root_path)
            except _MISSING_PATH_ERRORS:
                continue
            target_path.write_bytes(content)
            hashes[str(source_path)] = hashlib.sha256(content).hexdigest()
        self._referenced_paths = referenced
        self._content_hashes = hashes

    def content_hashes(self) -> dict[str, str]:
        """Sha256, per real path, of the disk content each mirrored file holds.

        This is the disk state the mirror was last synced from, which can differ
        from the disk state right now. A poller seeded from it detects edits
        that landed after the copy but before the poller existed."""
        return dict(self._content_hashes)

    def normalize_real_path(self, path: str | os.PathLike[str]) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root_path / candidate
        normalized = candidate.resolve(strict=False)
        try:
            normalized.relative_to(self.root_path)
        except ValueError as exc:
            raise ValueError(
                f"{normalized!s} is outside the workspace root {self.root!r}."
            ) from exc
        return str(normalized)

    def mirror_path_for_real(self, real_path: str) -> Path:
        return self.mirror_root_path / Path(real_path).relative_to(self.root_path)

    def write_overlay(self, real_path: str, content: bytes) -> None:
        """Put an editor buffer in the mirror in place of the file on disk."""

        mirror_path = self.mirror_path_for_real(real_path)
        self._materialize_mirror_directory(mirror_path.parent)
        if mirror_path.is_dir():
            shutil.rmtree(mirror_path)
        mirror_path.write_bytes(content)

    def sync_path_from_disk(self, real_path: str) -> None:
        source_path = Path(real_path)
        referenced = _requirements_reference_paths(
            self.root_path, self.ignored_dir_names, self.exclude_globs
        )
        allowed = _workspace_path_allowed(
            source_path,
            self.root_path,
            self.ignored_dir_names,
            self.exclude_globs,
        )
        relevant = allowed and (
            _is_relevant_file(source_path, self.root_path) or source_path in referenced
        )
        self._sync_one_path(source_path, relevant=relevant)

        for added in referenced - self._referenced_paths:
            self._sync_one_path(added, relevant=True)
        for removed in self._referenced_paths - referenced:
            if not _is_relevant_file(removed, self.root_path):
                self._sync_one_path(removed, relevant=False)
        self._referenced_paths = referenced

    def _sync_one_path(self, source_path: Path, *, relevant: bool) -> None:
        mirror_path = self.mirror_path_for_real(str(source_path))
        key = str(source_path)
        if source_path.exists() and not relevant:
            self._content_hashes.pop(key, None)
            if mirror_path.exists() and mirror_path.is_file():
                mirror_path.unlink()
                self._prune_empty_parents(mirror_path.parent)
            return
        if relevant:
            try:
                content = _read_workspace_file(source_path, self.root_path)
            except _MISSING_PATH_ERRORS:
                pass
            except IsADirectoryError:
                self._content_hashes.pop(key, None)
                self._materialize_mirror_directory(mirror_path)
                return
            else:
                self._materialize_mirror_directory(mirror_path.parent)
                if mirror_path.is_dir():
                    shutil.rmtree(mirror_path)
                mirror_path.write_bytes(content)
                self._content_hashes[key] = hashlib.sha256(content).hexdigest()
                return

        self._content_hashes.pop(key, None)
        if mirror_path.is_dir():
            shutil.rmtree(mirror_path)
        elif mirror_path.exists():
            mirror_path.unlink()
        self._prune_empty_parents(mirror_path.parent)

    def _materialize_mirror_directory(self, directory: Path) -> None:
        """Make `directory` exist in the mirror as a directory.

        A workspace path can swap kind, such as a tracked file replaced by a
        same-named directory. The mirror then holds the other kind, and every
        entry between the mirror root and `directory` has to give way before
        `directory` can be created.
        """

        current = self.mirror_root_path
        for part in directory.relative_to(self.mirror_root_path).parts:
            current = current / part
            if current.exists() and not current.is_dir():
                current.unlink()
        directory.mkdir(parents=True, exist_ok=True)

    def _prune_empty_parents(self, directory: Path) -> None:
        while directory != self.mirror_root_path:
            # A parent the mirror now holds as a file belongs to the swap that
            # replaced it, so only directories are candidates for pruning.
            if not directory.is_dir():
                directory = directory.parent
                continue
            try:
                next(directory.iterdir())
                return
            except StopIteration:
                directory.rmdir()
                directory = directory.parent


class PollingWorkspaceWatcher:
    def __init__(
        self,
        session: WorkspaceDriver,
        *,
        debounce_ms: int = 200,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session = session
        self._debounce_seconds = debounce_ms / 1000.0
        self._clock = clock or time.monotonic
        # Polls reuse a file's hash while its size and modification time hold.
        # The baseline does not fill it, so the first poll reads every file
        # and finds any edit made since the baseline was taken.
        self._stat_cache: dict[str, tuple[int, int, str]] = {}
        self._snapshot = self._baseline_snapshot()
        self._pending: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        # Held by `start`, by `stop` and `_request_stop` while they find the
        # event and thread to act on, and by `poll` for the whole poll. So none
        # of them interleaves with another. It is taken before the session's
        # lock (`start` registers, `poll` refreshes) and never after it. That
        # is why `_runs_in_current_thread` does without it.
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._on_change: Callable[[tuple[str, ...]], None] | None = None
        self._on_error: Callable[[Exception], None] | None = None

    def _baseline_snapshot(self) -> dict[str, str]:
        """The disk state to diff the first poll against.

        A session that mirrors the workspace reports the hashes its mirror was
        synced from, so anything edited between that sync and this watcher's
        construction still shows up as a difference. A driver without a mirror
        starts from the current disk state.
        """
        mirrored = getattr(self._session, "_mirrored_content_hashes", None)
        if mirrored is not None:
            baseline: dict[str, str] = dict(mirrored())
            return baseline
        return _collect_filesystem_snapshot(
            self._session.root,
            self._session._ignored_dir_names,
            self._session._exclude_globs,
        )

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def poll(self) -> tuple[str, ...]:
        # Check and poll under one hold of the lifecycle lock. With only the
        # check under it, a `start()` landing after the check ran the thread's
        # first poll alongside this one, over the same pending map and snapshot.
        with self._lifecycle_lock:
            if self.is_running:
                raise RuntimeError(
                    "PollingWorkspaceWatcher is running; stop() it before calling poll() directly."
                )
            return self._poll_once()

    def _poll_once(self) -> tuple[str, ...]:
        now = self._clock()
        current_snapshot = _collect_filesystem_snapshot(
            self._session.root,
            self._session._ignored_dir_names,
            self._session._exclude_globs,
            self._stat_cache,
        )

        changed_paths = {
            path
            for path in set(self._snapshot) | set(current_snapshot)
            if self._snapshot.get(path) != current_snapshot.get(path)
        }
        for path in changed_paths:
            self._pending[path] = now

        ready = tuple(
            sorted(
                path
                for path, seen_at in self._pending.items()
                if now - seen_at >= self._debounce_seconds
            )
        )
        in_flight = {path: self._pending.pop(path) for path in ready}
        self._snapshot = current_snapshot
        if ready:
            try:
                ready = self._paths_the_mirror_lacks(ready, current_snapshot)
                if ready:
                    self._session.refresh_paths(list(ready))
            except Exception:
                # The snapshot already matches disk, so nothing would re-detect
                # these paths. Requeue them (original timestamps, already past
                # the debounce) so the next tick retries the refresh.
                self._pending.update(in_flight)
                raise
        return ready

    def _paths_the_mirror_lacks(
        self, paths: tuple[str, ...], snapshot: dict[str, str]
    ) -> tuple[str, ...]:
        """The paths whose disk content differs from what the mirror holds.

        A save clears the overlay, which syncs the file from disk before this
        poll sees its new hash. Refreshing it again syncs nothing and runs a
        second analysis for the same save. So a path whose disk hash equals
        the hash the mirror was synced from is dropped. A deleted path the
        mirror no longer holds is dropped too. A driver without a mirror gets
        every path.
        """

        mirrored = getattr(self._session, "_mirrored_content_hashes", None)
        if mirrored is None:
            return paths
        hashes: dict[str, str] = mirrored()
        return tuple(path for path in paths if hashes.get(path) != snapshot.get(path))

    def start(
        self,
        on_change: Callable[[tuple[str, ...]], None],
        *,
        interval_s: float | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("PollingWorkspaceWatcher is already running.")
            effective_interval = interval_s if interval_s is not None else _DEFAULT_POLL_INTERVAL_S
            self._register_with_session()
            self._on_change = on_change
            self._on_error = on_error
            self._stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(effective_interval,),
                name="pyinc-tools-watcher",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                self._on_change = None
                self._on_error = None
                self._unregister_from_session()
                raise

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            self._request_stop()
            thread = self._thread
            if thread is None:
                return
            if thread is threading.current_thread():
                return
        thread.join(timeout)
        if thread.is_alive():
            print(
                "pyinc-tools watcher: thread did not stop within timeout",
                file=sys.stderr,
            )

    def _request_stop(self) -> None:
        # `start()` replaces the event under the lifecycle lock. Set without
        # it, a stop landing while `start()` ran could set the event being
        # replaced. The new thread then ran on, `stop()` timed out joining it,
        # and `WorkspaceSession.close()` removed the mirror under it.
        with self._lifecycle_lock:
            self._stop_event.set()

    def _runs_in_current_thread(self) -> bool:
        # No lifecycle lock here. `WorkspaceSession.close()` asks this while
        # holding the session's lock, which `start()` takes inside the
        # lifecycle lock. A reference read is atomic, and no lock is needed.
        # `_thread` changes only in `start()`, which refuses while the previous
        # thread is alive. So it cannot change under the one thread for which
        # the answer is True.
        return self._thread is threading.current_thread()

    def _register_with_session(self) -> None:
        register = getattr(self._session, "_register_watcher", None)
        if register is not None:
            register(self)

    def _unregister_from_session(self) -> None:
        unregister = getattr(self._session, "_unregister_watcher", None)
        if unregister is not None:
            unregister(self)

    def _finish_current_thread(self) -> None:
        # `_thread` keeps pointing at this thread. The thread is still running
        # here and until it returns, and `stop()` has to be able to join it.
        # With the reference cleared, a stop arriving in that window found
        # nothing to join and returned while the thread was alive. So
        # `WorkspaceSession.close` removed the mirror under it. `is_running`
        # asks the thread itself.
        with self._lifecycle_lock:
            if self._thread is not threading.current_thread():
                return
            self._on_change = None
            self._on_error = None
        self._unregister_from_session()

    def __enter__(self) -> PollingWorkspaceWatcher:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.stop()

    def _run(self, interval_s: float) -> None:
        # This thread reads `_stop_event`, `_on_change` and `_on_error` without
        # the lifecycle lock, and needs no lock for them. `start()` set them
        # before starting this thread and cannot set them again while it is
        # alive. Only this thread clears the callbacks, after its loop.
        try:
            while not self._stop_event.is_set():
                try:
                    ready = self._poll_once()
                except RuntimeError as exc:
                    if self._session_closed(exc):
                        # The session was closed under us. Exit cleanly.
                        return
                    self._handle_error(exc)
                    ready = ()
                except Exception as exc:  # pragma: no cover - defensive
                    self._handle_error(exc)
                    ready = ()
                if self._stop_event.is_set():
                    return
                if ready:
                    callback = self._on_change
                    if callback is not None:
                        try:
                            callback(ready)
                        except Exception as exc:
                            self._handle_error(exc)
                if self._stop_event.wait(interval_s):
                    return
        finally:
            self._finish_current_thread()

    def _session_closed(self, exc: RuntimeError) -> bool:
        """Whether `exc` is the session reporting that it closed.

        Only that contract may retire the watcher thread. RuntimeError also
        covers RecursionError and any bug in the poll path. Those have to be
        reported as errors, and never taken for a shutdown.
        """

        closed = getattr(self._session, "_closed", None)
        if closed is not None:
            return bool(closed)
        return str(exc) == SESSION_CLOSED_MESSAGE

    def _handle_error(self, exc: Exception) -> None:
        if self._on_error is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                self._on_error(exc)
            return
        print(
            f"pyinc-tools watcher: callback raised: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


PollingWorkspaceWatcher.__module__ = "pyinc_tools.session"

__all__ = [
    "DEFAULT_IGNORED_DIR_NAMES",
    "DEFAULT_MAX_WORKSPACE_FILES",
    "SESSION_CLOSED_MESSAGE",
    "PollingWorkspaceWatcher",
    "WorkspaceMirror",
    "WorkspaceTooLargeError",
]
