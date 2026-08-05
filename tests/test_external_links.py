from __future__ import annotations

import sys
import urllib.error
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts import check_external_links
    from scripts.check_external_links import Link
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import check_external_links  # noqa: E402
    from scripts.check_external_links import Link  # noqa: E402


def _http_404(target: str, method: str) -> int:
    raise urllib.error.HTTPError(target, 404, "not found", Message(), None)


@pytest.mark.parametrize(
    "target",
    (
        "https://github.com/Brumbelow/pyinc/blob/v3.1.2/docs/guide.md",
        "https://raw.githubusercontent.com/Brumbelow/pyinc/v3.1.2/docs/guide.md",
    ),
)
def test_current_version_links_validate_local_targets_before_tag_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pyinc"\nversion = "3.1.2"\n',
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Guide\n", encoding="utf-8")
    monkeypatch.setattr(check_external_links, "_open", _http_404)

    assert check_external_links.check_link(Link(tmp_path / "README.md", target), tmp_path) is None


@pytest.mark.parametrize(
    "target",
    (
        "https://github.com/Brumbelow/pyinc/blob/v3.1.1/docs/guide.md",
        "https://github.com/Brumbelow/pyinc/blob/v3.1.2/docs/missing.md",
        "https://github.com/example/pyinc/blob/v3.1.2/docs/guide.md",
    ),
)
def test_local_fallback_does_not_hide_wrong_versions_paths_or_repositories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pyinc"\nversion = "3.1.2"\n',
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Guide\n", encoding="utf-8")
    monkeypatch.setattr(check_external_links, "_open", _http_404)

    error = check_external_links.check_link(Link(tmp_path / "README.md", target), tmp_path)
    assert error is not None
    assert "HTTP 404" in error
