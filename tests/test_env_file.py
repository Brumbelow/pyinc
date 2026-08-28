from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database, FileSystemArtifactStore
from pyinc.integrations import SourcePosition, SourceRange
from pyinc.integrations.env_file import (
    EnvFileAnalysis,
    env_analysis,
    workspace_env_analysis,
)

_MINIMAL_ENV = """\
# Database config
DB_HOST=localhost
DB_PORT=5432
DB_NAME="myapp_dev"
SECRET_KEY='s3cr3t-value'
"""

_ENV_WITH_EXPORT = """\
export API_KEY=abc123
export DEBUG=true
VERBOSE=1
"""

_ENV_WITH_INTERPOLATION = """\
BASE_URL=https://example.com
API_URL=${BASE_URL}/api
"""

_ENV_WITH_INLINE_COMMENTS = """\
HOST=localhost # the host
PORT=8080 # the port
NAME="quoted value" # this comment is inside quotes? no
"""

# The same environment twice: the whole-line comment is reworded to a different
# length and the quote style changes, and every entry keeps its value and its
# source range through both.
_UNFORMATTED_ENV = "# aaa\nKEY=value\nOTHER='x'\n"
_REFORMATTED_ENV = '# a much longer comment body\nKEY=value\nOTHER="x"\n'


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_env_file_stable_api() -> None:
    assert "EnvEntry" in integrations.__all__
    assert "EnvFileAnalysis" in integrations.__all__
    assert "env_analysis" in integrations.__all__
    assert "workspace_env_analysis" in integrations.__all__

    assert hasattr(integrations, "env_analysis")
    assert hasattr(integrations, "workspace_env_analysis")
    assert hasattr(integrations, "EnvEntry")
    assert hasattr(integrations, "EnvFileAnalysis")

    # Experimental helpers must not leak.
    assert not hasattr(integrations, "env_file_text")
    assert not hasattr(integrations, "env_entries_payload")
    assert not hasattr(integrations, "env_analysis_payload")
    assert not hasattr(integrations, "env_diagnostics_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_extracts_entries(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_MINIMAL_ENV, encoding="utf-8")

    db = Database(mode=mode)
    result = env_analysis(db, str(path))

    assert isinstance(result, EnvFileAnalysis)
    assert result.path == str(path)

    keys = {e.key for e in result.entries}
    assert keys == {"DB_HOST", "DB_PORT", "DB_NAME", "SECRET_KEY"}

    # Check specific values
    by_key = {e.key: e for e in result.entries}
    assert by_key["DB_HOST"].value == "localhost"
    assert by_key["DB_HOST"].quoted is False
    assert by_key["DB_PORT"].value == "5432"
    assert by_key["DB_NAME"].value == "myapp_dev"
    assert by_key["DB_NAME"].quoted is True
    assert by_key["SECRET_KEY"].value == "s3cr3t-value"
    assert by_key["SECRET_KEY"].quoted is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_handles_export_prefix(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_ENV_WITH_EXPORT, encoding="utf-8")

    db = Database(mode=mode)
    result = env_analysis(db, str(path))

    keys = {e.key for e in result.entries}
    assert keys == {"API_KEY", "DEBUG", "VERBOSE"}

    by_key = {e.key: e for e in result.entries}
    assert by_key["API_KEY"].value == "abc123"
    assert by_key["DEBUG"].value == "true"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_flags_interpolation(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_ENV_WITH_INTERPOLATION, encoding="utf-8")

    db = Database(mode=mode)
    result = env_analysis(db, str(path))

    assert len(result.entries) == 2
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "interpolation-reference"
    assert "API_URL" in result.diagnostics[0][1]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_strips_inline_comments(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_ENV_WITH_INLINE_COMMENTS, encoding="utf-8")

    db = Database(mode=mode)
    result = env_analysis(db, str(path))

    by_key = {e.key: e for e in result.entries}
    assert by_key["HOST"].value == "localhost"
    assert by_key["PORT"].value == "8080"
    # Quoted values are not stripped of inline comments
    assert by_key["NAME"].value == "quoted value"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_invalid_lines(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("GOOD=value\nBAD LINE HERE\n", encoding="utf-8")

    db = Database(mode=mode)
    result = env_analysis(db, str(path))

    assert len(result.entries) == 1
    assert result.entries[0].key == "GOOD"
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "invalid-line"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_empty_file(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")

    db = Database(mode=mode)
    result = env_analysis(db, str(path))

    assert result.entries == ()
    assert result.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_missing_file(mode: str, tmp_path: Path) -> None:
    db = Database(mode=mode)
    result = env_analysis(db, str(tmp_path / "nonexistent.env"))

    assert result.entries == ()
    assert result.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_source_ranges(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_MINIMAL_ENV, encoding="utf-8")

    db = Database(mode=mode)
    result = env_analysis(db, str(path))

    by_key = {e.key: e for e in result.entries}
    assert by_key["DB_HOST"].range == SourceRange(SourcePosition(1, 0), SourcePosition(1, 17))
    assert by_key["DB_PORT"].range == SourceRange(SourcePosition(2, 0), SourcePosition(2, 12))
    assert by_key["DB_NAME"].range == SourceRange(SourcePosition(3, 0), SourcePosition(3, 19))
    assert by_key["SECRET_KEY"].range == SourceRange(SourcePosition(4, 0), SourcePosition(4, 25))


def test_env_analysis_range_preserves_unicode_codepoint_columns(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("  MESSAGE=\U0001f642value  \n", encoding="utf-8")

    entry = env_analysis(Database(), path).entries[0]

    assert entry.range == SourceRange(SourcePosition(0, 2), SourcePosition(0, 16))


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------


def test_workspace_env_analysis_discovers_env_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEY=value\n", encoding="utf-8")

    db = Database()
    result = workspace_env_analysis(db, str(tmp_path))

    assert result is not None
    assert len(result.entries) == 1
    assert result.entries[0].key == "KEY"


def test_workspace_env_analysis_returns_none_when_missing(tmp_path: Path) -> None:
    db = Database()
    result = workspace_env_analysis(db, str(tmp_path))
    assert result is None


def test_workspace_env_analysis_custom_filename(tmp_path: Path) -> None:
    path = tmp_path / ".env.local"
    path.write_text("LOCAL_KEY=local_value\n", encoding="utf-8")

    db = Database()
    result = workspace_env_analysis(db, str(tmp_path), filename=".env.local")

    assert result is not None
    assert result.entries[0].key == "LOCAL_KEY"


# ---------------------------------------------------------------------------
# Backdating
# ---------------------------------------------------------------------------


def test_trailing_comment_edit_backdates_env(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEY=value\n# old comment\n", encoding="utf-8")

    db = Database()
    first = env_analysis(db, str(path))

    # Change comment text after the entry — source range unchanged
    path.write_text("KEY=value\n# new comment\n", encoding="utf-8")
    second = env_analysis(db, str(path))

    assert first == second


def test_comment_shift_does_not_backdate_env(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEY=value\n", encoding="utf-8")

    db = Database()
    first = env_analysis(db, str(path))

    # Prepend a comment — shifts the source range, so this must not backdate.
    path.write_text("# new comment\nKEY=value\n", encoding="utf-8")
    second = env_analysis(db, str(path))

    assert first.entries[0].range != second.entries[0].range


def test_semantic_edit_invalidates_env(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEY=old_value\n", encoding="utf-8")

    db = Database()
    first = env_analysis(db, str(path))

    path.write_text("KEY=new_value\n", encoding="utf-8")
    second = env_analysis(db, str(path))

    assert first.entries[0].value != second.entries[0].value


def test_diagnostic_only_edit_invalidates_env(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("GOOD=value\nBAD LINE\n", encoding="utf-8")
    db = Database(mode="strict")
    assert env_analysis(db, path).diagnostics[0][1].startswith("line 2:")

    path.write_text("GOOD=value\n# moved diagnostic\nBAD LINE\n", encoding="utf-8")
    incremental = env_analysis(db, path)
    fresh = env_analysis(Database(mode="strict"), path)

    assert incremental == fresh
    assert incremental.diagnostics[0][1].startswith("line 3:")


# ---------------------------------------------------------------------------
# Reformatting costs nothing above the payloads
# ---------------------------------------------------------------------------

# The two queries that re-read the text and re-derive a projection of it.
_PAYLOAD_QUERIES = ("env_entries_payload", "env_diagnostics_payload")


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reformat_recomputes_the_payloads_and_backdates_the_composition(
    mode: str, tmp_path: Path
) -> None:
    # There are two comment classes here and only one of them is narrow. A
    # whole-line comment is skipped before anything is derived from it, so
    # rewording its body reaches nothing at any length -- which is why this edit
    # rewrites one to a different length. An inline comment is part of its
    # entry's own raw line, and the range end is that line's length, so there
    # only an equal-length rewrite leaves the entry where it was. Adding or
    # removing a comment line is a third thing again: it moves every following
    # entry's line number, and the analysis moves with it.
    path = tmp_path / ".env"
    path.write_text(_UNFORMATTED_ENV, encoding="utf-8")

    db = Database(mode=mode)
    first = env_analysis(db, str(path))

    db.reset_statistics()
    path.write_text(_REFORMATTED_ENV, encoding="utf-8")
    second = env_analysis(db, str(path))

    assert first == second, f"a reformat moved the analysis | first {first} | second {second}"

    # `query_profile()` records executions only, and `reset_statistics()` has
    # just cleared it, so a query that was reused has no row at all -- there is
    # no row carrying a zero to look for. Labels also carry an argument-hash
    # suffix, so a lookup by bare query name never matches; match by substring.
    executed = [profile.query_label for profile in db.query_profile()]
    for name in _PAYLOAD_QUERIES:
        assert any(name in label for label in executed), (
            f"{name} did not re-run | executed {executed}"
        )
    assert not [label for label in executed if "env_analysis_payload" in label], (
        f"env_analysis_payload re-ran instead of backdating | executed {executed}"
    )

    # Absolute counts for the second read, not deltas: the read executes on the
    # new bytes, the two payload queries re-derive equal projections and are
    # backdated, and everything above them is reused. The reuse figure is what
    # the reformat is supposed to cost nothing on.
    statistics = db.statistics()
    counts = (
        statistics.query_executions,
        statistics.query_backdates,
        statistics.query_reuses,
    )
    assert counts == (1, 2, 4), f"work counts moved | exec/backdate/reuse {counts}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reformat_leaves_workspace_discovery_identical(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_UNFORMATTED_ENV, encoding="utf-8")

    db = Database(mode=mode)
    first = workspace_env_analysis(db, str(tmp_path))

    path.write_text(_REFORMATTED_ENV, encoding="utf-8")
    second = workspace_env_analysis(db, str(tmp_path))

    assert first is not None, "discovery found no file to analyse"
    assert first == second, f"a reformat moved discovery | first {first} | second {second}"


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

# The ordering other integrations use -- edit, drive the entrypoint so a stale
# answer forms, then save -- is unconstructible here: this read compares the text
# it hands back, so there is no answer that disagrees with the file to save. The
# substitute edits the file after the save, which the reload has to notice. The
# second arm is what keeps that honest: with no edit the saved answer and a fresh
# one agree, and the row would pass on any tree at all.


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_checkpoint_reload_answers_an_edit_made_after_the_save(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / ".env"
    path.write_text(_UNFORMATTED_ENV, encoding="utf-8")

    store_dir = tmp_path / "store"
    saver = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    warm = env_analysis(saver, str(path))
    key = saver.save_checkpoint()

    path.write_text("# aaa\nKEY=changed\nOTHER='x'\n", encoding="utf-8")

    reloaded = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    reloaded.load_checkpoint(key)

    restored = env_analysis(reloaded, str(path))
    fresh = env_analysis(Database(mode=mode), str(path))

    assert restored == fresh, f"reloaded != fresh | reloaded {restored} | fresh {fresh}"
    assert restored != warm, f"the edit never reached the reload | warm {warm}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_checkpoint_reload_over_an_unchanged_file_runs_nothing(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / ".env"
    path.write_text(_UNFORMATTED_ENV, encoding="utf-8")

    store_dir = tmp_path / "store"
    saver = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    warm = env_analysis(saver, str(path))
    key = saver.save_checkpoint()

    reloaded = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    reloaded.load_checkpoint(key)
    restored = env_analysis(reloaded, str(path))

    assert restored == warm, f"the saved answer did not survive | warm {warm}"

    statistics = reloaded.statistics()
    counts = (
        statistics.query_executions,
        statistics.query_reuses,
        statistics.resource_loads,
        statistics.resource_probe_hits,
    )
    assert counts == (0, 1, 0, 1), f"the reload did work | exec/reuse/loads/probes {counts}"


# ---------------------------------------------------------------------------
# From-scratch consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_env_analysis_matches_fresh_recomputation(mode: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"

    steps: tuple[tuple[str, str], ...] = (
        ("initial", "DB_HOST=localhost\nDB_PORT=5432\n"),
        ("add comment", "# database\nDB_HOST=localhost\nDB_PORT=5432\n"),
        ("change value", "DB_HOST=remotehost\nDB_PORT=5432\n"),
        ("add key", "DB_HOST=localhost\nDB_PORT=5432\nDB_NAME=mydb\n"),
        ("remove key", "DB_HOST=localhost\n"),
        ("add export", "export DB_HOST=localhost\n"),
        ("add quotes", 'DB_HOST="localhost"\n'),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert env_analysis(incremental, str(path)) == env_analysis(fresh, str(path))
