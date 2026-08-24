"""Hostile filesystem shapes and a bounded-time runner for the suites.

A test that hands the library a FIFO must fail loudly rather than hang the
run, so the call under test runs in a forked child with a hard budget and a
child that outlives it is reported as a block. Forking is the mechanism
because it needs no import of the tree under test in a subprocess and no
temporary module: the child inherits the parent's imports exactly.

Every shape here is POSIX-only to build, so the factory skips rather than
fails where the platform has no such thing. Permission-shaped fixtures also
skip for a root euid, where a mode of 0o000 denies nothing.
"""

from __future__ import annotations

import os
import signal
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

#: A call that has not returned by now is not going to.
BUDGET_SECONDS = 5.0

#: Marks a cell whose fixture only exists on POSIX.
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX-only filesystem shape")


def skip_without_posix_permissions() -> None:
    """Skip a cell whose fixture depends on a mode denying something."""
    if os.name == "nt":
        pytest.skip("POSIX permission semantics are unavailable on this platform")
    if getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("a mode of 0o000 denies nothing to a root euid")


def make_fifo(path: Path) -> Path:
    make = getattr(os, "mkfifo", None)
    if make is None:
        pytest.skip("os.mkfifo is unavailable on this platform")
    make(path)
    return path


def make_socket(path: Path) -> tuple[Path, socket.socket]:
    """Bind a listening unix socket at ``path``; the caller closes it.

    Bound through a relative name after chdir because ``sun_path`` is
    length-capped and a pytest tmp_path can exceed the cap.
    """
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX sockets are unavailable on this platform")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.getcwd()
    os.chdir(path.parent)
    try:
        server.bind(path.name)
    finally:
        os.chdir(previous)
    return path, server


def character_device() -> str:
    """A device whose read never ends, or a skip where there is none."""
    if os.name == "nt" or not os.path.exists("/dev/zero"):
        pytest.skip("no unending character device is available on this platform")
    return "/dev/zero"


def make_symlink_loop(path: Path) -> Path:
    """Create a two-link cycle at ``path``; returns ``path``."""
    partner = path.with_name(path.name + "-partner")
    try:
        os.symlink(partner, path)
        os.symlink(path, partner)
    except (NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")
    return path


def nul_path(base: Path) -> str:
    """A path string holding an embedded NUL."""
    return str(base / "a\0b")


def within_budget(call: Callable[[], Any], *, budget: float = BUDGET_SECONDS) -> str:
    """Run ``call`` in a forked child and report how it ended.

    Returns "returned", "raised: <TypeName>", or "BLOCKED". The value itself
    is deliberately not returned: a cell that needs the value asserts it in
    the parent, on a shape that cannot block.
    """
    if not hasattr(os, "fork"):
        pytest.skip("os.fork is unavailable on this platform")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never reports coverage
        os.close(read_fd)
        try:
            call()
            outcome = "returned"
        except BaseException as error:  # noqa: BLE001 - the type IS the result
            outcome = f"raised: {type(error).__name__}"
        with os.fdopen(write_fd, "w") as handle:
            handle.write(outcome)
        os._exit(0)
    os.close(write_fd)
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        finished, _status = os.waitpid(pid, os.WNOHANG)
        if finished:
            with os.fdopen(read_fd) as handle:
                return handle.read()
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    os.close(read_fd)
    return "BLOCKED"
