from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.check_docs import PROJECT_ROOT, check_docs, check_local_links
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_docs import PROJECT_ROOT, check_docs, check_local_links  # noqa: E402


def test_documentation_checker_accepts_repository() -> None:
    errors = check_docs(PROJECT_ROOT)
    assert not errors, "\n".join(errors)


def test_documentation_checker_reports_missing_anchor(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    target = tmp_path / "target.md"
    readme.write_text("# Root\n\n[bad](target.md#missing)\n", encoding="utf-8")
    target.write_text("# Present\n", encoding="utf-8")

    errors = check_local_links(tmp_path, (readme, target))

    assert len(errors) == 1
    assert "missing anchor #missing" in errors[0]


def test_documentation_checker_ignores_external_links(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Root\n\n[external](https://example.invalid/missing)\n", encoding="utf-8")

    assert check_local_links(tmp_path, (readme,)) == ()
