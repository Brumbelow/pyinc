"""Suite-wide fixtures.

Every test runs against a fake, empty site-packages unless it opts out, because
a fresh analysis otherwise scans the real development environment and that scan
dominates the runtime of anything that builds a session.
"""

from __future__ import annotations

import site
from pathlib import Path

import pytest


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
    ``site.getsitepackages`` and ``site.getusersitepackages``; both are
    replaced so a fresh analysis lists nothing. A test that genuinely needs the
    real environment opts out with ``@pytest.mark.real_site_packages``.
    """
    if request.node.get_closest_marker("real_site_packages"):
        return
    fake = str(_fake_site_packages)
    monkeypatch.setattr(site, "getsitepackages", lambda *args, **kwargs: [fake])
    monkeypatch.setattr(site, "getusersitepackages", lambda *args, **kwargs: fake)
