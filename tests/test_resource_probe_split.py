"""Warm-path resource validation: the probe alone answers an unchanged request.

Each resource here tallies its hook calls in a side file next to its key. The
tally lives outside the resource because a query's capture set must be
immutable. A counter attribute or module global is rejected before the first
``get()``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from pyinc import Database, query
from pyinc.resources import FileStatResource, Resource


def _tally(key: str, event: str) -> None:
    with open(f"{key}.calls", "a", encoding="utf-8") as handle:
        handle.write(event)


def _tallied(key: str) -> str:
    calls = Path(f"{key}.calls")
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


@dataclass(frozen=True)
class _TallyingFileResource(Resource[str, str, tuple[str, str]]):
    """Decodes ``<key>``. Probing only reads and hashes the bytes."""

    def probe(self, key: str) -> tuple[str, str]:
        _tally(key, "p")
        return ("present", hashlib.sha256(Path(key).read_bytes()).hexdigest())

    def load(self, db: Database, key: str) -> str:
        _tally(key, "l")
        return Path(key).read_bytes().decode("utf-8")

    def label(self, key: str) -> str:
        return f"tallying-file[{key}]"


@dataclass(frozen=True)
class _AdvancingProbeResource(Resource[str, str, tuple[int]]):
    """Every probe call observes a strictly newer world via ``<key>.serial``."""

    def probe(self, key: str) -> tuple[int]:
        _tally(key, "p")
        serial_path = Path(f"{key}.serial")
        serial = int(serial_path.read_text(encoding="utf-8")) if serial_path.exists() else 0
        serial += 1
        serial_path.write_text(str(serial), encoding="utf-8")
        return (serial,)

    def load(self, db: Database, key: str) -> str:
        _tally(key, "l")
        return Path(key).read_text(encoding="utf-8")

    def label(self, key: str) -> str:
        return f"advancing[{key}]"


@dataclass(frozen=True)
class _ProbeRaisingResource(Resource[str, str, str]):
    """Standalone probing always raises. The combined read succeeds."""

    def probe(self, key: str) -> str:
        _tally(key, "p")
        raise RuntimeError(f"cannot probe {key}")

    def load(self, db: Database, key: str) -> str:
        raise AssertionError("load must not run")

    def probe_and_load(self, db: Database, key: str) -> tuple[str, str]:
        _tally(key, "a")
        raw = Path(key).read_bytes()
        return hashlib.sha256(raw).hexdigest(), raw.decode("utf-8")

    def label(self, key: str) -> str:
        return f"probe-raising[{key}]"


@dataclass(frozen=True)
class _HealingLoadResource(Resource[str, str, tuple[str, str]]):
    """Loading raises while ``<key>.broken`` exists, then heals."""

    def probe(self, key: str) -> tuple[str, str]:
        _tally(key, "p")
        return ("present", hashlib.sha256(Path(key).read_bytes()).hexdigest())

    def load(self, db: Database, key: str) -> str:
        _tally(key, "l")
        if Path(f"{key}.broken").exists():
            raise RuntimeError(f"cannot load {key}")
        return Path(key).read_text(encoding="utf-8")

    def label(self, key: str) -> str:
        return f"healing-load[{key}]"


@dataclass(frozen=True)
class _FlakyProbeResource(Resource[str, str, str]):
    """Probing raises while ``<key>.noprobe`` exists, then heals."""

    def probe(self, key: str) -> str:
        _tally(key, "p")
        if Path(f"{key}.noprobe").exists():
            raise RuntimeError(f"cannot probe {key}")
        return hashlib.sha256(Path(key).read_bytes()).hexdigest()

    def load(self, db: Database, key: str) -> str:
        _tally(key, "l")
        return Path(key).read_text(encoding="utf-8")

    def label(self, key: str) -> str:
        return f"flaky-probe[{key}]"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_warm_unchanged_read_probes_without_loading(mode: str, tmp_path: Path) -> None:
    """Only probe, load and label are written here. The rest are inherited.

    These are the three hooks the base class leaves unimplemented. The test
    drives the whole path on the smallest possible resource: first read, warm
    validation, invalidation, and a from-scratch comparison.
    """

    resource = _TallyingFileResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("hello", encoding="utf-8")

    @query
    def read_file(db: Database, key: str) -> str:
        return resource.read(db, key)

    # Each mode gets its own tmp_path, so the tally starts empty. A cell that
    # read another cell's tally would fail here.
    assert _tallied(target) == ""

    db = Database(mode=mode)
    assert db.get(read_file, target) == "hello"
    assert _tallied(target) == "pl"

    for _ in range(3):
        assert db.get(read_file, target) == "hello"

    # The probe alone validates an unchanged file: read and hash, no decode.
    # The load runs again only after the probe misses.
    assert _tallied(target) == "pl" + "ppp"
    assert db.statistics().resource_loads == 1
    record = db._records[db._resource_key(resource, target)]
    assert record.last_decision == "reused"
    assert record.reason == "resource probe unchanged"

    # Once the bytes change, the standalone probe disagrees, the combined
    # observation reloads, and the answer follows the file.
    Path(target).write_text("world", encoding="utf-8")
    assert db.get(read_file, target) == "world"
    assert _tallied(target) == "pl" + "ppp" + "ppl"
    assert db.statistics().resource_loads == 2

    # The warm answer matches a database with no history. The fresh database
    # adds to the tally, so it runs last.
    assert Database(mode=mode).get(read_file, target) == "world"


def test_probe_mismatch_stores_the_atomically_observed_pair(tmp_path: Path) -> None:
    resource = _AdvancingProbeResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("payload", encoding="utf-8")

    @query
    def read_serial(db: Database, key: str) -> str:
        return resource.read(db, key)

    db = Database()
    assert db.get(read_serial, target) == "payload"
    assert _tallied(target) == "pl"
    record = db._records[db._resource_key(resource, target)]
    assert record.probe == (1,)

    # The probe advances on every call, so the standalone probe (2) misses and
    # the combined read runs. The record stores the probe observed with the
    # value (3). The standalone probe and the value may straddle a change, so
    # they are never paired.
    assert db.get(read_serial, target) == "payload"
    assert _tallied(target) == "pl" + "ppl"
    record = db._records[db._resource_key(resource, target)]
    assert record.probe == (3,)

    assert db.get(read_serial, target) == "payload"
    assert _tallied(target) == "pl" + "ppl" + "ppl"
    record = db._records[db._resource_key(resource, target)]
    assert record.probe == (5,)


def test_probe_raise_falls_back_to_the_atomic_read(tmp_path: Path) -> None:
    resource = _ProbeRaisingResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("stable", encoding="utf-8")

    @query
    def read_file(db: Database, key: str) -> str:
        return resource.read(db, key)

    db = Database()
    assert db.get(read_file, target) == "stable"
    assert _tallied(target) == "a"

    # The standalone probe raises. A raise counts as no observation, so the
    # combined read decides as if the attempt never happened.
    assert db.get(read_file, target) == "stable"
    assert _tallied(target) == "a" + "pa"
    record = db._records[db._resource_key(resource, target)]
    assert not record.is_failed
    assert not record.probe_unconfirmed
    assert record.last_decision == "reused"
    stats = db.statistics()
    assert stats.resource_loads == 1
    assert stats.resource_probe_hits == 1


def test_failed_records_take_the_full_observation_path(tmp_path: Path) -> None:
    resource = _HealingLoadResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("content", encoding="utf-8")
    broken = Path(f"{target}.broken")
    broken.write_text("", encoding="utf-8")

    @query
    def read_or_default(db: Database, key: str) -> str:
        try:
            return resource.read(db, key)
        except RuntimeError:
            return "<default>"

    db = Database()
    assert db.get(read_or_default, target) == "<default>"
    # The combined read plus the failure-side probe observation.
    assert _tallied(target) == "plp"

    # A failure record holds no value to reuse, so a matching probe proves
    # nothing. Each request re-runs the full load and skips the standalone
    # probe.
    assert db.get(read_or_default, target) == "<default>"
    assert _tallied(target) == "plp" + "plp"

    broken.unlink()
    assert db.get(read_or_default, target) == "content"
    assert _tallied(target) == "plp" + "plp" + "pl"

    # Healed and confirmed, the record answers the next request by probe alone.
    assert db.get(read_or_default, target) == "content"
    assert _tallied(target) == "plp" + "plp" + "pl" + "p"


def test_unconfirmed_probe_records_take_the_full_observation_path(tmp_path: Path) -> None:
    resource = _FlakyProbeResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("content", encoding="utf-8")

    @query
    def read_or_default(db: Database, key: str) -> str:
        try:
            return resource.read(db, key)
        except RuntimeError:
            return "<default>"

    db = Database()
    assert db.get(read_or_default, target) == "content"
    assert _tallied(target) == "pl"

    # Probing breaks entirely. The dependent's verification pass tries the
    # standalone probe, the combined read's probe and the failure-side
    # observation. All three raise, nothing is recorded, and the stored probe
    # becomes unconfirmed. The query body's re-read then skips the standalone
    # attempt, because the record is unconfirmed, and fails through the
    # combined read and failure-side observation again.
    noprobe = Path(f"{target}.noprobe")
    noprobe.write_text("", encoding="utf-8")
    assert db.get(read_or_default, target) == "<default>"
    assert _tallied(target) == "pl" + "ppppp"
    record = db._records[db._resource_key(resource, target)]
    assert record.probe_unconfirmed

    # The world returns to the state the record describes. The record still may
    # not answer by probe match. A full observation rewrites it, with no
    # standalone probe first.
    noprobe.unlink()
    assert db.get(read_or_default, target) == "content"
    assert _tallied(target) == "pl" + "ppppp" + "pl"
    record = db._records[db._resource_key(resource, target)]
    assert not record.probe_unconfirmed
    assert record.last_decision == "executed"

    # Confirmed again, the record answers the next request by probe alone.
    assert db.get(read_or_default, target) == "content"
    assert _tallied(target) == "pl" + "ppppp" + "pl" + "p"


def test_warm_probe_hit_counts_as_probe_hit_not_load(tmp_path: Path) -> None:
    resource = _TallyingFileResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("hello", encoding="utf-8")

    @query
    def read_file(db: Database, key: str) -> str:
        return resource.read(db, key)

    db = Database()
    db.get(read_file, target)
    stats = db.statistics()
    assert stats.resource_loads == 1
    assert stats.resource_probe_hits == 0

    db.get(read_file, target)
    stats = db.statistics()
    assert stats.resource_probe_hits == 1
    assert stats.resource_loads == 1
    assert _tallied(target) == "pl" + "p"

    Path(target).write_text("world", encoding="utf-8")
    assert db.get(read_file, target) == "world"
    stats = db.statistics()
    assert stats.resource_loads == 2
    assert _tallied(target) == "pl" + "p" + "ppl"


def test_filestat_probe_and_load_classify_failures_identically(tmp_path: Path) -> None:
    resource = FileStatResource()
    db = Database(mode="strict")

    missing = tmp_path / "absent.txt"
    probe_result = resource.probe(missing)
    load_result = resource.load(db, missing)
    assert probe_result == (False, None, None)
    assert (load_result.exists, load_result.size, load_result.mtime_ns) == (False, None, None)

    through_file = tmp_path / "plain.txt" / "child"
    (tmp_path / "plain.txt").write_text("x", encoding="utf-8")

    def outcome(call: Callable[[], object]) -> type[BaseException] | object:
        try:
            call()
        except OSError as error:
            return type(error)
        return "no error"

    through_file_outcome = outcome(lambda: resource.probe(through_file))
    assert through_file_outcome == outcome(lambda: resource.load(db, through_file))
    # A child under a regular file is missing. The platform reports it as
    # WinError 3 (path not found) on Windows and ENOTDIR on POSIX. The snapshot
    # classifies both as missing, for probe and load alike.
    assert through_file_outcome == "no error"
    assert resource.probe(through_file) == (False, None, None)
    loaded = resource.load(db, through_file)
    assert (loaded.exists, loaded.size, loaded.mtime_ns) == (False, None, None)
