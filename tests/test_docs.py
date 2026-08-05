from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.check_docs import (
        PROJECT_ROOT,
        check_docs,
        check_local_links,
        check_public_claims,
        check_release_assurance_gate,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_docs import (  # noqa: E402
        PROJECT_ROOT,
        check_docs,
        check_local_links,
        check_public_claims,
        check_release_assurance_gate,
    )


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


def test_public_claim_scan_rejects_unqualified_release_claims(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        """\
pyinc is the first Python incremental engine.
pyinc is the only incremental framework.
pyinc offers a unique incremental runtime.
Evaluation is always safe and has zero overhead.
The demo provides full provenance and is byte-for-byte verified.
""",
        encoding="utf-8",
    )

    errors = check_public_claims(tmp_path, (readme,))

    assert len(errors) == 7
    assert any("pyinc is the first" in error for error in errors)
    assert any("pyinc is the only" in error for error in errors)
    assert any("unique" in error for error in errors)
    assert any("always safe" in error for error in errors)
    assert any("zero overhead" in error for error in errors)
    assert any("full provenance" in error for error in errors)
    assert any("byte-for-byte verified" in error for error in errors)


def test_public_claim_scan_accepts_explicit_qualification(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        """\
pyinc is not the first Python incremental engine.
The disabled branch has zero overhead when the compiler removes it.
Misleading \"always safe\" wording was removed.
""",
        encoding="utf-8",
    )

    assert check_public_claims(tmp_path, (readme,)) == ()


def test_release_assurance_version_and_automatic_publication_gate_are_checked(
    tmp_path: Path,
) -> None:
    (tmp_path / "release").mkdir()
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pyinc"\nversion = "3.1.2"\n', encoding="utf-8"
    )
    (tmp_path / "release/assurance.json").write_text(
        '{"schema_version": 1, "version": "3.1.1"}\n', encoding="utf-8"
    )
    (tmp_path / ".github/workflows/release.yml").write_text("", encoding="utf-8")
    (tmp_path / ".github/workflows/published-artifacts.yml").write_text("", encoding="utf-8")

    errors = check_release_assurance_gate(tmp_path)

    assert any("version must match" in error for error in errors)
    assert any("check_release_assurance.py" in error for error in errors)
    assert any("missing pre-tag candidate workflow" in error for error in errors)
    assert any("automatic post-publication gate" in error for error in errors)


def test_release_assurance_checker_rejects_an_incomplete_pre_tag_gate(tmp_path: Path) -> None:
    (tmp_path / "release").mkdir()
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pyinc"\nversion = "3.1.2"\n', encoding="utf-8"
    )
    (tmp_path / "release/assurance.json").write_text(
        '{"schema_version": 2, "version": "3.1.2"}\n', encoding="utf-8"
    )
    (workflows / "release.yml").write_text(
        "\n".join(
            (
                "python scripts/check_release_assurance.py",
                "gh issue list --state open --label",
                "gh run list",
                "--workflow release-candidate.yml",
                '--commit "$GITHUB_SHA"',
                "--event workflow_dispatch",
                "--status success",
                'test "$count" -gt 0',
            )
        ),
        encoding="utf-8",
    )
    (workflows / "release-candidate.yml").write_text(
        "on:\n  workflow_dispatch:\n", encoding="utf-8"
    )
    (workflows / "published-artifacts.yml").write_text(
        "release:\ntypes:\n- published\nverify-published\n", encoding="utf-8"
    )

    errors = check_release_assurance_gate(tmp_path)

    assert any("release-candidate.yml" in error and "ci.yml" in error for error in errors)
    assert any(
        "release-candidate.yml" in error and "REQUESTED_RELEASE_COMMIT" in error
        for error in errors
    )
    assert any(
        "release-candidate.yml" in error and "verify_expected_signature" in error
        for error in errors
    )
