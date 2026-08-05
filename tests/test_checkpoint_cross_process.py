"""Cross-process checkpoint round-trips.

The durable-cache contract only means something across a *real* process
boundary: fresh ``id()``s, explicitly different hash seeds, and fresh module
state. ``runpy`` (as used in ``test_examples.py``) reuses this interpreter, so
it cannot exercise D3 (deterministic identities across processes). These tests
spawn genuine subprocesses via ``sys.executable``.

The idiom (reused by every test below, and the template for future
cross-process tests):

* A single parameterized *fixture script* is written into ``tmp_path`` and
  runs in two phases -- ``save`` then ``load`` -- selected by argv. It defines
  every ``Input``/``Query``/``Resource`` at MODULE level so identities are
  reproducible (the D3 contract). ``save`` builds state, checkpoints, and
  persists the key to a file in the store dir; ``load`` reloads in a brand-new
  process, gets the root, and prints the decision plus value as JSON.
* The store is an on-disk ``FileSystemArtifactStore`` shared by both phases.
* ``PYTHONPATH`` is set *explicitly* (``src`` plus ``tmp_path``) so the run
  depends on nothing but the source tree -- no reliance on the repo layout, the
  installed package, or the test's own ``sys.path``.

Each row of the matrix is its own test. "reused vs executed" is observed with
``inspect(root).last_recompute`` -- the same public field the in-process
checkpoint tests assert.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import pyinc

# The base state saved by phase "save": alpha=1, beta=2, a 5-byte file
# ("hello"), and the helper module's WEIGHT=10 -> 1 + 2 + 5 + 10 == 18.
BASE_RESULT = 18
SAVE_HASH_SEED = "1"
LOAD_HASH_SEED = "4294967295"

FIXTURE_SCRIPT = '''\
"""Cross-process checkpoint fixture. Everything is defined at module level so
identities are reproducible across processes (D3)."""

import json
import sys
from pathlib import Path

from pyinc import Database, FileResource, FileSystemArtifactStore, Input, query

import mg_helpers  # written next to this script; captured by the root query

# Module-level, deterministic construction order -> reproducible Input seq.
alpha = Input[int]("cxp_alpha")
beta = Input[int]("cxp_beta")
files = FileResource()
identity_marker = tuple([1])

store_dir = sys.argv[1]
phase = sys.argv[2]
variant = sys.argv[3]

root_dir = Path(store_dir).parent
data_path = str(root_dir / "data.txt")
key_path = root_dir / "ckpt.key"

# Load-phase perturbation table: (alpha, beta, alternate_body).
LOAD_CONFIG = {
    "unchanged": (1, 2, False),
    "input_changed": (9, 2, False),
    "file_changed": (1, 2, False),
    "src_changed": (1, 2, True),
    "module_changed": (1, 2, False),
}


def build_root(alt_body):
    # Two bodies share the qualname (and thus query_id) but marshal differently,
    # so selecting `alt_body` moves the query's code identity while leaving every
    # other axis fixed -- isolating the "query source changed" case.
    if alt_body:

        @query
        def root(db):
            return (
                alpha.read(db) * 1000
                + beta.read(db)
                + mg_helpers.WEIGHT
            )

    else:

        @query
        def root(db):
            return (
                alpha.read(db)
                + beta.read(db)
                + mg_helpers.WEIGHT
            )

    return root


@query
def file_size(db, path):
    return len(files.read(db, path))


@query
def identity_sensitive(db):
    return len(identity_marker)


@query
def stable_sum(db, alpha_value, beta_value, weight):
    return alpha_value + beta_value + weight


def main():
    store = FileSystemArtifactStore(store_dir)
    db = Database(store=store)

    if phase == "save":
        root = build_root(False)
        db.set(alpha, 1)
        db.set(beta, 2)
        value = db.get(root) + db.get(file_size, data_path)
        assert db.get(identity_sensitive) == 1
        assert db.get(stable_sum, 1, 2, mg_helpers.WEIGHT) == 13
        key = db.save_checkpoint()
        key_path.write_text(key)
        print(json.dumps({"result": value, "key": key}))
        return

    alpha_value, beta_value, alt_body = LOAD_CONFIG[variant]
    root = build_root(alt_body)
    db.set(alpha, alpha_value)
    db.set(beta, beta_value)
    db.load_checkpoint(key_path.read_text())
    value = db.get(root) + db.get(file_size, data_path)
    assert db.get(identity_sensitive) == 1
    stable_value = db.get(stable_sum, alpha_value, beta_value, mg_helpers.WEIGHT)
    assert stable_value == alpha_value + beta_value + mg_helpers.WEIGHT
    if variant == "unchanged":
        decision = db.inspect(
            stable_sum, alpha_value, beta_value, mg_helpers.WEIGHT
        ).last_recompute
    elif variant == "file_changed":
        decision = db.inspect(file_size, data_path).last_recompute
    else:
        decision = db.inspect(root).last_recompute
    print(
        json.dumps(
            {
                "result": value,
                "recompute": decision,
                "identity_recompute": db.inspect(identity_sensitive).last_recompute,
            }
        )
    )


main()
'''

HELPER_MODULE_V1 = '__version__ = "1"\nWEIGHT = 10\n'
HELPER_MODULE_V2 = '__version__ = "2"\nWEIGHT = 20\n'


def _src_dir() -> str:
    """The ``src`` directory holding the ``pyinc`` package (not the repo root)."""
    return str(Path(pyinc.__file__).resolve().parent.parent)


def _run(args: list[str], env: dict[str, str]) -> dict[str, Any]:
    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (
        f"fixture subprocess failed ({proc.returncode})\n"
        f"argv: {args}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    payload: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


@dataclass
class CrossProcessEnv:
    python: str
    script: Path
    store_dir: Path
    root: Path
    data_path: Path
    helper_path: Path
    env: dict[str, str]
    save_result: int
    save_key: str

    def run_load(self, variant: str, *, optimize: bool = False) -> dict[str, Any]:
        args = [self.python]
        if optimize:
            args.append("-O")
        args += [str(self.script), str(self.store_dir), "load", variant]
        return _run(args, self.env)


@pytest.fixture
def cross_process(tmp_path: Path) -> CrossProcessEnv:
    script = tmp_path / "fixture_script.py"
    script.write_text(FIXTURE_SCRIPT, encoding="utf-8")
    helper_path = tmp_path / "mg_helpers.py"
    helper_path.write_text(HELPER_MODULE_V1, encoding="utf-8")
    data_path = tmp_path / "data.txt"
    data_path.write_text("hello", encoding="utf-8")  # 5 bytes
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    base_env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([_src_dir(), str(tmp_path)]),
        # Never cache bytecode: an edited helper module whose new source happens
        # to match the old size (V1 and V2 here) could otherwise reimport a stale
        # .pyc and mask the module bump. It also keeps tmp_path free of debris.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    save_env = {**base_env, "PYTHONHASHSEED": SAVE_HASH_SEED}
    load_env = {**base_env, "PYTHONHASHSEED": LOAD_HASH_SEED}
    saved = _run([sys.executable, str(script), str(store_dir), "save", "unchanged"], save_env)
    assert saved["result"] == BASE_RESULT
    return CrossProcessEnv(
        python=sys.executable,
        script=script,
        store_dir=store_dir,
        root=tmp_path,
        data_path=data_path,
        helper_path=helper_path,
        env=load_env,
        save_result=int(saved["result"]),
        save_key=str(saved["key"]),
    )


def test_cross_process_unchanged_state_reuses(cross_process: CrossProcessEnv) -> None:
    out = cross_process.run_load("unchanged")
    # Fresh process, identical explicit arguments, and no identity-observable
    # ambient captures: the capture-free query is served without re-executing.
    assert out["recompute"] == "reused"
    assert out["result"] == cross_process.save_result
    # Ordinary immutable captures carry a process-incarnation token because
    # Python can observe object identity. Their values still match fresh, but
    # a new process executes rather than trusting structural equality alone.
    assert out["identity_recompute"] == "executed"


def test_cross_process_input_change_reexecutes(cross_process: CrossProcessEnv) -> None:
    out = cross_process.run_load("input_changed")
    # alpha moves from 1 to 9 in the loading process; the warmed record carries a
    # real edge to that input, so the root re-executes against the new value.
    assert out["recompute"] == "executed"
    assert out["result"] == 9 + 2 + 5 + 10


def test_cross_process_file_change_reexecutes(cross_process: CrossProcessEnv) -> None:
    # The file resource changes on disk between the two processes.
    cross_process.data_path.write_text("goodbye!", encoding="utf-8")  # 8 bytes
    out = cross_process.run_load("file_changed")
    # The live probe over the changed file fails verification, so the leaf and
    # the root re-execute against the new content.
    assert out["recompute"] == "executed"
    assert out["result"] == 1 + 2 + 8 + 10


def test_cross_process_query_source_change_reexecutes(
    cross_process: CrossProcessEnv,
) -> None:
    out = cross_process.run_load("src_changed")
    # Same query_id, different body: the code fingerprint moves, so the loading
    # process's root no longer matches the checkpointed record and re-executes.
    assert out["recompute"] == "executed"
    assert out["result"] == 1 * 1000 + 2 + 5 + 10


def test_cross_process_captured_module_bump_reexecutes(
    cross_process: CrossProcessEnv,
) -> None:
    # The captured helper module is edited (version bump + constant change)
    # between the two processes.
    cross_process.helper_path.write_text(HELPER_MODULE_V2, encoding="utf-8")
    out = cross_process.run_load("module_changed")
    # The module's source digest and __version__ are folded into the root's
    # identity, so the bump moves the identity and forces a re-execute against
    # the new WEIGHT.
    assert out["recompute"] == "executed"
    assert out["result"] == 1 + 2 + 5 + 20


def test_cross_process_optimize_flag_reexecutes(
    cross_process: CrossProcessEnv,
) -> None:
    # Identical declared state, but the loading process runs under -O. The build
    # configuration is part of the identity, so the record is not reused even
    # though every declared input is unchanged.
    out = cross_process.run_load("unchanged", optimize=True)
    assert out["recompute"] == "executed"
    assert out["result"] == cross_process.save_result
