"""Suite-wide fixtures and collection rules.

Every test runs against a fake, empty site-packages unless it opts out. A
fresh analysis would otherwise scan the real development environment, and that
scan dominates the runtime of anything that builds a session.

Tests that check the repository itself are marked ``process`` by file name, so
the cross-platform matrix can skip them and the quality job can run them once.

``PYINC_TEST_SHARD``, written ``K/N``, keeps shard K of N. CI runs one cell as
several jobs this way. Unset, every collected test runs.
"""

from __future__ import annotations

import hashlib
import os
import re
import site
from pathlib import Path

import pytest

# Tests that verify the repository as shipped: its docs, release metadata
# and artifacts, the bench harness, the cutoff inventory, the test shards
# and the tool pins. They give the same result on every operating system
# and interpreter.
_PROCESS_FILES = frozenset(
    {
        "test_bench_smoke.py",
        "test_cutoff_inventory.py",
        "test_docs.py",
        "test_release_artifacts.py",
        "test_release_metadata.py",
        "test_repository_policy.py",
    }
)


def parse_shard(value: str) -> tuple[int, int]:
    """Read a ``PYINC_TEST_SHARD`` value, written ``K/N``, as ``(K, N)``."""
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    if match is None or int(match[1]) > int(match[2]):
        raise pytest.UsageError(f"PYINC_TEST_SHARD must read K/N with 1 <= K <= N, not {value!r}")
    return int(match[1]), int(match[2])


def in_shard(node_id: str, index: int, count: int) -> bool:
    """Whether shard ``index`` of ``count`` runs the test with this node id.

    The test's place comes from a digest of its node id. ``hash()`` is salted
    per process, so the xdist workers of one job would disagree on it. Pytest
    writes node ids with forward slashes on every system, so a shard holds the
    same tests on Windows as elsewhere.
    """
    digest = hashlib.sha256(node_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") % count == index - 1


def _requested_shard() -> tuple[int, int] | None:
    value = os.environ.get("PYINC_TEST_SHARD")
    return None if value is None else parse_shard(value)


def pytest_configure(config: pytest.Config) -> None:
    # Read here too, so a malformed value stops the run with its message
    # before xdist starts a worker. A worker's collection error names no cause.
    _requested_shard()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.name in _PROCESS_FILES:
            item.add_marker(pytest.mark.process)

    shard = _requested_shard()
    if shard is None:
        return
    index, count = shard
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        (kept if in_shard(item.nodeid, index, count) else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


@pytest.fixture(scope="session", autouse=True)
def _install_the_guard() -> None:
    """Install the ambient-read guard before any test runs.

    The guard wraps whatever each guarded name holds when the first `Database`
    is built. If a test had a guarded name patched at that moment, the name
    would hold the plain standard-library callable once the patch ends, until
    the next query execution wraps it again. Installing first gives every test
    the same guarded names, whatever order the tests run in.
    """
    from pyinc import Database

    Database()


@pytest.fixture(scope="session")
def _fake_site_packages(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("site-packages")


@pytest.fixture(autouse=True)
def _isolate_site_packages(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    _fake_site_packages: Path,
) -> None:
    """Point site-packages discovery at an empty directory.

    ``pyinc.integrations.installed_packages`` discovers the environment through
    ``site.getsitepackages`` and ``site.getusersitepackages``. Both are replaced,
    so a fresh analysis sees an empty environment. A test that needs the real
    environment opts out with ``@pytest.mark.real_site_packages``.
    """
    if request.node.get_closest_marker("real_site_packages"):
        return
    fake = str(_fake_site_packages)
    monkeypatch.setattr(site, "getsitepackages", lambda *args, **kwargs: [fake])
    monkeypatch.setattr(site, "getusersitepackages", lambda *args, **kwargs: fake)
