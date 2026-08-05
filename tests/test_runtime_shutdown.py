from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_state_identity_weakref_cleanup_survives_module_teardown() -> None:
    repository = Path(__file__).resolve().parents[1]
    source = """
import gc

import pyinc.runtime as runtime
from pyinc import Database


class Owner:
    pass


owner = Owner()
owner_id = id(owner)
Database._state_identity_owner(owner)
registry = runtime._STATE_IDENTITY_OWNERS
assert owner_id in registry

# Simulate the arbitrary module-global clearing order used during interpreter
# shutdown before releasing the weakly referenced configuration owner.
runtime._DEFINITION_IDENTITY_LOCK = None
runtime._STATE_IDENTITY_OWNERS = None
del owner
gc.collect()

assert owner_id not in registry
"""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=repository,
        env={**os.environ, "PYTHONPATH": str(repository / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Exception ignored in:" not in completed.stderr
