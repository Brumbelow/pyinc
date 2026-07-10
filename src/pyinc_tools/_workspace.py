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
    }
)

_RELEVANT_SUFFIXES = frozenset(
    {".py", ".pyi", ".toml", ".json", ".xml", ".csv", ".ipynb", ".txt", ".cfg", ".ini"}
)
_RELEVANT_NAMES = frozenset({".env", "Pipfile", "pyproject.toml"})
_REQUIREMENTS_REFERENCE = re.compile(r"^(?:-r|--requirement|-c|--constraint)\s+(.+)$")


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
    return not _is_excluded(path, root, exclude_globs)


def _is_workspace_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except FileNotFoundError:
        return False
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_point)


def _reject_symlink_components(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if not _is_workspace_link(current):
            continue
        _validate_symlink(current, root)
        raise ValueError(f"workspace symlinks are not supported: {current!s}")


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
    except FileNotFoundError:
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
    except FileNotFoundError:
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
        _reject_symlink_components(current, root)
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
            _reject_symlink_components(target, root)
            try:
                metadata = target.lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if _is_workspace_link(target):
                _validate_symlink(target, root)
                raise ValueError(f"workspace symlinks are not supported: {target!s}")
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
) -> tuple[set[Path], set[Path]]:
    root_path = Path(root).resolve(strict=False)
    files: set[Path] = set()
    for current_root, dirnames, filenames in os.walk(root_path):
        current_path = Path(current_root)
        kept_dirs: list[str] = []
        for name in dirnames:
            path = current_path / name
            if name in ignored_dir_names or _is_excluded(path, root_path, exclude_globs):
                continue
            if _is_workspace_link(path):
                _validate_symlink(path, root_path)
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for filename in filenames:
            source_path = current_path / filename
            if not _is_relevant_file(source_path) or _is_excluded(
                source_path, root_path, exclude_globs
            ):
                continue
            if _is_workspace_link(source_path):
                _validate_symlink(source_path, root_path)
                raise ValueError(f"workspace file symlinks are not supported: {source_path!s}")
            files.add(source_path)
    referenced = _requirements_reference_paths(root_path, ignored_dir_names, exclude_globs)
    files.update(path for path in referenced if path.is_file())
    return files, referenced


def _collect_filesystem_snapshot(
    root: str,
    ignored_dir_names: frozenset[str],
    exclude_globs: tuple[str, ...] = (),
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    files, _referenced = _workspace_files(root, ignored_dir_names, exclude_globs)
    for source_path in files:
        try:
            content = _read_workspace_file(source_path, Path(root).resolve(strict=False))
        except FileNotFoundError:
            continue
        snapshot[str(source_path)] = hashlib.sha256(content).hexdigest()
    return snapshot


def _is_relevant_file(path: Path) -> bool:
    return path.name in _RELEVANT_NAMES or path.suffix.lower() in _RELEVANT_SUFFIXES


def _is_excluded(path: Path, root: Path, patterns: tuple[str, ...]) -> bool:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return True
    return any(
        fnmatch.fnmatch(relative, pattern) or Path(relative).match(pattern) for pattern in patterns
    )


def _validate_symlink(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
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
    ) -> None:
        self.root = root
        self.root_path = Path(root)
        self.mirror_root_path = Path(mirror_root)
        self.ignored_dir_names = ignored_dir_names
        self.exclude_globs = exclude_globs
        self._referenced_paths: set[Path] = set()

    def copy_workspace(self) -> None:
        files, referenced = _workspace_files(self.root, self.ignored_dir_names, self.exclude_globs)
        for source_path in files:
            relative = source_path.relative_to(self.root_path)
            target_path = self.mirror_root_path / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                content = _read_workspace_file(source_path, self.root_path)
            except FileNotFoundError:
                continue
            target_path.write_bytes(content)
        self._referenced_paths = referenced

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
        relevant = allowed and (_is_relevant_file(source_path) or source_path in referenced)
        self._sync_one_path(source_path, relevant=relevant)

        for added in referenced - self._referenced_paths:
            self._sync_one_path(added, relevant=True)
        for removed in self._referenced_paths - referenced:
            if not _is_relevant_file(removed):
                self._sync_one_path(removed, relevant=False)
        self._referenced_paths = referenced

    def _sync_one_path(self, source_path: Path, *, relevant: bool) -> None:
        mirror_path = self.mirror_path_for_real(str(source_path))
        if source_path.exists() and not relevant:
            if mirror_path.exists() and mirror_path.is_file():
                mirror_path.unlink()
                self._prune_empty_parents(mirror_path.parent)
            return
        if relevant:
            try:
                content = _read_workspace_file(source_path, self.root_path)
            except FileNotFoundError:
                pass
            except IsADirectoryError:
                mirror_path.mkdir(parents=True, exist_ok=True)
                return
            else:
                mirror_path.parent.mkdir(parents=True, exist_ok=True)
                mirror_path.write_bytes(content)
                return

        if mirror_path.is_dir():
            shutil.rmtree(mirror_path)
        elif mirror_path.exists():
            mirror_path.unlink()
        self._prune_empty_parents(mirror_path.parent)

    def _prune_empty_parents(self, directory: Path) -> None:
        while directory != self.mirror_root_path:
            if not directory.exists():
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
        self._snapshot = _collect_filesystem_snapshot(
            self._session.root,
            self._session._ignored_dir_names,
            self._session._exclude_globs,
        )
        self._pending: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._on_change: Callable[[tuple[str, ...]], None] | None = None
        self._on_error: Callable[[Exception], None] | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def poll(self) -> tuple[str, ...]:
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
        for path in ready:
            self._pending.pop(path, None)
        if ready:
            self._session.refresh_paths(list(ready))

        self._snapshot = current_snapshot
        return ready

    def start(
        self,
        on_change: Callable[[tuple[str, ...]], None],
        *,
        interval_s: float | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if self.is_running:
            raise RuntimeError("PollingWorkspaceWatcher is already running.")
        effective_interval = (
            interval_s if interval_s is not None else max(self._debounce_seconds / 2.0, 0.05)
        )
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
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout)
        if thread.is_alive():
            print(
                "pyinc-tools watcher: thread did not stop within timeout",
                file=sys.stderr,
            )
        self._thread = None
        self._on_change = None
        self._on_error = None

    def __enter__(self) -> PollingWorkspaceWatcher:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.stop()

    def _run(self, interval_s: float) -> None:
        while not self._stop_event.is_set():
            try:
                ready = self._poll_once()
            except RuntimeError:
                # Session was closed out from under us; exit cleanly.
                return
            except Exception as exc:  # pragma: no cover - defensive
                self._handle_error(exc)
                ready = ()
            if ready:
                callback = self._on_change
                if callback is not None:
                    try:
                        callback(ready)
                    except Exception as exc:
                        self._handle_error(exc)
            if self._stop_event.wait(interval_s):
                return

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
    "PollingWorkspaceWatcher",
    "WorkspaceMirror",
]
