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


def _child_env(module_dir: str, seed: str | None) -> dict[str, str]:
    """A child environment whose hash seed is an axis the caller chooses.

    ``{**os.environ, ...}`` rather than a bare dict, so the child still inherits
    ``TMPDIR`` and, on Windows, ``SYSTEMROOT``. Bytecode caching is off in every
    child: a ``.pyc`` records the absolute path of the source it was built from,
    which the install-path test below would otherwise measure instead of the
    identity. A row that wants no pinned seed *deletes* ``PYTHONHASHSEED``
    rather than setting it to the empty string: CPython reads an empty value as
    absent, so the two are one configuration -- the one users actually run --
    and setting it would only prove that they are read alike.
    """

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([_src_dir(), module_dir]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if seed is None:
        env.pop("PYTHONHASHSEED", None)
    else:
        env["PYTHONHASHSEED"] = seed
    return env


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


# The two fixtures below both ask the shipped source-text query directly and
# again through a caller's own queries stacked above it. Every caller reaches the
# shipped query and the shipped source resource as OBJECTS -- imported by name,
# never as ``python_source.source_text`` or ``python_source._FILES``. That is
# load-bearing: a module attribute in the captured chain is something the warm
# path cannot pin, so the query below it would be executed to verify it while
# every caller above still reported a reuse, and the row would be measuring the
# warm gate rather than the identity it is here to hold open.
SEEDED_FIXTURE_SCRIPT = '''\
"""Cross-process checkpoint fixture for the hash-seed axis. Everything is
defined at module level so identities are reproducible across processes."""

import json
import sys
from pathlib import Path

from pyinc import Database, FileSystemArtifactStore, query
from pyinc.integrations.python_source import _FILES, source_text

store_dir = sys.argv[1]
phase = sys.argv[2]

root_dir = Path(store_dir).parent
source_path = str(root_dir / "sample.py")
key_path = root_dir / "seeded.key"


@query
def caller_leaf(db, path):
    return len(source_text(db, path)) + len(_FILES.read(db, path)[0])


@query
def caller_parent(db, path):
    return caller_leaf(db, path)


@query
def caller_grandparent(db, path):
    return caller_parent(db, path) + 1


ROOTS = (
    ("shipped", source_text),
    ("parent", caller_parent),
    ("grandparent", caller_grandparent),
)


def main():
    db = Database(store=FileSystemArtifactStore(store_dir))
    if phase == "save":
        results = [db.get(root, source_path) for _, root in ROOTS]
        key_path.write_text(db.save_checkpoint(), encoding="utf-8")
        print(json.dumps({"results": results}))
        return

    db.load_checkpoint(key_path.read_text(encoding="utf-8"))
    results = [db.get(root, source_path) for _, root in ROOTS]
    recomputes = {
        name: db.inspect(root, source_path).last_recompute for name, root in ROOTS
    }
    print(
        json.dumps(
            {
                "results": results,
                "recomputes": recomputes,
                "executions": db.statistics().query_executions,
            }
        )
    )


main()
'''

CROSSPATH_HELPER_SOURCE = """\
from pyinc import query
from pyinc.integrations.python_source import _FILES, source_text


@query
def caller_leaf(db, path):
    return len(source_text(db, path)) + len(_FILES.read(db, path)[0])


@query
def caller_root(db, path):
    return caller_leaf(db, path) + 1
"""

CROSSPATH_FIXTURE_SCRIPT = '''\
"""Cross-process checkpoint fixture for the install-path axis. The caller's
module is imported by name, so each phase picks up whichever byte-identical copy
its own PYTHONPATH names."""

import json
import sys
from pathlib import Path

from pyinc import Database, FileSystemArtifactStore
from pyinc.integrations.python_source import source_text

from cxp_crosspath_caller import caller_root

store_dir = sys.argv[1]
phase = sys.argv[2]
source_path = sys.argv[3]

key_path = Path(store_dir).parent / "crosspath.key"

ROOTS = (("shipped", source_text), ("root", caller_root))


def main():
    db = Database(store=FileSystemArtifactStore(store_dir))
    if phase == "save":
        results = [db.get(root, source_path) for _, root in ROOTS]
        key_path.write_text(db.save_checkpoint(), encoding="utf-8")
        print(json.dumps({"results": results}))
        return

    db.load_checkpoint(key_path.read_text(encoding="utf-8"))
    results = [db.get(root, source_path) for _, root in ROOTS]
    recomputes = {
        name: db.inspect(root, source_path).last_recompute for name, root in ROOTS
    }
    print(
        json.dumps(
            {
                "results": results,
                "recomputes": recomputes,
                "executions": db.statistics().query_executions,
            }
        )
    )


main()
'''

# The file the roots analyse. Its content never moves between the two phases, so
# it is never the axis; only the seed and the install prefix are.
SAMPLE_SOURCE = '"""sample"""\n\n\ndef f(x):\n    return x + 1\n'

SEEDED_ROOTS = ("shipped", "parent", "grandparent")
CROSSPATH_ROOTS = ("shipped", "root")


def _assert_warm_across_processes(
    saved: dict[str, Any],
    loaded: dict[str, Any],
    roots: tuple[str, ...],
    label: str,
) -> None:
    """Every root answered from the checkpoint, and nothing executed underneath.

    The executed-query count is asserted as well as the decision because
    ``last_recompute`` alone under-reports the work: a dependency the warm path
    cannot pin is executed to verify it, and the caller above it still reports a
    reuse. A row that only read the decision would pass with real work happening.
    """

    assert loaded["results"] == saved["results"], label
    assert loaded["recomputes"] == {name: "reused" for name in roots}, label
    assert loaded["executions"] == 0, label


def _seeded_round_trip(
    tmp_path: Path, save_seed: str | None, load_seed: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Save under one hash seed and load under another, same tree, same paths."""

    script = tmp_path / "seeded_fixture.py"
    script.write_text(SEEDED_FIXTURE_SCRIPT, encoding="utf-8")
    (tmp_path / "sample.py").write_text(SAMPLE_SOURCE, encoding="utf-8")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    saved = _run(
        [sys.executable, str(script), str(store_dir), "save"],
        _child_env(str(tmp_path), save_seed),
    )
    loaded = _run(
        [sys.executable, str(script), str(store_dir), "load"],
        _child_env(str(tmp_path), load_seed),
    )
    return saved, loaded


# There is deliberately no (0, non-zero) row here. ``PYTHONHASHSEED=0`` does not
# pick a seed: it turns hash randomization off, which is a difference in how the
# interpreter was configured rather than in the order anything was hashed -- so a
# row crossing it would not be evidence about the seed at all. The randomization
# flag has a cell of its own below, and
# ``test_cross_process_optimize_flag_reexecutes`` above is the tree's existing
# cell for a build-configuration difference that still, deliberately, misses.
@pytest.mark.parametrize(
    ("save_seed", "load_seed", "label"),
    [
        ("1", "1", "control_one_pinned_seed_on_both_sides"),
        ("0", "0", "control_randomization_off_on_both_sides"),
        ("1", "2", "two_different_non_zero_seeds"),
        ("3", "4", "two_further_different_non_zero_seeds"),
        (None, None, "no_pinned_seed_at_all_what_users_run"),
    ],
)
def test_cross_process_reuse_survives_a_differing_hash_seed(
    tmp_path: Path, save_seed: str | None, load_seed: str | None, label: str
) -> None:
    """A checkpoint written under one hash seed warms a process under another.

    The two same-seed rows are the controls: they say the round trip works at
    all, so a red on one of the other three is about the seed and nothing else.
    The last row pins no seed on either side, which is what an ordinary run does
    -- two such processes carry the same flags and different hash orders, so it
    is the row that fails first when anything a query's identity folds depends on
    the order a set or a dict was built in.
    """

    saved, loaded = _seeded_round_trip(tmp_path, save_seed, load_seed)
    _assert_warm_across_processes(saved, loaded, SEEDED_ROOTS, label)


@pytest.mark.parametrize(
    ("save_seed", "load_seed", "label"),
    [
        ("0", None, "randomization_off_when_written_on_when_read"),
        (None, "0", "randomization_on_when_written_off_when_read"),
    ],
)
def test_cross_process_reuse_survives_the_hash_randomization_flag(
    tmp_path: Path, save_seed: str | None, load_seed: str | None, label: str
) -> None:
    """The build-identity axis, not the hash-order one.

    ``PYTHONHASHSEED=0`` turns hash randomization off, and whether it is off is a
    property of how the interpreter was set up rather than of the order any
    particular dict was built in. The configurations that pin it are the ones
    that most want a shared cache -- a benchmark harness, a documentation runner,
    a CI job asking for a reproducible run -- so a checkpoint one of them writes
    has to warm an ordinary process and the other way round. This pair is kept
    out of the seed test above so that a red here reads as what it is.
    """

    saved, loaded = _seeded_round_trip(tmp_path, save_seed, load_seed)
    _assert_warm_across_processes(saved, loaded, SEEDED_ROOTS, label)


def _crosspath_round_trip(
    tmp_path: Path,
    arm: str,
    script: Path,
    source_path: Path,
    save_dir: Path,
    load_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Save with one copy of the caller's module on the path, load with another.

    The seed is pinned to the same value in both children so the absolute prefix
    the caller's module was imported from is the only axis moving.
    """

    store_dir = tmp_path / arm / "store"
    store_dir.mkdir(parents=True)
    saved = _run(
        [sys.executable, str(script), str(store_dir), "save", str(source_path)],
        _child_env(str(save_dir), "1"),
    )
    loaded = _run(
        [sys.executable, str(script), str(store_dir), "load", str(source_path)],
        _child_env(str(load_dir), "1"),
    )
    return saved, loaded


def test_cross_process_reuse_survives_a_different_install_path(tmp_path: Path) -> None:
    """A checkpoint written at one absolute prefix warms a load from another.

    The two directories hold byte-identical copies of the caller's module and
    differ only in name -- and in name *length*, so a payload that folded the
    path would differ in more than a substitution. This is the shape a container
    image, a second virtualenv or a second CI runner produces: the same
    distribution, installed somewhere else.
    """

    short_dir = tmp_path / "lib"
    long_dir = tmp_path / "lib-under-a-considerably-longer-name"
    for directory in (short_dir, long_dir):
        directory.mkdir()
        (directory / "cxp_crosspath_caller.py").write_text(
            CROSSPATH_HELPER_SOURCE, encoding="utf-8"
        )
    script = tmp_path / "crosspath_fixture.py"
    script.write_text(CROSSPATH_FIXTURE_SCRIPT, encoding="utf-8")
    source_path = tmp_path / "sample.py"
    source_path.write_text(SAMPLE_SOURCE, encoding="utf-8")

    # The control comes first: the same copy on both sides. It is what makes the
    # cross-path arm below evidence about the prefix rather than about the round
    # trip refusing everything.
    saved, loaded = _crosspath_round_trip(
        tmp_path, "control", script, source_path, short_dir, short_dir
    )
    _assert_warm_across_processes(saved, loaded, CROSSPATH_ROOTS, "same install path")

    saved, loaded = _crosspath_round_trip(
        tmp_path, "moved", script, source_path, short_dir, long_dir
    )
    _assert_warm_across_processes(
        saved, loaded, CROSSPATH_ROOTS, "different install path"
    )


# A package whose parent reaches its child as a module attribute -- the spelling
# `import pkg.queries as q` produces -- beside the `from pkg.queries import thing`
# control. The two callers live in separate modules on purpose: on 3.11
# ``inspect.getclosurevars`` reports an attribute name that is also a global of
# the same module as a captured global, so one module holding both spellings
# would pin the child through the control's import and measure nothing.
MODATTR_QUERIES_SOURCE = """\
from pyinc import query
from pyinc.integrations.python_source import _FILES, source_text


@query
def thing(db, path):
    return len(source_text(db, path)) + len(_FILES.read(db, path)[0])
"""

MODATTR_VIA_MODULE_SOURCE = """\
from pyinc import query

import cxp_pkg.queries as q


@query
def parent_via_module(db, path):
    return q.thing(db, path) + 1


@query
def grandparent_via_module(db, path):
    return parent_via_module(db, path) + 1
"""

MODATTR_VIA_NAME_SOURCE = """\
from pyinc import query

from cxp_pkg.queries import thing


@query
def parent_via_name(db, path):
    return thing(db, path) + 1
"""

MODATTR_FIXTURE_SCRIPT = '''\
"""Cross-process checkpoint fixture for the module-attribute reach. The save
phase asks every root; a load phase asks ONE root, named by argv, so nothing an
earlier request established in the same process can stand in for the pin."""

import json
import sys
from pathlib import Path

from pyinc import Database, FileSystemArtifactStore

from cxp_pkg.via_module import grandparent_via_module, parent_via_module
from cxp_pkg.via_name import parent_via_name

store_dir = sys.argv[1]
phase = sys.argv[2]
root_name = sys.argv[3]

root_dir = Path(store_dir).parent
source_path = str(root_dir / "sample.py")
key_path = root_dir / "modattr.key"

ROOTS = {
    "parent_via_module": parent_via_module,
    "grandparent_via_module": grandparent_via_module,
    "parent_via_name": parent_via_name,
}


def main():
    db = Database(store=FileSystemArtifactStore(store_dir))
    if phase == "save":
        results = {name: db.get(root, source_path) for name, root in ROOTS.items()}
        key_path.write_text(db.save_checkpoint(), encoding="utf-8")
        print(json.dumps({"results": results}))
        return

    db.load_checkpoint(key_path.read_text(encoding="utf-8"))
    root = ROOTS[root_name]
    result = db.get(root, source_path)
    print(
        json.dumps(
            {
                "result": result,
                "recompute": db.inspect(root, source_path).last_recompute,
                "executions": db.statistics().query_executions,
            }
        )
    )


main()
'''


@pytest.mark.parametrize(
    "root",
    ["parent_via_module", "grandparent_via_module", "parent_via_name"],
)
def test_cross_process_reuse_through_a_module_attribute(tmp_path: Path, root: str) -> None:
    """A parent that calls its child as ``q.thing(db, x)`` warms like one that
    calls ``thing(db, x)``.

    The fingerprint folds the child behind a static module-attribute chain
    into the parent's identity, so the chain pins the child as a direct capture
    does and the warm gate must count it. The grandparent row is the shape that
    hid the defect: its own record warmed while the parent underneath was
    executed to verify, so ``last_recompute`` alone read as a reuse. Every row
    asserts the execution count for that reason. The last row is the control.
    """

    package = tmp_path / "cxp_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "queries.py").write_text(MODATTR_QUERIES_SOURCE, encoding="utf-8")
    (package / "via_module.py").write_text(MODATTR_VIA_MODULE_SOURCE, encoding="utf-8")
    (package / "via_name.py").write_text(MODATTR_VIA_NAME_SOURCE, encoding="utf-8")
    script = tmp_path / "modattr_fixture.py"
    script.write_text(MODATTR_FIXTURE_SCRIPT, encoding="utf-8")
    (tmp_path / "sample.py").write_text(SAMPLE_SOURCE, encoding="utf-8")
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    # The seed is pinned on both sides so the spelling of the edge is the only
    # axis; the seed has cells of its own above.
    env = _child_env(str(tmp_path), "1")
    saved = _run([sys.executable, str(script), str(store_dir), "save", root], env)
    loaded = _run([sys.executable, str(script), str(store_dir), "load", root], env)
    assert loaded["result"] == saved["results"][root], root
    assert loaded["recompute"] == "reused", root
    assert loaded["executions"] == 0, root


# A package whose module captures form a cycle: `top` reaches `q.child`, `q`
# reaches `q2.leaf`, and `q2` reaches back into `q` for `helper`. The
# fingerprint folds a module already being folded on the same chain by
# identity and chain names only, so `helper`'s body is not part of `parent`'s
# identity. It lives in its own module on purpose: editing it moves neither
# `q`'s nor `q2`'s file digest, so the parent's saved record is found and only
# the pinned walk and the dep warm stand between the load and a stale answer.
MODCYCLE_TOP_SOURCE = """\
from pyinc import query

import cxp_cycle.q as q


@query
def parent(db, path):
    return q.child(db, path) + 100
"""

MODCYCLE_Q_SOURCE = """\
from pyinc import query

import cxp_cycle.q2 as q2
from cxp_cycle.h import helper  # noqa: F401  (reached as q.helper from q2)


@query
def child(db, path):
    return q2.leaf(db, path) + 1
"""

MODCYCLE_Q2_SOURCE = """\
from pyinc import query

import cxp_cycle.q as q


@query
def leaf(db, path):
    return q.helper(db, path) * 10
"""

MODCYCLE_HELPER_SOURCE_V1 = """\
from pyinc import query
from pyinc.integrations.python_source import source_text


@query
def helper(db, path):
    return len(source_text(db, path)) + 5000
"""

MODCYCLE_HELPER_SOURCE_V2 = MODCYCLE_HELPER_SOURCE_V1.replace("+ 5000", "+ 50000")

MODCYCLE_FIXTURE_SCRIPT = '''\
"""Cross-process checkpoint fixture for the module-capture cycle. The load
phase also asks a database with no store, whose answer is the fresh one by
construction."""

import json
import sys
from pathlib import Path

from pyinc import Database, FileSystemArtifactStore

from cxp_cycle.top import parent

store_dir = sys.argv[1]
phase = sys.argv[2]

root_dir = Path(store_dir).parent
source_path = str(root_dir / "sample.py")
key_path = root_dir / "modcycle.key"


def main():
    db = Database(store=FileSystemArtifactStore(store_dir))
    if phase == "save":
        result = db.get(parent, source_path)
        key_path.write_text(db.save_checkpoint(), encoding="utf-8")
        print(json.dumps({"result": result}))
        return

    db.load_checkpoint(key_path.read_text(encoding="utf-8"))
    result = db.get(parent, source_path)
    print(
        json.dumps(
            {
                "result": result,
                "recompute": db.inspect(parent, source_path).last_recompute,
                "executions": db.statistics().query_executions,
                "fresh": Database().get(parent, source_path),
            }
        )
    )


main()
'''


def test_cross_process_module_capture_cycle_reexecutes_an_edited_leaf(tmp_path: Path) -> None:
    """A query reached only around a module-capture cycle is not pinned, and
    an edit to it re-executes the parent in a fresh process.

    The parent's identity is unchanged by the edit, so its saved record is
    found and the deps decide. The cell fails when the pinned walk descends
    the module the fold declined to descend: every dep then warms from its old
    record and the parent is served with zero executions. The answer must be
    the fresh one, not merely different from the saved one, and the recompute
    must be an execution, so a warm that re-ran nothing cannot pass by luck.
    """

    package = tmp_path / "cxp_cycle"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "top.py").write_text(MODCYCLE_TOP_SOURCE, encoding="utf-8")
    (package / "q.py").write_text(MODCYCLE_Q_SOURCE, encoding="utf-8")
    (package / "q2.py").write_text(MODCYCLE_Q2_SOURCE, encoding="utf-8")
    helper_path = package / "h.py"
    helper_path.write_text(MODCYCLE_HELPER_SOURCE_V1, encoding="utf-8")
    script = tmp_path / "modcycle_fixture.py"
    script.write_text(MODCYCLE_FIXTURE_SCRIPT, encoding="utf-8")
    (tmp_path / "sample.py").write_text(SAMPLE_SOURCE, encoding="utf-8")
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    env = _child_env(str(tmp_path), "1")
    saved = _run([sys.executable, str(script), str(store_dir), "save"], env)
    helper_path.write_text(MODCYCLE_HELPER_SOURCE_V2, encoding="utf-8")
    loaded = _run([sys.executable, str(script), str(store_dir), "load"], env)
    assert loaded["fresh"] != saved["result"]
    assert loaded["result"] == loaded["fresh"]
    assert loaded["recompute"] == "executed"
    assert loaded["executions"] >= 1
