"""Fault-injection harness for the action reconcile suites.

The reconcile path touches the filesystem at a fixed set of seams. These
helpers arm exactly one fault at one seam -- keyed on the path the seam
receives, never on call order -- and provide the two assertion shapes the
action contract distinguishes:

- a preflight refusal leaves the output tree AND the ledger byte-identical,
  and ``plan()`` refuses identically;
- a mutation-phase fault leaves each completed step in place (the set is
  deliberately not transactional), leaves no temporary file behind, leaves
  the ledger unchanged-or-old, and the next locked run converges.

The first ``read_regular_file`` of every reconcile is the action manifest,
so hooks gate on names, never on call counts; manifest names carry the
stable ``.pyinc-action.`` prefix, which separates ledger traffic from output
traffic at the same seam. Hooks accept ``**kwargs`` and forward them so a
keyword added to a primitive (``expected_identity`` today) flows through.
"""

from __future__ import annotations

import errno
import importlib
import json
import os
import socket
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from _action_witness import TreeWitness, manifest_bytes, tree_witness

from pyinc import Database, InMemoryArtifactStore, Input, query
from pyinc.action import Action, Output, action
from pyinc.errors import ActionPathError

action_module = importlib.import_module("pyinc.action")

#: What a fault may escape ``reconcile()`` as, today and after the error
#: surface is retyped: raw escapes are OSError subclasses (including
#: UnsafeFilesystemPathError), typed refusals are ValueError-based action
#: errors. A cell that pins an escape it does not own asserts this union
#: plus the phase's safety invariants, never a bare type.
RAW_OR_TYPED: tuple[type[BaseException], ...] = (OSError, ActionPathError)

#: The injected OSError families. CPython maps a multi-argument OSError to
#: its errno-keyed subclass (EACCES/EPERM -> PermissionError, ENOTDIR ->
#: NotADirectoryError, EINTR -> InterruptedError; ENOSPC/EIO/ELOOP stay
#: OSError), so an injected fault carries exactly the type a real syscall
#: failure would. Real EINTR is retried inside os.* by the interpreter and
#: is unobservable at these seams; an injected EINTR behaves as any other
#: OSError, which is exactly what its cells document.
FAULT_FAMILIES: tuple[int, ...] = (
    errno.EACCES,
    errno.EPERM,
    errno.ENOSPC,
    errno.EIO,
    errno.EINTR,
    errno.ENOTDIR,
    errno.ELOOP,
)

#: Every action ledger filename starts with this prefix.
MANIFEST_PREFIX = ".pyinc-action."


def fault(code: int, path: object) -> OSError:
    """The injected error for one fault family, carrying its real subclass."""
    return OSError(code, os.strerror(code), str(path))


def manifest_gate(path: Path) -> bool:
    """Match ledger traffic at a shared seam."""
    return path.name.startswith(MANIFEST_PREFIX)


def named_gate(name: str) -> Callable[[Path], bool]:
    """Match exactly one basename at a shared seam."""

    def gate(path: Path) -> bool:
        return path.name == name

    return gate


def inject_fault(
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    code: int,
    *,
    gate: Callable[[Path], bool],
) -> Callable[[], None]:
    """Arm one fault at a ``pyinc.action`` module attribute; returns disarm.

    Patching the action module's own binding leaves ``pyinc._safe_fs``
    untouched, so the witness helpers and the convergence runs read the
    real tree. (Patching the identity read at the ``_safe_fs`` level would
    also change ``read_regular_file``'s POSIX branch -- including the
    manifest read -- which is why this harness never does that.)
    """
    original = getattr(action_module, seam)
    armed = [True]

    def hook(path: Path, *args: object, **kwargs: object) -> object:
        if armed[0] and gate(path):
            raise fault(code, path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(action_module, seam, hook)

    def disarm() -> None:
        armed[0] = False

    return disarm


def inject_path_method_fault(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    code: int,
    *,
    gate: Callable[[Path], bool],
) -> Callable[[], None]:
    """Arm one fault at a ``pathlib.Path`` method, gated on the receiver.

    The patch is class-wide, so the gate must name a basename unique to the
    fixture, and tree witnesses are taken only while the fault is disarmed
    (the witness walk itself calls ``lstat``).
    """
    original = getattr(Path, method)
    armed = [True]

    def hook(self: Path, *args: object, **kwargs: object) -> object:
        if armed[0] and gate(self):
            raise fault(code, self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, hook)

    def disarm() -> None:
        armed[0] = False

    return disarm


def inject_lock_acquire_fault(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> Callable[[], None]:
    """Arm one fault at ``FileLock.acquire`` for action lock files."""
    lock_class = action_module.FileLock
    original = lock_class.acquire
    armed = [True]

    def hook(self: Any) -> None:
        if armed[0] and self.path.name.endswith(".lock"):
            raise fault(code, self.path)
        original(self)

    monkeypatch.setattr(lock_class, "acquire", hook)

    def disarm() -> None:
        armed[0] = False

    return disarm


def desired_spec(desired: Mapping[str, str]) -> str:
    """Encode a desired layout for an input-driven action."""
    return json.dumps(sorted(desired.items()))


def input_driven_action(tool: str) -> tuple[Action, Input[str]]:
    """An action whose desired set flows through the database.

    The desired layout is read from an input through a query, so a
    checkpoint of the database round-trips the provenance a refusal was
    computed from -- the ledger itself is re-read from disk on every
    reconcile regardless. Tool identities must be unique per cell; the
    input key embeds the tool name.
    """
    source = Input[str](f"fault-harness.{tool}.desired")

    @query
    def rendered(db: Database) -> tuple[tuple[str, str], ...]:
        entries = json.loads(source.read(db))
        return tuple((str(path), str(text)) for path, text in entries)

    @action(tool=tool)
    def emit(db: Database) -> list[Output]:
        return [Output.text(path, text) for path, text in rendered(db)]

    return emit, source


def assert_refusal_replays_after_checkpoint(
    source: Input[str],
    spec: str,
    store: InMemoryArtifactStore,
    warm: Database,
    refuse: Callable[[Database], str],
) -> None:
    """The same refusal, warm and after a checkpoint rebuild, identical text.

    Actions publish durable state, so a refusal must not depend on warm
    in-memory state: the desired set is re-derived through a database
    rebuilt from the warm one's checkpoint and the refusal text must not
    change. ``refuse`` asserts the per-cell witnesses on each run and
    returns the exception text it observed. The warm database must be
    ``Database("strict", store=store)`` -- the rebuilt one matches it.
    """
    warm_text = refuse(warm)
    key = warm.save_checkpoint()
    reloaded = Database("strict", store=store)
    reloaded.set(source, spec)
    reloaded.load_checkpoint(key)
    reloaded_text = refuse(reloaded)
    assert reloaded_text == warm_text, (
        "the refusal changed across the checkpoint: "
        f"warm={warm_text!r} reloaded={reloaded_text!r}"
    )


def assert_tree_and_ledger_unchanged(
    root: Path,
    state_dir: Path,
    tool: str,
    before_tree: TreeWitness,
    before_ledger: bytes | None,
) -> None:
    """The preflight-refusal invariant: nothing moved, on disk or in the ledger."""
    after_tree = tree_witness(root)
    differing = sorted(
        path
        for path in before_tree.keys() | after_tree.keys()
        if before_tree.get(path) != after_tree.get(path)
    )
    assert after_tree == before_tree, f"the tree changed at {differing}"
    after_ledger = manifest_bytes(state_dir, tool)
    before_size = "absent" if before_ledger is None else f"{len(before_ledger)} bytes"
    after_size = "absent" if after_ledger is None else f"{len(after_ledger)} bytes"
    assert after_ledger == before_ledger, (
        f"the ledger changed: before={before_size} after={after_size}"
    )


def assert_no_tmp_residue(*directories: Path) -> None:
    """No atomic-write temporary survives a fault under the given trees."""
    for directory in directories:
        if directory.is_dir():
            leftovers = sorted(str(path) for path in directory.rglob(".tmp-*"))
            assert leftovers == [], f"temporary files survived: {leftovers}"


def assert_mutation_fault_invariants(
    root: Path, state_dir: Path, tool: str, before_ledger: bytes | None
) -> None:
    """The mutation-phase invariant: ledger unchanged-or-old, no torn file.

    Mutation faults fire before the ledger write, so the ledger equals its
    pre-call bytes. The tree is deliberately NOT asserted byte-identical
    here: each completed step stays completed by design, and the caller
    pins the exact steps its fixture performed.
    """
    after_ledger = manifest_bytes(state_dir, tool)
    before_size = "absent" if before_ledger is None else f"{len(before_ledger)} bytes"
    after_size = "absent" if after_ledger is None else f"{len(after_ledger)} bytes"
    assert after_ledger == before_ledger, (
        "the ledger changed under a mutation fault: "
        f"before={before_size} after={after_size}"
    )
    assert_no_tmp_residue(root, state_dir)


def make_nonregular_node(path: Path, kind: str) -> None:
    """Create a FIFO, unix socket, or device node; skip where impossible.

    Sockets are bound through a relative name to dodge the AF_UNIX path
    length cap; device nodes need mknod privilege, so their cells skip on
    ordinary accounts -- the skip reason records the capability gap.
    """
    if kind == "fifo":
        make_fifo = getattr(os, "mkfifo", None)
        if make_fifo is None:
            pytest.skip("os.mkfifo is unavailable on this platform")
        make_fifo(path)
        return
    if kind == "socket":
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("AF_UNIX sockets are unavailable on this platform")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.getcwd()
        try:
            os.chdir(path.parent)
            server.bind(path.name)
        finally:
            server.close()
            os.chdir(previous)
        return
    node_type = {"char-device": stat.S_IFCHR, "block-device": stat.S_IFBLK}[kind]
    make_node = getattr(os, "mknod", None)
    if make_node is None:
        pytest.skip("os.mknod is unavailable on this platform")
    try:
        make_node(path, mode=node_type | 0o600, device=os.makedev(1, 3))
    except OSError as error:
        pytest.skip(f"cannot create a {kind} node without privilege: {error}")
