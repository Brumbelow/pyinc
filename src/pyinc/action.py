"""Declared-output reconciliation — the ``@action`` layer.

Queries stay pure and derive *desired* artifacts; this layer reconciles a
desired output set against the filesystem. Side effects live only in
:meth:`Action.reconcile` (called at top level, never inside a query):

- atomic output replacement (temp file + ``os.replace``),
- content hashing to detect changes and out-of-band tampering,
- a per-tool ownership ledger (manifest) so stale outputs are deleted and no
  file the action did not write is ever touched,
- dry-run / plan mode.

An :class:`Output` is snapshot-safe (``path: str``, ``content: bytes``), so a
``tuple[Output, ...]`` may be returned directly from a ``@query``. See
``docs/action-contract.md`` for the full contract.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from .runtime import Database

_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class Output:
    """A single declared output artifact: a path *relative to the reconcile
    root* and the exact bytes to write there.

    Snapshot-safe (``str`` + ``bytes``), so a ``tuple[Output, ...]`` may be the
    return value of a ``@query`` and participate in kernel caching/backdating.
    """

    path: str
    content: bytes

    @classmethod
    def text(cls, path: str, text: str, *, encoding: str = "utf-8") -> Output:
        """Build an :class:`Output` from text, encoding it to bytes."""
        return cls(path=path, content=text.encode(encoding))


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of :meth:`Action.reconcile`. All paths are root-relative POSIX."""

    written: tuple[str, ...]
    deleted: tuple[str, ...]
    unchanged: tuple[str, ...]
    dry_run: bool


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_rel(path: str) -> str:
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"Output path must be relative, got: {path!r}")
    if ".." in pure.parts:
        raise ValueError(f"Output path must not escape the root: {path!r}")
    return pure.as_posix()


def _tool_slug(tool: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in tool)


def _manifest_path(state_dir: Path, tool: str) -> Path:
    return state_dir / f".pyinc-action.{_tool_slug(tool)}.json"


def _read_manifest(state_dir: Path, tool: str) -> dict[str, str]:
    try:
        raw = _manifest_path(state_dir, tool).read_bytes()
    except FileNotFoundError:
        return {}
    data = json.loads(raw)
    outputs = data.get("outputs", {})
    return {str(key): str(value) for key, value in outputs.items()}


def _write_manifest(state_dir: Path, tool: str, outputs: dict[str, str]) -> None:
    payload = {
        "tool": tool,
        "version": _MANIFEST_VERSION,
        "outputs": dict(sorted(outputs.items())),
    }
    _atomic_write(
        _manifest_path(state_dir, tool),
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
    )


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
        replaced = True
    finally:
        if not replaced:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)


class Action:
    """Reconciles a pure desired-output set against the filesystem.

    Produced by :func:`action`. Not a ``Query`` — call :meth:`reconcile` /
    :meth:`plan` at top level (never inside a query). The wrapped function is
    pure: it returns the desired ``Output`` set (typically by calling queries).
    """

    def __init__(self, fn: Callable[..., Iterable[Output]], *, tool: str) -> None:
        if not tool:
            raise ValueError("@action requires a non-empty tool identity.")
        self.fn = fn
        self.tool = tool
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__
        self.__module__ = fn.__module__
        self.__wrapped__ = fn

    def outputs(self, db: Database, *args: object, **kwargs: object) -> tuple[Output, ...]:
        """Compute the desired output set (pure; forwards to the wrapped fn)."""
        return tuple(self.fn(db, *args, **kwargs))

    def reconcile(
        self,
        db: Database,
        *args: object,
        root: str | os.PathLike[str],
        dry_run: bool = False,
        state_dir: str | os.PathLike[str] | None = None,
        **kwargs: object,
    ) -> ReconcileResult:
        """Reconcile the desired output set against ``root``.

        Writes only outputs whose on-disk bytes differ from the desired bytes
        (this also repairs out-of-band edits), deletes outputs this action
        previously owned but no longer declares, and updates the ownership
        manifest. With ``dry_run=True`` nothing is written and the planned
        ``written`` / ``deleted`` / ``unchanged`` sets are reported.
        """
        root_path = Path(root)
        state_path = Path(state_dir) if state_dir is not None else root_path

        desired_map: dict[str, bytes] = {}
        for output in self.outputs(db, *args, **kwargs):
            rel = _normalize_rel(output.path)
            if rel in desired_map:
                raise ValueError(f"Duplicate output path from action {self.tool!r}: {rel!r}")
            desired_map[rel] = output.content

        previous = _read_manifest(state_path, self.tool)

        written: list[str] = []
        unchanged: list[str] = []
        for rel in sorted(desired_map):
            content = desired_map[rel]
            target = root_path / rel
            try:
                current: bytes | None = target.read_bytes()
            except (FileNotFoundError, IsADirectoryError):
                current = None
            if current is not None and _content_hash(current) == _content_hash(content):
                unchanged.append(rel)
                continue
            if not dry_run:
                _atomic_write(target, content)
            written.append(rel)

        deleted: list[str] = []
        for rel in sorted(set(previous) - set(desired_map)):
            target = root_path / rel
            if target.exists():
                if not dry_run:
                    target.unlink()
                deleted.append(rel)

        new_manifest = {rel: _content_hash(content) for rel, content in desired_map.items()}
        if not dry_run and new_manifest != previous:
            _write_manifest(state_path, self.tool, new_manifest)

        return ReconcileResult(
            written=tuple(written),
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
        **kwargs: object,
    ) -> ReconcileResult:
        """Dry-run :meth:`reconcile`: report planned changes, write nothing."""
        return self.reconcile(db, *args, root=root, dry_run=True, state_dir=state_dir, **kwargs)


@overload
def action(fn: Callable[..., Iterable[Output]], *, tool: str) -> Action: ...
@overload
def action(
    fn: None = None, *, tool: str
) -> Callable[[Callable[..., Iterable[Output]]], Action]: ...
def action(
    fn: Callable[..., Iterable[Output]] | None = None, *, tool: str
) -> Action | Callable[[Callable[..., Iterable[Output]]], Action]:
    """Decorate a ``(db, *args) -> Iterable[Output]`` function as an :class:`Action`.

    ``tool`` is the stable ownership identity recorded in the manifest; two
    distinct tools writing the same root keep separate ledgers and never delete
    each other's files.
    """

    def decorate(wrapped: Callable[..., Iterable[Output]]) -> Action:
        return Action(wrapped, tool=tool)

    if fn is None:
        return decorate
    return decorate(fn)
