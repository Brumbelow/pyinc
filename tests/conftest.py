"""Suite-wide fixtures and collection rules.

Every test runs against a fake, empty site-packages unless it opts out. A
fresh analysis would otherwise scan the real development environment, and that
scan dominates the runtime of anything that builds a session.

Tests that check the repository itself are marked ``process`` by file name, so
the cross-platform matrix can skip them and the quality job can run them once.
"""

from __future__ import annotations

import site
from pathlib import Path

import pytest

# Tests that verify the repository as shipped: its docs, signed history,
# release metadata and artifacts, the bench harness, and the cutoff inventory.
# They give the same result on every operating system and interpreter.
_PROCESS_FILES = frozenset(
    {
        "test_bench_smoke.py",
        "test_cutoff_inventory.py",
        "test_docs.py",
        "test_release_artifacts.py",
        "test_release_metadata.py",
        "test_signed_history.py",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.name in _PROCESS_FILES:
            item.add_marker(pytest.mark.process)


@pytest.fixture(scope="session", autouse=True)
def _install_the_guard() -> None:
    """Install the ambient-read guard before any test runs.

    The guard wraps whatever each guarded name holds when the first `Database`
    is built, once per process. A test that patches a guarded name and then
    builds the first `Database` would leave that name unguarded for every
    later test once its patch is undone.
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
