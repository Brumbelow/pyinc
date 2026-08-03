from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tokenize
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pyinc_tools._workspace as workspace


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("name = 'café'\n", "name = 'café'\n".encode()),
        ("\ufeffname = 'café'\n", "name = 'café'\n".encode("utf-8-sig")),
        (
            "# coding: definitely-unknown\nname = 'café'\n",
            b"# coding: definitely-unknown\nname = 'caf\xc3\xa9'\n",
        ),
        ("# coding: ascii\nname = 'café'\n", b"# coding: utf-8\nname = 'caf\xc3\xa9'\n"),
        (
            "#!/usr/bin/python\n# coding: ascii\nname = 'café'\n",
            b"#!/usr/bin/python\n# coding: utf-8\nname = 'caf\xc3\xa9'\n",
        ),
    ],
)
def test_encode_python_text_honors_or_repairs_encoding_declarations(
    source: str, expected: bytes
) -> None:
    assert workspace._encode_python_text(source) == expected


def test_encode_python_text_falls_back_when_reported_encoding_cannot_represent_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tokenize, "detect_encoding", lambda _readline: ("ascii", []))

    assert workspace._encode_python_text("name = 'café'\n") == "name = 'café'\n".encode()


def test_logical_requirement_lines_join_continuations_and_keep_dangling_slash() -> None:
    text = "one " + "\\" + "\n two\nthree" + "\\"
    assert workspace._logical_requirement_lines(text) == ("one  two", "three\\")
    assert workspace._logical_requirement_lines("") == ()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("package>=1 # pinned", "package>=1"),
        ("package#fragment", "package#fragment"),
        ("# full-line comment", "# full-line comment"),
        ('package; marker == "value # literal" # note', 'package; marker == "value # literal"'),
        ("package; marker == 'value # literal'", "package; marker == 'value # literal'"),
        ("plain", "plain"),
    ],
)
def test_strip_requirement_inline_comment_respects_quotes(line: str, expected: str) -> None:
    assert workspace._strip_requirement_inline_comment(line) == expected


def test_workspace_path_filters_cover_outside_ignored_excluded_and_allowed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert workspace._workspace_path_allowed(root / "mod.py", root, frozenset(), ())
    assert not workspace._workspace_path_allowed(tmp_path / "outside.py", root, frozenset(), ())
    assert not workspace._workspace_path_allowed(
        root / ".cache" / "mod.py", root, frozenset({".cache"}), ()
    )
    assert not workspace._workspace_path_allowed(
        root / "generated" / "mod.py", root, frozenset(), ("generated/**",)
    )


def test_workspace_link_detection_handles_windows_reparse_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Probe:
        def __init__(self, attributes: int | None) -> None:
            self.attributes = attributes

        def is_symlink(self) -> bool:
            return False

        def lstat(self) -> SimpleNamespace:
            if self.attributes is None:
                raise FileNotFoundError
            return SimpleNamespace(st_file_attributes=self.attributes)

    monkeypatch.setattr(workspace, "os", SimpleNamespace(name="nt"))

    assert workspace._is_workspace_link(Probe(0x400))  # type: ignore[arg-type]
    assert not workspace._is_workspace_link(Probe(0))  # type: ignore[arg-type]
    assert not workspace._is_workspace_link(Probe(None))  # type: ignore[arg-type]

    symlink = tmp_path / "link"
    _symlink_or_skip(symlink, tmp_path / "target")
    assert workspace._is_workspace_link(symlink)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("module.py", True),
        ("stub.PYI", True),
        ("pyproject.toml", True),
        (".env", True),
        ("Pipfile", True),
        ("requirements.in", False),
        ("image.png", False),
    ],
)
def test_relevant_file_classification(name: str, expected: bool) -> None:
    assert workspace._is_relevant_file(Path(name)) is expected


def test_exclusion_matching_supports_fnmatch_path_match_and_outside_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"

    assert workspace._is_excluded(root / "build" / "mod.py", root, ("build/**",))
    assert workspace._is_excluded(root / "pkg" / "generated.py", root, ("**/generated.py",))
    assert not workspace._is_excluded(root / "pkg" / "mod.py", root, ("build/**",))
    assert workspace._is_excluded(tmp_path / "outside.py", root, ())


def test_validate_symlink_accepts_in_root_target_and_rejects_missing_or_escaping(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.py"
    target.write_text("pass\n", encoding="utf-8")
    in_root = root / "inside.py"
    _symlink_or_skip(in_root, target)

    workspace._validate_symlink(in_root, root)

    missing = root / "missing.py"
    _symlink_or_skip(missing, root / "absent.py")
    with pytest.raises(ValueError, match="escapes the root"):
        workspace._validate_symlink(missing, root)

    outside_target = tmp_path / "outside.py"
    outside_target.write_text("pass\n", encoding="utf-8")
    escaping = root / "outside.py"
    _symlink_or_skip(escaping, outside_target)
    with pytest.raises(ValueError, match="escapes the root"):
        workspace._validate_symlink(escaping, root)


def test_reject_symlink_components_rejects_even_an_in_root_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    real = root / "real"
    real.mkdir(parents=True)
    link = root / "link"
    _symlink_or_skip(link, real, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks are not supported"):
        workspace._reject_symlink_components(link / "mod.py", root)


def test_read_workspace_file_rejects_outside_unnormalized_directories_and_special_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside")

    with pytest.raises(ValueError, match="outside the root"):
        workspace._read_workspace_file(outside, root)
    with pytest.raises(ValueError, match="not normalized"):
        workspace._read_workspace_file(root, root)
    with pytest.raises(ValueError, match="not normalized"):
        workspace._read_workspace_file(root / ".." / "outside.py", root)

    directory = root / "directory.py"
    directory.mkdir()
    with pytest.raises(IsADirectoryError):
        workspace._read_workspace_file(directory, root)

    if hasattr(os, "mkfifo"):
        fifo = root / "pipe.py"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="not a regular file"):
            workspace._read_workspace_file(fifo, root)

    # A parent that is a file means the path is gone, not that it is unsafe.
    plain = root / "mod.py"
    plain.write_bytes(b"pass\n")
    with pytest.raises(NotADirectoryError):
        workspace._read_workspace_file(plain / "inner.py", root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_read_workspace_file_rejects_unsafe_parent_and_file_links(tmp_path: Path) -> None:
    root = tmp_path / "root"
    real = root / "real"
    real.mkdir(parents=True)
    (real / "mod.py").write_bytes(b"pass\n")

    linked_parent = root / "linked"
    _symlink_or_skip(linked_parent, real, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe path component"):
        workspace._read_workspace_file(linked_parent / "mod.py", root)

    linked_file = root / "link.py"
    _symlink_or_skip(linked_file, real / "mod.py")
    with pytest.raises(ValueError, match="symlinks are not supported"):
        workspace._read_workspace_file(linked_file, root)


def test_read_workspace_file_dispatches_to_windows_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "mod.py"
    observed: list[tuple[Path, Path]] = []

    def read_windows(path: Path, workspace_root: Path) -> bytes:
        observed.append((path, workspace_root))
        return b"contents"

    monkeypatch.setattr(workspace, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(workspace, "_read_workspace_file_windows", read_windows)

    assert workspace._read_workspace_file(target, root) == b"contents"
    assert observed == [(target, root)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_require_workspace_parent_identity_reports_missing_and_unsafe_components(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    regular = root / "regular"
    regular.write_text("not a directory", encoding="utf-8")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        with pytest.raises(FileNotFoundError):
            workspace._require_workspace_parent_identity(
                descriptor, root / "missing", (), root / "missing" / "mod.py", flags
            )
        with pytest.raises(ValueError, match="parent is not safe to read"):
            workspace._require_workspace_parent_identity(
                descriptor, root, ("regular",), regular / "mod.py", flags
            )
    finally:
        os.close(descriptor)

    other = tmp_path / "other"
    other.mkdir()
    descriptor = os.open(other, flags)
    try:
        with pytest.raises(ValueError, match="changed identity"):
            workspace._require_workspace_parent_identity(
                descriptor, root, (), root / "mod.py", flags
            )
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_read_workspace_file_propagates_missing_parent_and_wraps_leaf_open_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        workspace._read_workspace_file(root / "missing" / "mod.py", root)

    denied = root / "denied.py"
    denied.write_text("pass\n", encoding="utf-8")
    original_open = os.open

    def deny_leaf(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "denied.py":
            raise PermissionError(errno.EACCES, "denied", path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_leaf)
    with pytest.raises(ValueError, match="not safe to read"):
        workspace._read_workspace_file(denied, root)


def test_windows_workspace_reader_validates_file_types_and_reads_regular_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "mod.py"
    target.write_bytes(b"contents")

    assert workspace._read_workspace_file_windows(target, root) == b"contents"

    with pytest.raises(FileNotFoundError):
        workspace._read_workspace_file_windows(root / "missing.py", root)
    with pytest.raises(IsADirectoryError):
        workspace._read_workspace_file_windows(root, root)

    if hasattr(os, "mkfifo"):
        fifo = root / "pipe.py"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="not a regular file"):
            workspace._read_workspace_file_windows(fifo, root)


def test_windows_workspace_reader_rejects_links_open_errors_and_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "mod.py"
    target.write_bytes(b"original")
    link = root / "link.py"
    _symlink_or_skip(link, target)

    monkeypatch.setattr(workspace, "_reject_symlink_components", lambda *_args: None)
    with pytest.raises(ValueError, match="symlinks are not supported"):
        workspace._read_workspace_file_windows(link, root)

    original_open = os.open

    def deny(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == target:
            raise PermissionError(errno.EACCES, "denied", path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(ValueError, match="not safe to read"):
        workspace._read_workspace_file_windows(target, root)

    def disappear(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == target:
            raise FileNotFoundError(path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", disappear)
    with pytest.raises(FileNotFoundError):
        workspace._read_workspace_file_windows(target, root)

    monkeypatch.setattr(os, "open", original_open)
    replaced = False

    def replace_before_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if path == target and not replaced:
            replaced = True
            target.rename(root / "original.py")
            target.write_bytes(b"replacement")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(ValueError, match="changed identity"):
        workspace._read_workspace_file_windows(target, root)


def test_requirements_reference_paths_follows_allowed_recursive_closure(tmp_path: Path) -> None:
    root = tmp_path / "root"
    deps = root / "deps"
    ignored = root / "ignored"
    deps.mkdir(parents=True)
    ignored.mkdir()
    outside = tmp_path / "outside.in"
    outside.write_text("outside\n", encoding="utf-8")

    base = deps / "base.in"
    more = deps / "more.in"
    constraints = deps / "constraints.in"
    absolute = deps / "absolute.in"
    directory = deps / "directory.in"
    directory.mkdir()
    for path in (base, more, constraints, absolute):
        path.write_text("package\n", encoding="utf-8")
    (ignored / "hidden.in").write_text("hidden\n", encoding="utf-8")
    (root / "excluded.in").write_text("excluded\n", encoding="utf-8")
    base.write_text("-r more.in\n", encoding="utf-8")
    more.write_text("-r base.in\n", encoding="utf-8")

    (root / "requirements.txt").write_text(
        "\n".join(
            [
                "package>=1",
                "-r deps/base.in",
                "-r \\",
                " deps/more.in",
                "--constraint deps/constraints.in # comment",
                f"-c {absolute}",
                "-r deps/directory.in",
                "-r missing.in",
                "-r ignored/hidden.in",
                "-r excluded.in",
                "-r ../outside.in",
                f"-r {outside}",
                "-r bad\0name.in",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert workspace._requirements_reference_paths(
        root,
        frozenset({"ignored"}),
        ("excluded.in",),
    ) == {base, more, constraints, absolute}


def test_requirements_reference_paths_handles_missing_directory_and_invalid_utf8_entrypoints(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert workspace._requirements_reference_paths(root, frozenset(), ()) == set()

    requirements = root / "requirements.txt"
    requirements.mkdir()
    assert workspace._requirements_reference_paths(root, frozenset(), ()) == set()

    requirements.rmdir()
    requirements.write_bytes(b"\xff")
    assert workspace._requirements_reference_paths(root, frozenset(), ()) == set()
    assert (
        workspace._requirements_reference_paths(root, frozenset(), ("requirements.txt",)) == set()
    )


def test_workspace_files_include_relevant_and_referenced_files_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "pkg").mkdir(parents=True)
    (root / "ignored").mkdir()
    (root / "excluded").mkdir()
    (root / "pkg" / "mod.py").write_text("pass\n", encoding="utf-8")
    (root / "pkg" / "data.bin").write_bytes(b"data")
    (root / "ignored" / "hidden.py").write_text("pass\n", encoding="utf-8")
    (root / "excluded" / "hidden.py").write_text("pass\n", encoding="utf-8")
    reference = root / "pkg" / "base.in"
    reference.write_text("package\n", encoding="utf-8")
    (root / "requirements.txt").write_text("-r pkg/base.in\n", encoding="utf-8")

    files, referenced = workspace._workspace_files(
        str(root), frozenset({"ignored"}), ("excluded/**",)
    )

    assert files == {root / "pkg" / "mod.py", root / "requirements.txt", reference}
    assert referenced == {reference}


def test_workspace_files_skip_supported_in_root_directory_links(tmp_path: Path) -> None:
    root = tmp_path / "root"
    real = root / "real"
    real.mkdir(parents=True)
    (real / "mod.py").write_text("pass\n", encoding="utf-8")
    _symlink_or_skip(root / "linked", real, target_is_directory=True)

    files, referenced = workspace._workspace_files(str(root), frozenset(), ())

    assert files == {real / "mod.py"}
    assert referenced == set()


def test_collect_filesystem_snapshot_hashes_contents_and_tolerates_a_disappearing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "mod.py"
    target.write_bytes(b"pass\n")

    assert workspace._collect_filesystem_snapshot(str(root), frozenset()) == {
        str(target): hashlib.sha256(b"pass\n").hexdigest()
    }

    monkeypatch.setattr(workspace, "_workspace_files", lambda *_args: ({target}, set()))
    monkeypatch.setattr(
        workspace,
        "_read_workspace_file",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError(target)),
    )
    assert workspace._collect_filesystem_snapshot(str(root), frozenset()) == {}


def test_workspace_mirror_copies_and_normalizes_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    (root / "pkg").mkdir(parents=True)
    target = root / "pkg" / "mod.py"
    target.write_bytes(b"pass\n")
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())

    mirror.copy_workspace()

    assert (mirror_root / "pkg" / "mod.py").read_bytes() == b"pass\n"
    assert mirror.normalize_real_path("pkg/mod.py") == str(target)
    assert mirror.normalize_real_path(target) == str(target)
    assert mirror.mirror_path_for_real(str(target)) == mirror_root / "pkg" / "mod.py"
    with pytest.raises(ValueError, match="outside the workspace"):
        mirror.normalize_real_path(tmp_path / "outside.py")


def test_workspace_mirror_canonicalizes_alias_roots(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    canonical_mirror = tmp_path / "canonical-mirror"
    canonical_mirror.mkdir()
    mirror_alias = tmp_path / "mirror-alias"
    _symlink_or_skip(mirror_alias, canonical_mirror, target_is_directory=True)

    mirror = workspace.WorkspaceMirror(str(root), str(mirror_alias), frozenset(), ())

    assert mirror.root_path == root.resolve(strict=True)
    assert mirror.mirror_root_path == canonical_mirror.resolve(strict=True)
    assert mirror.mirror_path_for_real(str(root / "mod.py")) == canonical_mirror / "mod.py"


def test_workspace_mirror_copy_tolerates_a_file_disappearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    root.mkdir()
    target = root / "gone.py"
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())
    monkeypatch.setattr(workspace, "_workspace_files", lambda *_args: ({target}, set()))
    monkeypatch.setattr(
        workspace,
        "_read_workspace_file",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError(target)),
    )

    mirror.copy_workspace()

    assert not (mirror_root / "gone.py").exists()


def test_workspace_mirror_syncs_regular_directory_deleted_and_irrelevant_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    root.mkdir()
    mirror_root.mkdir()
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())

    regular = root / "mod.py"
    regular.write_bytes(b"first")
    mirror.sync_path_from_disk(str(regular))
    assert (mirror_root / "mod.py").read_bytes() == b"first"

    directory = root / "package.py"
    directory.mkdir()
    mirror.sync_path_from_disk(str(directory))
    assert (mirror_root / "package.py").is_dir()

    regular.unlink()
    mirror.sync_path_from_disk(str(regular))
    assert not (mirror_root / "mod.py").exists()

    irrelevant = root / "asset.bin"
    irrelevant.write_bytes(b"data")
    mirrored_irrelevant = mirror_root / "asset.bin"
    mirrored_irrelevant.write_bytes(b"stale")
    mirror.sync_path_from_disk(str(irrelevant))
    assert not mirrored_irrelevant.exists()
    mirror.sync_path_from_disk(str(irrelevant))

    missing_directory = root / "missing.py"
    (mirror_root / "missing.py" / "child").mkdir(parents=True)
    mirror.sync_path_from_disk(str(missing_directory))
    assert not (mirror_root / "missing.py").exists()

    missing_irrelevant = root / "missing.bin"
    stale = mirror_root / "missing.bin"
    stale.write_bytes(b"stale")
    mirror.sync_path_from_disk(str(missing_irrelevant))
    assert not stale.exists()
    mirror.sync_path_from_disk(str(missing_irrelevant))


def test_workspace_mirror_syncs_a_tracked_file_swapped_for_a_directory_and_back(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    root.mkdir()
    mirror_root.mkdir()
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())

    target = root / "mod.py"
    target.write_bytes(b"first")
    mirror.sync_path_from_disk(str(target))
    assert (mirror_root / "mod.py").read_bytes() == b"first"

    # The mirror still holds a file where the directory now belongs.
    target.unlink()
    target.mkdir()
    child = target / "inner.py"
    child.write_bytes(b"inner")
    mirror.sync_path_from_disk(str(target))
    mirror.sync_path_from_disk(str(child))

    assert (mirror_root / "mod.py").is_dir()
    assert (mirror_root / "mod.py" / "inner.py").read_bytes() == b"inner"
    assert str(target) not in mirror.content_hashes()

    # ...and the reverse: a populated mirror directory where a file now belongs.
    shutil.rmtree(target)
    target.write_bytes(b"second")
    mirror.sync_path_from_disk(str(target))

    assert (mirror_root / "mod.py").read_bytes() == b"second"
    assert mirror.content_hashes()[str(target)] == hashlib.sha256(b"second").hexdigest()

    # The child the directory used to hold is now unreachable, not unsafe.
    mirror.sync_path_from_disk(str(child))
    assert (mirror_root / "mod.py").read_bytes() == b"second"
    assert str(child) not in mirror.content_hashes()


def test_workspace_mirror_syncs_a_child_whose_mirrored_parent_is_still_a_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    root.mkdir()
    mirror_root.mkdir()
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())

    target = root / "mod.py"
    target.write_bytes(b"first")
    mirror.sync_path_from_disk(str(target))

    target.unlink()
    target.mkdir()
    child = target / "inner.py"
    child.write_bytes(b"inner")
    # Only the child is refreshed, so the stale mirror file is the parent.
    mirror.sync_path_from_disk(str(child))

    assert (mirror_root / "mod.py" / "inner.py").read_bytes() == b"inner"


def test_workspace_mirror_tracks_added_and_removed_requirement_references(tmp_path: Path) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    root.mkdir()
    mirror_root.mkdir()
    first = root / "first.in"
    second = root / "second.in"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    requirements = root / "requirements.txt"
    requirements.write_text("-r first.in\n", encoding="utf-8")
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())

    mirror.copy_workspace()
    assert (mirror_root / "first.in").exists()

    requirements.write_text("-r second.in\n", encoding="utf-8")
    mirror.sync_path_from_disk(str(requirements))

    assert not (mirror_root / "first.in").exists()
    assert (mirror_root / "second.in").read_text(encoding="utf-8") == "second\n"


def test_workspace_mirror_keeps_normally_relevant_files_when_reference_is_removed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    root.mkdir()
    mirror_root.mkdir()
    referenced = root / "constraints.txt"
    referenced.write_text("package\n", encoding="utf-8")
    requirements = root / "requirements.txt"
    requirements.write_text("-r constraints.txt\n", encoding="utf-8")
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())
    mirror.copy_workspace()

    requirements.write_text("package\n", encoding="utf-8")
    mirror.sync_path_from_disk(str(requirements))

    assert (mirror_root / "constraints.txt").read_text(encoding="utf-8") == "package\n"


def test_workspace_mirror_prunes_empty_missing_parents_but_keeps_nonempty_ones(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    mirror_root = tmp_path / "mirror"
    root.mkdir()
    mirror_root.mkdir()
    mirror = workspace.WorkspaceMirror(str(root), str(mirror_root), frozenset(), ())

    mirror._prune_empty_parents(mirror_root / "missing" / "nested")
    keep = mirror_root / "keep"
    keep.mkdir()
    (keep / "file.txt").write_text("keep", encoding="utf-8")
    mirror._prune_empty_parents(keep)
    assert keep.exists()

    empty = mirror_root / "empty" / "nested"
    empty.mkdir(parents=True)
    mirror._prune_empty_parents(empty)
    assert not (mirror_root / "empty").exists()


class _Driver:
    def __init__(self, root: Path) -> None:
        self.root = str(root)
        self._ignored_dir_names = frozenset[str]()
        self._exclude_globs: tuple[str, ...] = ()
        self.refreshed: list[tuple[str, ...]] = []
        self._closed = False

    def refresh_paths(self, paths: Any) -> tuple[str, ...]:
        result = tuple(paths)
        self.refreshed.append(result)
        return result


def test_watcher_run_exits_when_its_session_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _Driver(tmp_path)
    watcher = workspace.PollingWorkspaceWatcher(driver)
    observed: list[Exception] = []
    watcher._on_error = observed.append

    def closed() -> tuple[str, ...]:
        driver._closed = True
        raise RuntimeError("WorkspaceSession is closed.")

    monkeypatch.setattr(watcher, "_poll_once", closed)
    watcher._run(0.0)

    assert observed == []
    assert capsys.readouterr().err == ""


def test_watcher_run_reports_a_runtime_error_that_is_not_a_closed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RuntimeError also covers RecursionError and plain bugs in the poll path.

    Only the closed-session contract may retire the watcher thread; anything
    else has to reach the error handler instead of disappearing.
    """

    watcher = workspace.PollingWorkspaceWatcher(_Driver(tmp_path))
    observed: list[Exception] = []
    watcher._on_error = observed.append
    failures = iter((RecursionError("too deep"), RuntimeError("snapshot bug")))

    def failing() -> tuple[str, ...]:
        error = next(failures, None)
        if error is None:
            watcher._stop_event.set()
            return ()
        raise error

    monkeypatch.setattr(watcher, "_poll_once", failing)
    watcher._run(0.0)

    assert [type(error).__name__ for error in observed] == ["RecursionError", "RuntimeError"]


def test_watcher_run_exits_when_a_driver_without_closed_state_signals_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _Driver(tmp_path)
    del driver._closed
    watcher = workspace.PollingWorkspaceWatcher(driver)
    observed: list[Exception] = []
    watcher._on_error = observed.append

    def closed() -> tuple[str, ...]:
        raise RuntimeError("WorkspaceSession is closed.")

    monkeypatch.setattr(watcher, "_poll_once", closed)
    watcher._run(0.0)

    assert observed == []


def test_watcher_run_returns_immediately_when_already_stopped(tmp_path: Path) -> None:
    watcher = workspace.PollingWorkspaceWatcher(_Driver(tmp_path))
    watcher._stop_event.set()

    watcher._run(0.0)


def test_watcher_run_ignores_ready_batch_without_callback(tmp_path: Path) -> None:
    watcher = workspace.PollingWorkspaceWatcher(_Driver(tmp_path))

    class StopAfterOneIteration:
        def is_set(self) -> bool:
            return False

        def wait(self, _interval: float) -> bool:
            return True

    watcher._stop_event = StopAfterOneIteration()  # type: ignore[assignment]
    watcher._poll_once = lambda: ("changed.py",)  # type: ignore[method-assign]
    watcher._run(0.0)


def test_watcher_stop_reports_a_thread_that_does_not_finish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watcher = workspace.PollingWorkspaceWatcher(_Driver(tmp_path))

    class StuckThread:
        def join(self, timeout: float) -> None:
            assert timeout == 0.25

        def is_alive(self) -> bool:
            return True

    watcher._thread = StuckThread()  # type: ignore[assignment]
    watcher.stop(timeout=0.25)

    assert "thread did not stop within timeout" in capsys.readouterr().err
    assert watcher._thread is not None


def test_watcher_error_handler_uses_callback_or_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watcher = workspace.PollingWorkspaceWatcher(_Driver(tmp_path))
    observed: list[Exception] = []
    error = ValueError("broken")

    watcher._on_error = observed.append
    watcher._handle_error(error)
    assert observed == [error]
    assert capsys.readouterr().err == ""

    watcher._on_error = None
    watcher._handle_error(error)
    assert capsys.readouterr().err == "pyinc-tools watcher: callback raised: ValueError: broken\n"
