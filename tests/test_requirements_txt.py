from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from _hostile_paths import (
    make_symlink_loop,
    nul_path,
    posix_only,
    skip_without_posix_permissions,
)
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

import pyinc.integrations as integrations
from pyinc import Database, InMemoryArtifactStore, UnsupportedValueError
from pyinc._safe_fs import UnsafeFilesystemPathError
from pyinc.integrations import requirements_txt as requirements_module
from pyinc.integrations.requirements_txt import (
    RequirementsAnalysis,
    deep_requirements_analysis,
    requirements_analysis,
    workspace_requirements_analysis,
)
from pyinc.resources import ResolvedPathResource

Operation = tuple[Literal["write", "delete"], str, str | None]

_MINIMAL_REQUIREMENTS = """\
# Core dependencies
requests>=2.28,<3.0
click==8.1.7
flask[async]>=2.3
numpy  # numerical computing

# Platform-specific
pywin32; sys_platform == "win32"

# Editable
-e .

# References
-r dev-requirements.txt
-c constraints.txt

# Index
--index-url https://pypi.org/simple/
--extra-index-url https://internal.example.com/simple/
--find-links /local/wheels
"""


@pytest.mark.parametrize(
    "requirement_text",
    [
        "requests>=2.28,<3",
        "Flask[async,dotenv]~=3.0",
        'typing-extensions>=4; python_version < "3.12"',
        "demo @ https://example.invalid/demo-1.0.tar.gz",
        "zope.interface!=6.0,>=5.0",
    ],
)
def test_supported_requirement_vectors_match_packaging(
    tmp_path: Path, requirement_text: str
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(f"{requirement_text}\n", encoding="utf-8")
    parsed = requirements_analysis(Database(), path).requirements
    oracle = Requirement(requirement_text)

    assert len(parsed) == 1
    actual = parsed[0]
    assert actual.name == canonicalize_name(oracle.name).replace("-", "_")
    assert set(actual.extras) == set(oracle.extras)
    assert actual.markers == (str(oracle.marker) if oracle.marker is not None else "")
    if oracle.url is not None:
        assert actual.version_spec == f"@ {oracle.url}"
    else:
        assert SpecifierSet(actual.version_spec) == oracle.specifier


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_requirements_txt_stable_api() -> None:
    assert "RequirementRef" in integrations.__all__
    assert "RequirementsAnalysis" in integrations.__all__
    assert "FileReference" in integrations.__all__
    assert "IndexDirective" in integrations.__all__
    assert "requirements_analysis" in integrations.__all__
    assert "workspace_requirements_analysis" in integrations.__all__
    assert hasattr(integrations, "requirements_analysis")
    assert hasattr(integrations, "workspace_requirements_analysis")
    assert hasattr(integrations, "RequirementsAnalysis")
    # Experimental helpers stay private.
    assert not hasattr(integrations, "requirements_file_text")
    assert not hasattr(integrations, "requirements_payload")
    assert not hasattr(integrations, "requirements_analysis_payload")
    assert not hasattr(integrations, "file_references_payload")
    assert not hasattr(integrations, "index_directives_payload")
    assert not hasattr(integrations, "requirements_diagnostics_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_requirements_analysis_extracts_packages(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(_MINIMAL_REQUIREMENTS, encoding="utf-8")

    db = Database(mode=mode)
    result = requirements_analysis(db, str(path))

    assert isinstance(result, RequirementsAnalysis)
    assert result.path == str(path)

    names = {r.name for r in result.requirements}
    assert "requests" in names
    assert "click" in names
    assert "flask" in names
    assert "numpy" in names
    assert "pywin32" in names

    assert len(result.file_references) == 2
    assert len(result.index_directives) == 3
    assert result.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_requirements_analysis_reports_diagnostics_for_unparseable_lines(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests>=2.0\n!!! bad line !!!\nclick\n", encoding="utf-8")

    db = Database(mode=mode)
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 2
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "unparseable-line"


def test_deep_requirements_reports_nul_reference_without_path_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_bytes(b"-r invalid\x00name.in\nrequests>=2\n")

    result = deep_requirements_analysis(Database(), path)

    assert tuple(requirement.name for requirement in result.requirements) == ("requests",)
    assert result.file_references == ()
    assert result.diagnostics == (("unparseable-line", "line 1: -r invalid\x00name.in"),)


# ---------------------------------------------------------------------------
# Specific correctness
# ---------------------------------------------------------------------------


def test_requirements_analysis_parses_version_specifiers(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "alpha>=2.0,<3.0\nbeta==1.0.0\ngamma~=1.4.0\ndelta!=2.0\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    by_name = {r.name: r for r in result.requirements}
    assert by_name["alpha"].version_spec == ">=2.0,<3.0"
    assert by_name["beta"].version_spec == "==1.0.0"
    assert by_name["gamma"].version_spec == "~=1.4.0"
    assert by_name["delta"].version_spec == "!=2.0"


def test_requirements_analysis_parses_extras(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests[security,socks]>=2.0\n", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 1
    req = result.requirements[0]
    assert req.name == "requests"
    assert req.extras == ("security", "socks")
    assert req.version_spec == ">=2.0"


def test_requirements_analysis_parses_environment_markers(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text('pywin32; sys_platform == "win32"\n', encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 1
    req = result.requirements[0]
    assert req.name == "pywin32"
    assert req.markers == 'sys_platform == "win32"'


def test_requirements_analysis_parses_editable_installs(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("-e .\n-e git+https://github.com/example/pkg.git\n", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 2
    assert all(r.is_editable for r in result.requirements)


def test_requirements_analysis_parses_file_references(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "-r other.txt\n--requirement base.txt\n-c constraints.txt\n--constraint pins.txt\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.file_references) == 4
    kinds = [r.kind for r in result.file_references]
    assert kinds.count("requirement") == 2
    assert kinds.count("constraint") == 2
    paths = [r.path for r in result.file_references]
    assert "other.txt" in paths
    assert "base.txt" in paths
    assert "constraints.txt" in paths
    assert "pins.txt" in paths


def test_recursive_file_references_strip_inline_comments(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    leaf = nested / "leaf.in"
    leaf.write_text("leaf-package==1\n", encoding="utf-8")
    base = tmp_path / "base.in"
    base.write_text("-r nested/leaf.in # nested include\nbase-package==2\n", encoding="utf-8")
    constraints = tmp_path / "constraints.in"
    constraints.write_text("base-package<3\n", encoding="utf-8")
    main = tmp_path / "requirements.txt"
    main.write_text(
        "-r base.in  # shared requirements\n--constraint constraints.in # deployment pins\n",
        encoding="utf-8",
    )

    shallow = requirements_analysis(Database(), main)
    assert [(item.kind, item.path) for item in shallow.file_references] == [
        ("requirement", "base.in"),
        ("constraint", "constraints.in"),
    ]
    deep = deep_requirements_analysis(Database(), main)
    assert {item.name for item in deep.requirements} == {
        "base_package",
        "leaf_package",
    }
    assert deep.diagnostics == ()


def test_requirements_analysis_parses_index_directives(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "--index-url https://pypi.org/simple/\n"
        "--extra-index-url https://internal.example.com/simple/\n"
        "-f /local/wheels\n"
        "--find-links https://download.example.com/\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.index_directives) == 4
    kinds = [d.kind for d in result.index_directives]
    assert "index-url" in kinds
    assert "extra-index-url" in kinds
    assert kinds.count("find-links") == 2


def test_requirements_analysis_handles_line_continuations(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "requests\\\n>=2.0\nclick\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    by_name = {r.name: r for r in result.requirements}
    assert "requests" in by_name
    assert by_name["requests"].version_spec == ">=2.0"
    assert "click" in by_name
    assert by_name["requests"].range.start.line == 0
    assert by_name["requests"].range.end.line == 1
    assert by_name["click"].range.start.line == 2


def test_requirements_analysis_splits_hash_options_from_pip_compile_lines(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "requests==2.31.0 \\\n"
        "    --hash=sha256:aaaa \\\n"
        "    --hash=sha256:bbbb\n"
        'pywin32==306 ; sys_platform == "win32" --hash=sha256:cccc\n',
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    by_name = {r.name: r for r in result.requirements}
    assert by_name["requests"].version_spec == "==2.31.0"
    assert by_name["requests"].markers == ""
    assert by_name["pywin32"].version_spec == "==306"
    assert by_name["pywin32"].markers == 'sys_platform == "win32"'
    assert result.diagnostics == ()


def test_requirements_analysis_handles_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert result.requirements == ()
    assert result.file_references == ()
    assert result.index_directives == ()
    assert result.diagnostics == ()


def test_requirements_analysis_handles_comments_only(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("# Just a comment\n# Another comment\n\n", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert result.requirements == ()
    assert result.diagnostics == ()


def test_requirements_analysis_on_nonexistent_file(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.txt"

    db = Database()
    result = requirements_analysis(db, str(path))

    assert result.requirements == ()
    assert result.file_references == ()
    assert result.index_directives == ()
    assert result.diagnostics == ()


def test_requirements_analysis_normalizes_package_names(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("Requests>=2.0\nmy-package==1.0\nAnother.Pkg>=3.0\n", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    names = {r.name for r in result.requirements}
    assert "requests" in names
    assert "my_package" in names
    assert "another_pkg" in names


def test_requirements_analysis_parses_url_requirements(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "mypackage @ https://example.com/mypackage-1.0.tar.gz\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 1
    req = result.requirements[0]
    assert req.name == "mypackage"
    assert "https://example.com/mypackage-1.0.tar.gz" in req.version_spec


# ---------------------------------------------------------------------------
# Backdating
# ---------------------------------------------------------------------------


def test_comment_text_edit_backdates_requirements(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("# Original comment\nrequests>=2.0\nclick\n", encoding="utf-8")

    db = Database()
    first = requirements_analysis(db, str(path))

    # Reword the comment. Line count and requirement positions stay the same.
    path.write_text("# Different comment\nrequests>=2.0\nclick\n", encoding="utf-8")
    second = requirements_analysis(db, str(path))

    assert first == second


def test_semantic_edit_invalidates_downstream_requirements(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests>=2.0\nclick\n", encoding="utf-8")

    db = Database()
    first = requirements_analysis(db, str(path))
    assert len(first.requirements) == 2

    # Replace a dependency (a semantic edit).
    path.write_text("httpx>=0.24\nclick\n", encoding="utf-8")
    second = requirements_analysis(db, str(path))

    names = {r.name for r in second.requirements}
    assert "httpx" in names
    assert "requests" not in names
    assert first != second


# ---------------------------------------------------------------------------
# Reads that must answer with the text they compared
# ---------------------------------------------------------------------------

# One document with every line kind whose public output uses text that a
# line-normalizing comparison discards: an index directive, a plain requirement,
# an editable install, a line the parser rejects, and a two-line requirement
# joined by a continuation backslash.
_SEQUENCE_START = (
    "--index-url https://example.com/simple  # primary\n"
    "requests>=2.0  # http client\n"
    "-e ./pkg  # local\n"
    "this is not a requirement  # junk\n"
    "flask \\\n"
    "==2.0\n"
)

# Seven edits, each changing one thing from the state above it. The first four
# reword the inline comment on each line kind. The next two move only
# whitespace. The last puts a single space after the continuation backslash,
# which ends the continuation.
#
# The requirement-evaluation suite imports this to drive the other two
# entrypoints over it. One shared definition keeps the two rows in sync.
_EDIT_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("initial", _SEQUENCE_START),
    (
        "reword the comment on a requirement",
        "--index-url https://example.com/simple  # primary\n"
        "requests>=2.0  # the http client we use\n"
        "-e ./pkg  # local\n"
        "this is not a requirement  # junk\n"
        "flask \\\n"
        "==2.0\n",
    ),
    (
        "reword the comment on an index directive",
        "--index-url https://example.com/simple  # the primary index\n"
        "requests>=2.0  # the http client we use\n"
        "-e ./pkg  # local\n"
        "this is not a requirement  # junk\n"
        "flask \\\n"
        "==2.0\n",
    ),
    (
        "reword the comment on an editable install",
        "--index-url https://example.com/simple  # the primary index\n"
        "requests>=2.0  # the http client we use\n"
        "-e ./pkg  # the local package\n"
        "this is not a requirement  # junk\n"
        "flask \\\n"
        "==2.0\n",
    ),
    (
        "reword the comment on an unparseable line",
        "--index-url https://example.com/simple  # the primary index\n"
        "requests>=2.0  # the http client we use\n"
        "-e ./pkg  # the local package\n"
        "this is not a requirement  # not a requirement at all\n"
        "flask \\\n"
        "==2.0\n",
    ),
    (
        "indent a requirement",
        "--index-url https://example.com/simple  # the primary index\n"
        "    requests>=2.0  # the http client we use\n"
        "-e ./pkg  # the local package\n"
        "this is not a requirement  # not a requirement at all\n"
        "flask \\\n"
        "==2.0\n",
    ),
    (
        "add trailing whitespace",
        "--index-url https://example.com/simple  # the primary index\n"
        "    requests>=2.0  # the http client we use\n"
        "-e ./pkg  # the local package   \n"
        "this is not a requirement  # not a requirement at all\n"
        "flask \\\n"
        "==2.0\n",
    ),
    (
        "put a space after the continuation backslash",
        "--index-url https://example.com/simple  # the primary index\n"
        "    requests>=2.0  # the http client we use\n"
        "-e ./pkg  # the local package   \n"
        "this is not a requirement  # not a requirement at all\n"
        "flask \\ \n"
        "==2.0\n",
    ),
)


def _assert_ranges_span_their_raw_lines(analysis: RequirementsAnalysis, where: str) -> None:
    """Check that each one-line, unindented requirement's range measures its raw_line.

    The range and the raw_line come from two different reads of the same file,
    so this checks one answer for internal coherence and needs only one
    database. It covers requirements that start at column zero and end on the
    line they start on, where the arithmetic is exact. A continued or indented
    line has a range that legitimately spans more than its stripped text.
    """
    for req in analysis.requirements:
        if req.range.start.line != req.range.end.line or req.range.start.character != 0:
            continue
        span = req.range.end.character - req.range.start.character
        assert span == len(req.raw_line), (
            f"range spans {span} characters but raw_line is {len(req.raw_line)} | "
            f"raw_line={req.raw_line!r} | name={req.name} | {where}"
        )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_every_requirements_surface_matches_a_fresh_read_across_the_edit_sequence(
    mode: str, tmp_path: Path
) -> None:
    """Every requirements entrypoint answers each edit the way a fresh database does.

    Five entrypoints consume this parse. The three in this module are driven
    here. The two evaluation surfaces are driven in the requirement-evaluation
    suite, which owns the environment they need and imports _EDIT_SEQUENCE from
    this module. All five are asserted at every step.
    """
    root = tmp_path / "ws"
    root.mkdir()
    path = root / "requirements.txt"

    incremental = Database(mode=mode)
    for label, content in _EDIT_SEQUENCE:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        for name, warm_value, fresh_value in (
            (
                "requirements_analysis",
                requirements_analysis(incremental, str(path)),
                requirements_analysis(fresh, str(path)),
            ),
            (
                "deep_requirements_analysis",
                deep_requirements_analysis(incremental, str(path)),
                deep_requirements_analysis(fresh, str(path)),
            ),
            (
                "workspace_requirements_analysis",
                workspace_requirements_analysis(incremental, str(root)),
                workspace_requirements_analysis(fresh, str(root)),
            ),
        ):
            assert warm_value == fresh_value, (
                f"{name} disagrees with a fresh read | after: {label} | mode={mode}"
            )
            # Both sides must have answered. Workspace discovery returns None
            # when it finds no requirements.txt, and None == None would pass the
            # comparison above even if neither surface read a file.
            assert fresh_value is not None, (
                f"{name} returned nothing to compare | after: {label} | mode={mode}"
            )
    # The range-against-raw_line check lives in the warm-database row below,
    # test_a_requirement_range_spans_exactly_its_own_raw_line, because staleness
    # is what breaks it and a fresh database is never stale.


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_requirement_range_spans_exactly_its_own_raw_line(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("flask==2.0  # web framework\n", encoding="utf-8")

    db = Database(mode=mode)
    requirements_analysis(db, str(path))

    # Reword the comment so the line grows by six characters. The requirement
    # itself stays the same, so a line-normalizing comparison would absorb this
    # edit. The range would then be measured on the new file while raw_line
    # carried over from the old one, giving 27 characters of text under a
    # 33-character span. Both now come from one read, so the arithmetic below
    # holds.
    path.write_text("flask==2.0  # small web framework\n", encoding="utf-8")

    _assert_ranges_span_their_raw_lines(
        requirements_analysis(db, str(path)), "comment reworded six characters longer"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_space_after_a_continuation_backslash_ends_the_logical_line(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("flask \\\n==2.0\n", encoding="utf-8")

    db = Database(mode=mode)
    requirements_analysis(db, str(path))

    # A backslash followed by a space ends the continuation, so the two physical
    # lines become separate. Three public fields move at once (the specifier, the
    # raw line and the diagnostics), yet a comparison that strips whitespace
    # treats the two files as equal.
    path.write_text("flask \\ \n==2.0\n", encoding="utf-8")

    warm = requirements_analysis(db, str(path))
    fresh = requirements_analysis(Database(mode=mode), str(path))

    assert warm == fresh, (
        f"a space after the continuation backslash reads differently warm | mode={mode} | "
        f"warm={[(r.name, r.version_spec, r.raw_line) for r in warm.requirements]} "
        f"diagnostics={warm.diagnostics}"
    )
    assert [(r.name, r.version_spec, r.raw_line) for r in fresh.requirements] == [
        ("flask", "\\", "flask \\")
    ]
    assert fresh.diagnostics == (("unparseable-line", "line 2: ==2.0"),)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reloaded_requirements_checkpoint_answers_like_a_fresh_database(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("flask==2.0  # web framework\n", encoding="utf-8")

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    requirements_analysis(saver, str(path))
    deep_requirements_analysis(saver, str(path))

    # The edit lands BEFORE the save and both entrypoints run again, so the
    # checkpoint holds the state the database reached by answering after the
    # edit. Saving before the edit would checkpoint a database that never
    # answered from a stale comparison, and the row would always pass.
    path.write_text("flask==2.0  # small web framework\n", encoding="utf-8")
    requirements_analysis(saver, str(path))
    deep_requirements_analysis(saver, str(path))
    key = saver.save_checkpoint()

    reloaded = Database(mode=mode, store=store)
    reloaded.load_checkpoint(key)
    fresh = Database(mode=mode)

    for name, entrypoint in (
        ("requirements_analysis", requirements_analysis),
        ("deep_requirements_analysis", deep_requirements_analysis),
    ):
        restored = entrypoint(reloaded, str(path))
        expected = entrypoint(fresh, str(path))
        assert restored == expected, (
            f"{name} from a reloaded checkpoint disagrees with a fresh read | mode={mode} | "
            f"restored raw_lines={[r.raw_line for r in restored.requirements]} | "
            f"expected raw_lines={[r.raw_line for r in expected.requirements]}"
        )

    # The reload covers the durable half of the bug. A database that answered
    # from a stale comparison would pair the file's current text with a raw_line
    # from before the edit, and a checkpoint would carry that pairing past a
    # restart. So the check runs here as well as on the live database.
    _assert_ranges_span_their_raw_lines(
        requirements_analysis(reloaded, str(path)), "reloaded from a checkpoint"
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_workspace_requirements_analysis_discovers_requirements_txt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    reqs = root / "requirements.txt"
    reqs.write_text("requests>=2.0\n", encoding="utf-8")

    db = Database()
    result = workspace_requirements_analysis(db, str(root))
    assert result is not None
    assert isinstance(result, RequirementsAnalysis)
    assert len(result.requirements) == 1


def test_workspace_requirements_analysis_returns_none_when_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    db = Database()
    result = workspace_requirements_analysis(db, str(root))
    assert result is None


# ---------------------------------------------------------------------------
# From-scratch oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_requirements_analysis_matches_fresh_recomputation_over_changes(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "requirements.txt"
    steps: tuple[tuple[str, str], ...] = (
        ("initial", "# deps\nrequests>=2.0\nclick\n"),
        ("change comment", "# updated deps\nrequests>=2.0\nclick\n"),
        ("change dep", "# updated deps\nhttpx>=0.24\nclick\n"),
        ("add extras", "httpx[http2]>=0.24\nclick\nflask[async]\n"),
        ("remove line", "click\nflask[async]\n"),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert requirements_analysis(incremental, str(path)) == requirements_analysis(
            fresh, str(path)
        )


# ---------------------------------------------------------------------------
# deep_requirements_analysis
# ---------------------------------------------------------------------------


def test_deep_single_file_matches_shallow(tmp_path: Path) -> None:
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("requests>=2.0\nflask\n")
    db = Database()
    shallow = requirements_analysis(db, str(req_file))
    deep = deep_requirements_analysis(db, str(req_file))
    assert len(deep.requirements) == len(shallow.requirements)
    names = {r.name for r in deep.requirements}
    assert names == {"requests", "flask"}


def test_deep_two_level_chain(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    base.write_text("numpy>=1.20\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\npandas>=1.0\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"numpy", "pandas"}


def test_deep_three_level_chain(tmp_path: Path) -> None:
    core = tmp_path / "core.txt"
    core.write_text("click\n")
    base = tmp_path / "base.txt"
    base.write_text("-r core.txt\nflask\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\ngunicorn\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"click", "flask", "gunicorn"}


def test_deep_circular_reference_produces_diagnostic(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("-r b.txt\nrequests\n")
    b.write_text("-r a.txt\nflask\n")
    db = Database()
    result = deep_requirements_analysis(db, str(a))
    cycle_diagnostics = [d for d in result.diagnostics if d[0] == "cycle"]
    assert len(cycle_diagnostics) >= 1
    names = {r.name for r in result.requirements}
    assert "requests" in names
    assert "flask" in names


def test_deep_duplicate_dedup_last_wins(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    base.write_text("requests>=1.0\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\nrequests>=2.0\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    req_names = [r.name for r in result.requirements]
    assert req_names.count("requests") == 1
    req = [r for r in result.requirements if r.name == "requests"][0]
    assert "2.0" in req.version_spec


def test_deep_relative_path_resolution(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    leaf = sub / "leaf.txt"
    leaf.write_text("boto3\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r sub/leaf.txt\ndjango\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"boto3", "django"}


def test_deep_constraint_not_followed(tmp_path: Path) -> None:
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("requests<3.0\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-c constraints.txt\nrequests\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"requests"}
    assert len(result.file_references) == 1
    assert result.file_references[0].kind == "constraint"


def test_deep_missing_referenced_file(tmp_path: Path) -> None:
    main = tmp_path / "requirements.txt"
    main.write_text("-r nonexistent.txt\nflask\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    assert "flask" in {r.name for r in result.requirements}


def test_deep_incremental_revalidation(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    base.write_text("numpy>=1.0\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\npandas\n")
    db = Database()

    result1 = deep_requirements_analysis(db, str(main))
    assert {r.name for r in result1.requirements} == {"numpy", "pandas"}

    base.write_text("numpy>=2.0\nscipy\n")
    result2 = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result2.requirements}
    assert names == {"numpy", "scipy", "pandas"}


#: Two unreadable path shapes a caller can pass to an entry point.
_HOSTILE_SHAPES = ("symlink-loop", "embedded-null")


def _hostile_entry(shape: str, tmp_path: Path) -> str:
    if shape == "symlink-loop":
        return str(make_symlink_loop(tmp_path / "loop"))
    return nul_path(tmp_path)


@posix_only
@pytest.mark.parametrize("shape", _HOSTILE_SHAPES)
def test_a_deep_requirements_analysis_of_a_hostile_path_is_refused_by_type(
    shape: str, tmp_path: Path
) -> None:
    # The deep entry canonicalizes its root. For these two shapes the answer
    # used to depend on the interpreter. Older ones raised a loop error. Newer
    # ones returned a path that was still a link, which read back as an empty
    # analysis. Canonicalizing through the tracked path answers
    # "unresolvable" on every interpreter, and an unresolvable root the caller
    # named is refused here. A reference found inside a file is the other case
    # and keeps its diagnostic, because that path is file content.
    db = Database()

    with pytest.raises(UnsupportedValueError, match="Path cannot be resolved"):
        deep_requirements_analysis(db, _hostile_entry(shape, tmp_path))

    main = tmp_path / "requirements.txt"
    main.write_text("flask\n")
    assert {req.name for req in deep_requirements_analysis(db, str(main)).requirements} == {
        "flask"
    }


@posix_only
@pytest.mark.parametrize("shape", _HOSTILE_SHAPES)
def test_a_shallow_requirements_analysis_of_a_hostile_path_is_refused_by_type(
    shape: str, tmp_path: Path
) -> None:
    # The shallow entry leaves canonicalization to the tracked file resource it
    # reads through, and that resource already refuses both shapes. This pins
    # the refusal to the read seam, so it holds whichever entry point a caller
    # uses.
    db = Database()

    with pytest.raises(UnsafeFilesystemPathError):
        requirements_analysis(db, _hostile_entry(shape, tmp_path))

    main = tmp_path / "requirements.txt"
    main.write_text("flask\n")
    assert {req.name for req in requirements_analysis(db, str(main)).requirements} == {"flask"}


@posix_only
@pytest.mark.parametrize("shape", _HOSTILE_SHAPES)
def test_a_workspace_requirements_analysis_of_a_hostile_root_is_refused_by_type(
    shape: str, tmp_path: Path
) -> None:
    # The workspace entry lists its root before it calls the deep entry, so the
    # tracked listing refuses a hostile root before any canonicalization. Pinned
    # beside the deep entry's refusal to show these are two separate refusals.
    db = Database()

    with pytest.raises(UnsafeFilesystemPathError):
        workspace_requirements_analysis(db, _hostile_entry(shape, tmp_path))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "requirements.txt").write_text("flask\n")
    analysis = workspace_requirements_analysis(db, str(workspace))
    assert analysis is not None
    assert {req.name for req in analysis.requirements} == {"flask"}


# ---------------------------------------------------------------------------
# The referenced file's existence
# ---------------------------------------------------------------------------


def _resolved(path: Path | str) -> str:
    """Return the spelling the walk reaches for *path*, resolved the way the walk does.

    The walk names a reference by its canonical path, and on some platforms a
    temporary directory is reached through a link. Deriving the expectation
    from the same tracked resolution makes these cells compare the spellings
    the code distinguishes, whatever spelling a fixture produced.
    """
    resolved = ResolvedPathResource().read(Database(), os.fspath(path))
    assert resolved is not None
    return resolved


#: Worlds whose ``-r`` target starts unreadable and then becomes a readable
#: file. The walk answers each with the missing-file diagnostic before the
#: transition and with the reference's own requirements after it. Each takes a
#: different route through the filesystem: an absent sibling, an absent entry
#: below a real directory, a directory where a file is expected, a path whose
#: parent is a file, and a dangling link.
_APPEARING_REFERENCE_CASES = (
    "absent-sibling",
    "absent-below-a-directory",
    "directory-becomes-a-file",
    "parent-file-becomes-a-directory",
    "dangling-link-gains-a-target",
)

#: Worlds whose target starts as a readable file and then changes some other
#: way. They join the five above in the preservation oracle only.
_ALREADY_READABLE_CASES = ("present-then-deleted", "link-retargeted")


def _reference_world(case: str, workspace: Path) -> tuple[Path, Callable[[], None]]:
    """Build *case* under *workspace* and return its reference path and transition.

    The returned path is the one the ``-r`` line names, before resolution. The
    fixtures leave out inline comments. Elsewhere in this file those decide how
    a line parses, which would confound a case about the filesystem.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    reference = "extra.txt"

    def _write_reference(target: Path) -> Callable[[], None]:
        def transition() -> None:
            target.write_text("requests\n", encoding="utf-8")

        return transition

    if case == "absent-sibling":
        raw = workspace / "extra.txt"
        transition = _write_reference(raw)
    elif case == "absent-below-a-directory":
        reference = "sub/extra.txt"
        (workspace / "sub").mkdir()
        raw = workspace / "sub" / "extra.txt"
        transition = _write_reference(raw)
    elif case == "directory-becomes-a-file":
        raw = workspace / "extra.txt"
        raw.mkdir()

        def transition() -> None:
            raw.rmdir()
            raw.write_text("requests\n", encoding="utf-8")

    elif case == "parent-file-becomes-a-directory":
        reference = "sub/extra.txt"
        blocker = workspace / "sub"
        blocker.write_text("not a directory\n", encoding="utf-8")
        raw = blocker / "extra.txt"

        def transition() -> None:
            blocker.unlink()
            blocker.mkdir()
            raw.write_text("requests\n", encoding="utf-8")

    elif case == "dangling-link-gains-a-target":
        raw = workspace / "extra.txt"
        raw.symlink_to(workspace / "target.txt")
        transition = _write_reference(workspace / "target.txt")
    elif case == "present-then-deleted":
        raw = workspace / "extra.txt"
        raw.write_text("requests\n", encoding="utf-8")

        def transition() -> None:
            raw.unlink()

    elif case == "link-retargeted":
        reference = "link.txt"
        (workspace / "a.txt").write_text("requests\n", encoding="utf-8")
        (workspace / "b.txt").write_text("urllib3\n", encoding="utf-8")
        raw = workspace / "link.txt"
        raw.symlink_to(workspace / "a.txt")

        def transition() -> None:
            raw.unlink()
            raw.symlink_to(workspace / "b.txt")

    else:  # pragma: no cover - a case name with no world is a test bug
        raise AssertionError(f"unknown reference world: {case}")

    (workspace / "requirements.txt").write_text(f"-r {reference}\nclick\n", encoding="utf-8")
    return raw, transition


def _records_naming(db: Database, target: str) -> dict[str, int]:
    """Map each resource record whose label names *target* to its ``changed_at``."""
    return {
        node.label: node.changed_at
        for node in db.dependency_graph()
        if node.kind == "resource" and target in node.label
    }


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reference_coming_into_existence_moves_a_record_the_walk_declared(
    mode: str, tmp_path: Path
) -> None:
    # The walk's answer depends on whether the reference names a readable file,
    # so the walk must declare that as a dependency: a record already in the
    # graph whose probe moves with the filesystem. Only records present BEFORE
    # the transition count. A record that appears afterwards is irrelevant to
    # what the earlier answer depended on. One does appear here, because
    # reaching the reference registers the reference's own file record.
    for case in _APPEARING_REFERENCE_CASES:
        workspace = tmp_path / case
        raw, transition = _reference_world(case, workspace)
        target = _resolved(raw)
        presence = f"requirementsfilepresence[{target}]"
        root = str(workspace / "requirements.txt")
        db = Database(mode=mode)

        warm = deep_requirements_analysis(db, root)
        assert [code for code, _ in warm.diagnostics] == ["missing-requirements-file"], (
            f"the world did not start missing | case={case} | mode={mode} | "
            f"diagnostics={warm.diagnostics}"
        )
        assert {req.name for req in warm.requirements} == {"click"}, (
            f"the root's own requirements did not survive | case={case} | mode={mode}"
        )

        before = _records_naming(db, target)
        assert presence in before, (
            f"the walk declared no existence read for the reference | case={case} | "
            f"mode={mode} | records naming the target={sorted(before)}"
        )
        probe_before = requirements_module._PRESENCE.probe(target)

        transition()
        probe_after = requirements_module._PRESENCE.probe(target)
        answer = deep_requirements_analysis(db, root)
        after = _records_naming(db, target)

        moved = sorted(label for label, changed in before.items() if after.get(label) != changed)
        appeared = sorted(set(after) - set(before))
        assert moved, (
            f"the reference came to name a file and no record the walk had already "
            f"declared moved | case={case} | mode={mode} | before={sorted(before)} | "
            f"appeared={appeared}"
        )
        assert presence in moved, (
            f"the record that moved is not the existence read's own | case={case} | "
            f"mode={mode} | moved={moved}"
        )
        assert probe_before == ("missing",), (
            f"the existence probe did not start missing | case={case} | mode={mode} | "
            f"probe={probe_before}"
        )
        assert probe_after[0] == "present", (
            f"the existence probe did not end present | case={case} | mode={mode} | "
            f"probe={probe_after}"
        )
        assert {req.name for req in answer.requirements} == {"click", "requests"}, (
            f"the reference's requirements were not merged | case={case} | mode={mode} | "
            f"requirements={sorted(req.name for req in answer.requirements)}"
        )
        assert answer.diagnostics == (), (
            f"the diagnostic outlived the missing file | case={case} | mode={mode} | "
            f"diagnostics={answer.diagnostics}"
        )


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_direct_walk_answers_a_changed_reference_the_way_a_fresh_one_does(
    mode: str, tmp_path: Path
) -> None:
    # A preservation oracle. Every case here already answers soundly when the
    # entry point is called directly, because a direct call re-walks each time
    # with no node to reuse. These cells check that the existence read kept that
    # soundness. They measure preservation only, and reading the seven green
    # rows as coverage of its repairs would overstate what this file measures.
    for case in _APPEARING_REFERENCE_CASES + _ALREADY_READABLE_CASES:
        workspace = tmp_path / case
        _raw, transition = _reference_world(case, workspace)
        root = str(workspace / "requirements.txt")

        warm_db = Database(mode=mode)
        deep_requirements_analysis(warm_db, root)
        workspace_requirements_analysis(warm_db, str(workspace))

        transition()

        warm_deep = deep_requirements_analysis(warm_db, root)
        warm_workspace = workspace_requirements_analysis(warm_db, str(workspace))

        fresh_db = Database(mode=mode)
        fresh_deep = deep_requirements_analysis(fresh_db, root)
        fresh_workspace = workspace_requirements_analysis(fresh_db, str(workspace))

        assert warm_deep == fresh_deep, (
            f"a warm direct walk disagrees with a fresh one | case={case} | mode={mode} | "
            f"warm={sorted(req.name for req in warm_deep.requirements)} "
            f"diagnostics={warm_deep.diagnostics} | "
            f"fresh={sorted(req.name for req in fresh_deep.requirements)} "
            f"diagnostics={fresh_deep.diagnostics}"
        )
        assert warm_workspace == fresh_workspace, (
            f"a warm workspace walk disagrees with a fresh one | case={case} | mode={mode} | "
            f"warm={warm_workspace} | fresh={fresh_workspace}"
        )


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reference_below_an_unreadable_directory_reports_the_missing_file(
    mode: str, tmp_path: Path
) -> None:
    # The file exists but the process cannot reach it. Asking the filesystem
    # directly gives different answers on different builds. One swallows the
    # denial and reports the reference missing, and another raises it out of the
    # entry point, so the caller's answer depends on the interpreter. Reading
    # existence through the file resource's own probe settles it. On every
    # interpreter, a reference this library refuses to read is reported like an
    # absent one.
    skip_without_posix_permissions()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    blocked = workspace / "blocked"
    blocked.mkdir()
    (blocked / "extra.txt").write_text("requests\n", encoding="utf-8")
    (workspace / "requirements.txt").write_text("-r blocked/extra.txt\nflask\n", encoding="utf-8")

    blocked.chmod(0o000)
    try:
        analysis = deep_requirements_analysis(
            Database(mode=mode), str(workspace / "requirements.txt")
        )
    finally:
        blocked.chmod(0o755)

    assert [code for code, _ in analysis.diagnostics] == ["missing-requirements-file"], (
        f"a reference below an unreadable directory was not reported missing | mode={mode} | "
        f"diagnostics={analysis.diagnostics}"
    )
    assert {req.name for req in analysis.requirements} == {"flask"}, (
        f"the root's own requirements did not survive the unreachable reference | mode={mode} | "
        f"requirements={sorted(req.name for req in analysis.requirements)}"
    )


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reference_the_process_cannot_open_reports_the_missing_file(
    mode: str, tmp_path: Path
) -> None:
    # The other denial: the reference is a regular file without read
    # permission, inside a directory the process can traverse. The walk gives
    # any unreadable reference the missing-file diagnostic and keeps the denial
    # inside the entry point. The sibling arm gives the same answer for a
    # reference that fails to canonicalize, and a caller can act on it.
    skip_without_posix_permissions()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra = workspace / "extra.txt"
    extra.write_text("requests\n", encoding="utf-8")
    (workspace / "requirements.txt").write_text("-r extra.txt\nflask\n", encoding="utf-8")

    extra.chmod(0o000)
    try:
        analysis = deep_requirements_analysis(
            Database(mode=mode), str(workspace / "requirements.txt")
        )
    finally:
        extra.chmod(0o644)

    assert [code for code, _ in analysis.diagnostics] == ["missing-requirements-file"], (
        f"an unreadable reference was not reported missing | mode={mode} | "
        f"diagnostics={analysis.diagnostics}"
    )
    assert {req.name for req in analysis.requirements} == {"flask"}, (
        f"the root's own requirements did not survive the unreadable reference | mode={mode} | "
        f"requirements={sorted(req.name for req in analysis.requirements)}"
    )


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_an_unreadable_root_requirements_file_reports_the_missing_file(
    mode: str, tmp_path: Path
) -> None:
    # The caller's root goes through the same arm as its references, so an
    # unopenable root gets the same answer: an analysis of that file holding
    # only the report that it is unreadable. Both entry points run, because the
    # workspace entry finds the file by listing and hands it to the deep entry.
    # A caller who reaches the walk that way gets the same report.
    skip_without_posix_permissions()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / "requirements.txt"
    root.write_text("flask\n", encoding="utf-8")
    expected = _resolved(root)

    root.chmod(0o000)
    try:
        direct = deep_requirements_analysis(Database(mode=mode), str(root))
        by_discovery = workspace_requirements_analysis(Database(mode=mode), str(workspace))
    finally:
        root.chmod(0o644)

    assert by_discovery is not None, (
        f"the workspace entry point stopped finding the root it lists | mode={mode}"
    )
    for name, analysis in (("deep", direct), ("workspace", by_discovery)):
        assert analysis.diagnostics == (
            ("missing-requirements-file", f"referenced requirements file is missing: {expected}"),
        ), f"an unreadable root was not reported missing | entry={name} | mode={mode}"
        assert analysis.requirements == (), (
            f"an unreadable root yielded requirements | entry={name} | mode={mode} | "
            f"requirements={sorted(req.name for req in analysis.requirements)}"
        )
        assert analysis.path == expected, (
            f"the analysis is not about the root that was asked for | entry={name} | "
            f"mode={mode} | path={analysis.path}"
        )


#: The three shapes that reach the reference arm without naming a readable file.
#: All three are reported with the same code, which is what a caller one layer
#: up reads.
_NON_FILE_REFERENCE_SHAPES = ("absent", "a-directory", "below-a-file")


@posix_only
@pytest.mark.parametrize("shape", _NON_FILE_REFERENCE_SHAPES)
def test_a_reference_that_names_no_file_is_reported_by_its_resolved_path(
    shape: str, tmp_path: Path
) -> None:
    # The workspace is reached through a symlinked directory, so the caller's
    # path and the walk's resolved path are different strings, and the message
    # uses only one of them. The expectation comes from the same tracked
    # resolution the walk uses. Built from the fixture's spelling, it would pass
    # here and fail on a platform whose temporary directory is itself reached
    # through a link.
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    reference = "extra.txt"
    if shape == "a-directory":
        (real / "extra.txt").mkdir()
    elif shape == "below-a-file":
        reference = "blocker/extra.txt"
        (real / "blocker").write_text("not a directory\n", encoding="utf-8")

    (real / "requirements.txt").write_text(f"-r {reference}\nflask\n", encoding="utf-8")
    expected = _resolved(linked / reference)
    assert expected != os.fspath(linked / reference), (
        "the fixture stopped distinguishing the two spellings, so the assertion below "
        f"would hold whichever one the walk reported | resolved={expected}"
    )

    analysis = deep_requirements_analysis(Database(), str(linked / "requirements.txt"))

    assert analysis.diagnostics == (
        ("missing-requirements-file", f"referenced requirements file is missing: {expected}"),
    ), f"the reference was not reported by its resolved path | shape={shape}"
    assert {req.name for req in analysis.requirements} == {"flask"}, (
        f"the root's own requirements did not survive the missing reference | shape={shape}"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reloaded_checkpoint_restores_the_reference_existence_record(
    mode: str, tmp_path: Path
) -> None:
    # Use one filesystem path throughout. Saving under one name and loading
    # under another reuses nothing, and the row would pass on any tree. The
    # edit lands BEFORE the save and the entry point runs again, so the
    # checkpoint holds the state the database reached by answering after the
    # edit.
    root = tmp_path / "requirements.txt"
    root.write_text("-r extra.txt\nclick\n", encoding="utf-8")
    presence = f"requirementsfilepresence[{_resolved(tmp_path / 'extra.txt')}]"

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    deep_requirements_analysis(saver, str(root))
    root.write_text("-r extra.txt\nclick\nflask\n", encoding="utf-8")
    deep_requirements_analysis(saver, str(root))
    key = saver.save_checkpoint()

    assert presence in {node.label for node in saver.dependency_graph()}, (
        f"the saved database declared no existence read to carry | mode={mode} | "
        f"records={sorted(node.label for node in saver.dependency_graph())}"
    )

    reloaded = Database(mode=mode, store=store)
    reloaded.load_checkpoint(key)
    reloaded.reset_statistics()
    restored = deep_requirements_analysis(reloaded, str(root))
    statistics = reloaded.statistics()
    profile = [
        entry
        for entry in reloaded.query_profile()
        if ":requirements_analysis_payload[" in entry.query_label
    ]
    expected = deep_requirements_analysis(Database(mode=mode), str(root))

    assert restored == expected, (
        f"a reloaded checkpoint disagrees with a fresh read | mode={mode} | "
        f"restored={sorted(req.name for req in restored.requirements)} | "
        f"expected={sorted(req.name for req in expected.requirements)}"
    )

    # The record is in the graph and the ask that revealed it made zero
    # resource loads, so its value came from the checkpoint. Read the graph only
    # after that ask. A reload stages the checkpoint and warms records on demand,
    # so right after the reload every record is absent, restored or not.
    assert presence in {node.label for node in reloaded.dependency_graph()}, (
        f"the existence read did not come back from the checkpoint | mode={mode} | "
        f"records={sorted(node.label for node in reloaded.dependency_graph())}"
    )
    assert statistics.resource_loads == 0, (
        f"the first ask after a reload loaded a resource, so a record it should have "
        f"carried was rebuilt instead | mode={mode} | loads={statistics.resource_loads}"
    )
    assert statistics.query_executions == 0, (
        f"the first ask after a reload re-executed | mode={mode} | "
        f"executions={statistics.query_executions}"
    )
    assert profile == [], (
        f"the payload nodes were not reused across the reload | mode={mode} | "
        f"profile={[(entry.query_label, entry.execution_count) for entry in profile]}"
    )


def test_deep_requirements_reports_cycles_duplicates_and_escape(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = project / "requirements.txt"
    first = project / "first.txt"
    second = project / "second.txt"
    shared = project / "shared.txt"
    absolute = project / "absolute.txt"
    outside = tmp_path / "outside.txt"

    root.write_text(
        f"-r first.txt\n-r second.txt\n-r {absolute}\n-r ../outside.txt\nroot-pkg\n",
        encoding="utf-8",
    )
    first.write_text(f"-r {root}\n-r shared.txt\nfirst-pkg\n", encoding="utf-8")
    second.write_text("-r shared.txt\nsecond-pkg\n", encoding="utf-8")
    shared.write_text("shared-pkg\n", encoding="utf-8")
    absolute.write_text("absolute-pkg\n", encoding="utf-8")
    outside.write_text("outside-pkg\n", encoding="utf-8")

    analysis = deep_requirements_analysis(Database(), root)

    assert {requirement.name for requirement in analysis.requirements} == {
        "absolute_pkg",
        "first_pkg",
        "root_pkg",
        "second_pkg",
        "shared_pkg",
    }
    assert any(code == "cycle" for code, _message in analysis.diagnostics)
    assert any("outside project" in message for _code, message in analysis.diagnostics)
