from __future__ import annotations

from pathlib import Path

import pytest

from pyinc import Database, InMemoryArtifactStore
from pyinc.integrations import SourcePosition, SourceRange
from pyinc.integrations.requirements_txt import (
    _requirements_cutoff_token,
    deep_requirements_analysis,
    requirements_analysis,
    requirements_file_text,
    requirements_geometry_payload,
    requirements_payload,
    requirements_public_payload,
    workspace_requirements_analysis,
)

_MODES = ("strict", "checked", "fast")


@pytest.mark.parametrize("mode", _MODES)
def test_inline_comments_indentation_and_trailing_spaces_are_public_exactness(
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests>=2  # first\n", encoding="utf-8")
    warm_db = Database(mode=mode)
    before = requirements_analysis(warm_db, path)
    assert before.requirements[0].raw_line == "requests>=2  # first"
    assert before.requirements[0].range == SourceRange(
        SourcePosition(0, 0), SourcePosition(0, len("requests>=2  # first"))
    )

    updated = "  requests>=2  # second   \n"
    path.write_text(updated, encoding="utf-8")
    warm = requirements_analysis(warm_db, path)
    fresh = requirements_analysis(Database(mode=mode), path)

    assert warm == fresh
    assert warm != before
    assert warm.requirements[0].raw_line == updated[:-1]
    assert warm.requirements[0].range == SourceRange(
        SourcePosition(0, 2), SourcePosition(0, len(updated) - 1)
    )
    assert warm_db.inspect(requirements_file_text, str(path)).last_recompute == "executed"
    assert warm_db.inspect(requirements_payload, str(path)).last_recompute == "executed"
    assert warm_db.inspect(requirements_geometry_payload, str(path)).last_recompute == "executed"
    assert warm_db.inspect(requirements_public_payload, str(path)).last_recompute == "executed"


@pytest.mark.parametrize("mode", _MODES)
def test_continuation_backslashes_and_internal_line_endings_are_retained(
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "requirements.txt"
    initial = "requests==2 \\\r\n    --hash=sha256:aaaa\r\n"
    path.write_bytes(initial.encode())
    warm_db = Database(mode=mode)
    before = requirements_analysis(warm_db, path)

    requirement = before.requirements[0]
    assert requirement.raw_line == initial[:-2]
    assert requirement.version_spec == "==2"
    assert requirement.range == SourceRange(
        SourcePosition(0, 0), SourcePosition(1, len("    --hash=sha256:aaaa"))
    )

    updated = "requests==2 \\\n  --hash=sha256:bbbb  \n"
    path.write_text(updated, encoding="utf-8")
    warm = requirements_analysis(warm_db, path)
    fresh = requirements_analysis(Database(mode=mode), path)

    assert warm == fresh
    assert warm != before
    assert warm.requirements[0].raw_line == updated[:-1]
    assert warm.requirements[0].version_spec == "==2"
    assert warm.requirements[0].range == SourceRange(
        SourcePosition(0, 0), SourcePosition(1, len("  --hash=sha256:bbbb  "))
    )


@pytest.mark.parametrize("mode", _MODES)
def test_editable_index_reference_and_malformed_geometry_matches_fresh(
    mode: str,
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.in"
    child.write_text("click\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text(
        "-e .\n--index-url https://example.invalid/simple\n-r child.in\n!!! old malformed !!!\n",
        encoding="utf-8",
    )
    warm_db = Database(mode=mode)
    before = requirements_analysis(warm_db, path)

    path.write_text(
        "  --editable .  \n"
        "   --index-url https://example.invalid/simple   \n"
        "    --requirement child.in\n"
        "!!! new malformed !!!\n",
        encoding="utf-8",
    )
    warm = requirements_analysis(warm_db, path)
    fresh = requirements_analysis(Database(mode=mode), path)

    assert warm == fresh
    assert warm != before
    assert warm.requirements[0].is_editable
    assert warm.requirements[0].raw_line == "  --editable .  "
    assert warm.requirements[0].range.start == SourcePosition(0, 2)
    assert warm.index_directives[0].range == SourceRange(
        SourcePosition(1, 3),
        SourcePosition(1, len("   --index-url https://example.invalid/simple   ")),
    )
    assert warm.file_references[0].range.start == SourcePosition(2, 4)
    assert warm.diagnostics == (("unparseable-line", "line 4: !!! new malformed !!!"),)


@pytest.mark.parametrize("mode", _MODES)
def test_deep_and_workspace_consumers_keep_exact_child_spelling(
    mode: str,
    tmp_path: Path,
) -> None:
    root = tmp_path / "requirements.txt"
    child = tmp_path / "child.in"
    root.write_text("-r child.in\n", encoding="utf-8")
    child.write_text("click>=8  # old\n", encoding="utf-8")
    warm_db = Database(mode=mode)
    before_deep = deep_requirements_analysis(warm_db, root)
    before_workspace = workspace_requirements_analysis(warm_db, tmp_path)
    assert before_workspace is not None
    assert before_deep.requirements[0].raw_line == "click>=8  # old"

    child.write_text("  click>=8  # new   \n", encoding="utf-8")
    warm_deep = deep_requirements_analysis(warm_db, root)
    warm_workspace = workspace_requirements_analysis(warm_db, tmp_path)
    fresh_db = Database(mode=mode)
    fresh_deep = deep_requirements_analysis(fresh_db, root)
    fresh_workspace = workspace_requirements_analysis(fresh_db, tmp_path)

    assert warm_deep == fresh_deep
    assert warm_workspace == fresh_workspace
    assert warm_deep != before_deep
    assert warm_workspace != before_workspace
    assert warm_deep.requirements[0].raw_line == "  click>=8  # new   "
    assert warm_deep.requirements[0].range == SourceRange(
        SourcePosition(0, 2), SourcePosition(0, len("  click>=8  # new   "))
    )


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_reload_cannot_reuse_old_raw_line_or_geometry(
    mode: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests>=2  # old\n", encoding="utf-8")
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    initial = requirements_analysis(writer, path)
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert requirements_analysis(reader, path) == initial

    path.write_text("   requests>=2  # new   \n", encoding="utf-8")
    warm = requirements_analysis(reader, path)
    fresh = requirements_analysis(Database(mode=mode), path)

    assert warm == fresh
    assert warm != initial
    assert warm.requirements[0].raw_line == "   requests>=2  # new   "
    assert warm.requirements[0].range == SourceRange(
        SourcePosition(0, 3), SourcePosition(0, len("   requests>=2  # new   "))
    )
    assert reader.inspect(requirements_file_text, str(path)).last_recompute == "executed"


def test_requirements_projection_is_exact_for_every_source_spelling() -> None:
    left = "requests>=2  # first\n"
    right = "  requests>=2  # second\n"
    assert _requirements_cutoff_token(left) == ("raw", left)
    assert _requirements_cutoff_token(right) == ("raw", right)
    assert _requirements_cutoff_token(left) != _requirements_cutoff_token(right)
