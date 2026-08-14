"""Cross-process checkpoint round-trips.

The durable-cache contract only means something across a *real* process
boundary: fresh ``id()``s, a fresh randomized hash seed, and fresh module
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
                + len(files.read(db, data_path))
                + mg_helpers.WEIGHT
            )

    else:

        @query
        def root(db):
            return (
                alpha.read(db)
                + beta.read(db)
                + len(files.read(db, data_path))
                + mg_helpers.WEIGHT
            )

    return root


def main():
    store = FileSystemArtifactStore(store_dir)
    db = Database(store=store)

    if phase == "save":
        root = build_root(False)
        db.set(alpha, 1)
        db.set(beta, 2)
        value = db.get(root)
        key = db.save_checkpoint()
        key_path.write_text(key)
        print(json.dumps({"result": value, "key": key}))
        return

    alpha_value, beta_value, alt_body = LOAD_CONFIG[variant]
    root = build_root(alt_body)
    db.set(alpha, alpha_value)
    db.set(beta, beta_value)
    db.load_checkpoint(key_path.read_text())
    value = db.get(root)
    print(
        json.dumps(
            {"result": value, "recompute": db.inspect(root).last_recompute}
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

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([_src_dir(), str(tmp_path)]),
        # Never cache bytecode: an edited helper module whose new source happens
        # to match the old size (V1 and V2 here) could otherwise reimport a stale
        # .pyc and mask the module bump. It also keeps tmp_path free of debris.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    saved = _run([sys.executable, str(script), str(store_dir), "save", "unchanged"], env)
    assert saved["result"] == BASE_RESULT
    return CrossProcessEnv(
        python=sys.executable,
        script=script,
        store_dir=store_dir,
        root=tmp_path,
        data_path=data_path,
        helper_path=helper_path,
        env=env,
        save_result=int(saved["result"]),
        save_key=str(saved["key"]),
    )


def test_cross_process_unchanged_state_reuses(cross_process: CrossProcessEnv) -> None:
    out = cross_process.run_load("unchanged")
    # Fresh process, identical declared state: the checkpoint verifies (input
    # digests match, the unchanged file re-establishes its resource record from
    # the probe hint) and the root is served without re-executing.
    assert out["recompute"] == "reused"
    assert out["result"] == cross_process.save_result


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


WRAPPED_FIXTURE_SCRIPT = '''\
"""Cross-process fixture: queries capturing a wraps-decorated callable whose
instance state is mutated at runtime in the loading process. The same instance
is reached both through the helper module and through a direct import."""

import json
import sys
from pathlib import Path

from pyinc import Database, FileSystemArtifactStore, query

import wrapped_state_helper  # tmp_path module; byte-identical in both phases
from wrapped_state_helper import scaler

store_dir = sys.argv[1]
phase = sys.argv[2]


@query
def scaled_via_module(db):
    return wrapped_state_helper.scaler(10)


@query
def scaled_direct(db):
    return scaler(10)


def main():
    store = FileSystemArtifactStore(store_dir)
    if phase == "save":
        db = Database(store=store)
        results = [db.get(scaled_via_module), db.get(scaled_direct)]
        key = db.save_checkpoint()
        json.dump({"results": results, "key": key}, sys.stdout)
        return

    # Mutate the live instance before anything is loaded: the helper's source
    # is untouched, so only the callable's state can distinguish the processes.
    # Phase "load_unchanged" skips the mutation and must be served instead.
    if phase == "load":
        wrapped_state_helper.scaler.k = 3
    db = Database(store=store)
    db.load_checkpoint((Path(store_dir).parent / "wrapped.key").read_text())
    results = [db.get(scaled_via_module), db.get(scaled_direct)]
    recomputes = [
        db.inspect(scaled_via_module).last_recompute,
        db.inspect(scaled_direct).last_recompute,
    ]
    json.dump({"results": results, "recomputes": recomputes}, sys.stdout)


main()
'''

WRAPPED_HELPER_SOURCE = """\
import functools


def base(value):
    return value


class Scaler:
    def __init__(self, k):
        self.k = k
        functools.wraps(base)(self)

    def __call__(self, value):
        return self.k * value


scaler = Scaler(2)
"""


def test_wrapped_callable_state_change_misses_across_processes(tmp_path: Path) -> None:
    script = tmp_path / "wrapped_fixture.py"
    script.write_text(WRAPPED_FIXTURE_SCRIPT, encoding="utf-8")
    helper = tmp_path / "wrapped_state_helper.py"
    helper.write_text(WRAPPED_HELPER_SOURCE, encoding="utf-8")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([_src_dir(), str(tmp_path)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    saved = _run([sys.executable, str(script), str(store_dir), "save"], env)
    assert saved["results"] == [20, 20]
    (tmp_path / "wrapped.key").write_text(saved["key"])

    # A loading process that leaves the factor alone is served from the
    # checkpoint. This is what makes the mutated run below evidence about the
    # callable's state rather than about the harness refusing every warm.
    unchanged = _run([sys.executable, str(script), str(store_dir), "load_unchanged"], env)
    assert unchanged["results"] == [20, 20]
    assert unchanged["recomputes"] == ["reused", "reused"]

    # Both phases run the same file and the helper is never rewritten, so the
    # module stamp is identical in every process; the factor moving from 2 to 3
    # in the loading process is the only difference the identity can see. The
    # checkpointed records must miss and the queries re-execute against k=3,
    # whether the callable is reached through the module or imported directly.
    loaded = _run([sys.executable, str(script), str(store_dir), "load"], env)
    assert loaded["results"] == [30, 30]
    assert loaded["recomputes"] == ["executed", "executed"]


PINNED_FIXTURE_SCRIPT = '''\
"""Cross-process fixture: two roots reaching the same dependency query, one
through a captured function and one through a class that carries __wrapped__."""

import json
import sys
from pathlib import Path

from pyinc import Database, FileSystemArtifactStore

import wrapped_pin_helper

store_dir = sys.argv[1]
phase = sys.argv[2]

through_function = wrapped_pin_helper.through_function
through_class = wrapped_pin_helper.through_class


def main():
    store = FileSystemArtifactStore(store_dir)
    db = Database(store=store)
    key_path = Path(store_dir).parent / "pinned.key"
    if phase == "save":
        results = [db.get(through_function), db.get(through_class)]
        key_path.write_text(db.save_checkpoint())
        json.dump({"results": results}, sys.stdout)
        return

    db.load_checkpoint(key_path.read_text())
    function_result = db.get(through_function)
    function_recompute = db.inspect(through_function).last_recompute
    class_result = db.get(through_class)
    class_recompute = db.inspect(through_class).last_recompute
    json.dump(
        {
            "results": [function_result, class_result],
            "recomputes": [function_recompute, class_recompute],
        },
        sys.stdout,
    )


main()
'''

PINNED_HELPER_SOURCE = """\
from pyinc import query


@query
def leaf(db):
    return 7


def reach(db):
    return leaf(db)


class Gate:
    __wrapped__ = reach


@query
def through_function(db):
    return reach(db) + 2


@query
def through_class(db):
    return Gate.__wrapped__(db) + 1
"""


def test_dep_query_behind_wrapped_class_reexecutes_across_processes(tmp_path: Path) -> None:
    script = tmp_path / "pinned_fixture.py"
    script.write_text(PINNED_FIXTURE_SCRIPT, encoding="utf-8")
    helper = tmp_path / "wrapped_pin_helper.py"
    helper.write_text(PINNED_HELPER_SOURCE, encoding="utf-8")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([_src_dir(), str(tmp_path)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    saved = _run([sys.executable, str(script), str(store_dir), "save"], env)
    assert saved["results"] == [9, 8]

    # Nothing changed between the processes, so both roots must answer the same
    # values. They get there differently: the root capturing the function has
    # `leaf` code-pinned and its checkpointed record is served, while the root
    # reaching `leaf` only through a class is not walked into -- captured classes
    # are uniformly skipped by the pinning walk -- so `leaf` is unpinned there,
    # the warm refuses the record rather than serving it, and the root executes.
    # Should _collect_pinned_capture_objects walk captured classes again, the
    # expectation here becomes ["reused", "reused"]; update it, do not delete it.
    loaded = _run([sys.executable, str(script), str(store_dir), "load"], env)
    assert loaded["results"] == [9, 8]
    assert loaded["recomputes"] == ["reused", "executed"]
