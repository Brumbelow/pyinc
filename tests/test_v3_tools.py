from __future__ import annotations

import os
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

import pyinc_tools._workspace as workspace_module
from pyinc.integrations import SourcePosition, SourceRange
from pyinc_tools.lsp import LanguageServer
from pyinc_tools.session import (
    AnalysisDiagnostic,
    CallHierarchyCallSite,
    CallHierarchyItem,
    CodeActionEdit,
    CodeLens,
    DeclarationLocation,
    DocumentHighlight,
    DocumentLink,
    FileDeletionEdit,
    FileRenameEdit,
    FoldingRange,
    InlayHint,
    LinkedEditingRange,
    PollingWorkspaceWatcher,
    RenameEdit,
    SelectionRange,
    SemanticToken,
    TypeDefinitionLocation,
    TypeHierarchyItem,
    WorkspaceSession,
)


def _encoded_character(text: str, character: int, encoding: str) -> int:
    prefix = text[:character]
    if encoding == "utf-8":
        return len(prefix.encode("utf-8"))
    if encoding == "utf-16":
        return len(prefix.encode("utf-16-le")) // 2
    return len(prefix)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_lsp_negotiates_and_converts_position_encodings(tmp_path: Path, encoding: str) -> None:
    source = 'note = "😀"; target = 1\nprint(target)\n'
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")
    server = LanguageServer(default_root=str(tmp_path))
    try:
        initialized = server._handle_request(
            "initialize",
            {
                "rootUri": tmp_path.as_uri(),
                "capabilities": {"general": {"positionEncodings": [encoding]}},
                "initializationOptions": {"pyinc.watcher.enabled": False},
            },
        )
        assert initialized["capabilities"]["positionEncoding"] == encoding

        target_character = source.splitlines()[0].index("target")
        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {
                    "line": 0,
                    "character": _encoded_character(
                        source.splitlines()[0], target_character + 1, encoding
                    ),
                },
                "context": {"includeDeclaration": True},
            },
        )
        assert len(locations) == 2
        assert locations[0]["range"]["start"]["character"] == _encoded_character(
            source.splitlines()[0], target_character, encoding
        )
    finally:
        server._teardown_session()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_lsp_unicode_identifier_and_mixed_line_endings(tmp_path: Path, encoding: str) -> None:
    source = 'note = "😀"; café = 1\r\nprint(café)\n'
    path = tmp_path / "mod.py"
    path.write_bytes(source.encode("utf-8"))
    server = LanguageServer(default_root=str(tmp_path))
    try:
        server._handle_request(
            "initialize",
            {
                "rootUri": tmp_path.as_uri(),
                "capabilities": {"general": {"positionEncodings": [encoding]}},
                "initializationOptions": {"pyinc.watcher.enabled": False},
            },
        )
        declaration_line = source.splitlines()[0]
        start = declaration_line.index("café")
        locations = server._handle_request(
            "textDocument/references",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {
                    "line": 0,
                    "character": _encoded_character(declaration_line, start + 1, encoding),
                },
                "context": {"includeDeclaration": True},
            },
        )
        assert len(locations) == 2
        assert locations[0]["range"]["start"] == {
            "line": 0,
            "character": _encoded_character(declaration_line, start, encoding),
        }
        use_line = source.splitlines()[1]
        use_start = use_line.index("café")
        assert locations[1]["range"]["end"] == {
            "line": 1,
            "character": _encoded_character(use_line, use_start + len("café"), encoding),
        }
        renamed = server._handle_request(
            "textDocument/rename",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {
                    "line": 0,
                    "character": _encoded_character(declaration_line, start + 1, encoding),
                },
                "newName": "renamed",
            },
        )
        assert [edit["range"] for edit in renamed["changes"][path.as_uri()]] == [
            location["range"] for location in locations
        ]
    finally:
        server._teardown_session()


def test_lsp_did_save_advertises_disk_authoritative_text(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("value = 1\n", encoding="utf-8")
    server = LanguageServer(default_root=str(tmp_path))
    try:
        initialized = server._handle_request(
            "initialize",
            {
                "rootUri": tmp_path.as_uri(),
                "initializationOptions": {"pyinc.watcher.enabled": False},
            },
        )
        assert initialized["capabilities"]["textDocumentSync"]["save"] == {"includeText": False}
        server.publish_workspace_diagnostics = lambda: None  # type: ignore[method-assign]
        server._handle_notification(
            "textDocument/didOpen",
            {"textDocument": {"uri": path.as_uri(), "text": "value = 2\n"}},
        )
        path.write_text("value = 3\n", encoding="utf-8")
        server._handle_notification(
            "textDocument/didSave",
            {
                "textDocument": {"uri": path.as_uri()},
                "text": "value = 999\n",
            },
        )
        assert server._require_session().source_text(path) == "value = 3\n"
    finally:
        server._teardown_session()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_lsp_encodes_semantic_tokens_and_folding_characters(tmp_path: Path, encoding: str) -> None:
    lines = [
        "def fold():",
        "    target = 1",
        '    print("😀é", target)',
    ]
    path = tmp_path / "mod.py"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    server = LanguageServer(default_root=str(tmp_path))
    try:
        server._handle_request(
            "initialize",
            {
                "rootUri": tmp_path.as_uri(),
                "capabilities": {"general": {"positionEncodings": [encoding]}},
                "initializationOptions": {"pyinc.watcher.enabled": False},
            },
        )
        semantic = server._handle_request(
            "textDocument/semanticTokens/full",
            {"textDocument": {"uri": path.as_uri()}},
        )
        folding = server._handle_request(
            "textDocument/foldingRange",
            {"textDocument": {"uri": path.as_uri()}},
        )
    finally:
        server._teardown_session()

    chunks = [semantic["data"][index : index + 5] for index in range(0, len(semantic["data"]), 5)]
    target_start = lines[2].index("target")
    assert chunks[-1][:3] == [
        2,
        _encoded_character(lines[2], target_start, encoding),
        len("target"),
    ]
    assert folding == [
        {
            "startLine": 0,
            "startCharacter": 0,
            "endLine": 2,
            "endCharacter": _encoded_character(lines[2], len(lines[2]), encoding),
        }
    ]


def test_public_tool_results_expose_only_source_geometry() -> None:
    for result_type in (
        AnalysisDiagnostic,
        RenameEdit,
        FileRenameEdit,
        FileDeletionEdit,
        CodeActionEdit,
        DocumentHighlight,
        LinkedEditingRange,
        FoldingRange,
        SelectionRange,
        DocumentLink,
        CodeLens,
        TypeDefinitionLocation,
        DeclarationLocation,
        SemanticToken,
        CallHierarchyCallSite,
    ):
        names = {field.name for field in fields(result_type)}
        assert "range" in names
        assert names.isdisjoint(
            {
                "lineno",
                "col_offset",
                "end_col_offset",
                "start_line",
                "start_character",
                "end_line",
                "end_character",
            }
        )
    assert {field.name for field in fields(InlayHint)} >= {"position"}
    for hierarchy_type in (CallHierarchyItem, TypeHierarchyItem):
        names = {field.name for field in fields(hierarchy_type)}
        assert {"range", "selection_range"} <= names
        assert names.isdisjoint(
            {
                "range_start_line",
                "range_start_character",
                "range_end_line",
                "range_end_character",
                "selection_start_line",
                "selection_start_character",
                "selection_end_line",
                "selection_end_character",
            }
        )
    source_range = SourceRange(SourcePosition(0, 1), SourcePosition(0, 2))
    assert RenameEdit("mod.py", source_range, "x").range == source_range


def test_workspace_mirror_filters_files_and_honors_exclusions(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "asset.bin").write_bytes(b"not source")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "skip.py").write_text("value = 2\n", encoding="utf-8")

    with WorkspaceSession(tmp_path, exclude_globs=("generated/**",)) as session:
        mirror = Path(session.mirror_root)
        assert (mirror / "keep.py").is_file()
        assert not (mirror / "asset.bin").exists()
        assert not (mirror / "generated" / "skip.py").exists()


def test_workspace_mirror_rejects_escaping_file_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    link = tmp_path / "escape.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not available")

    with pytest.raises(ValueError, match="symlink escapes"):
        WorkspaceSession(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows junction")
def test_workspace_mirror_rejects_escaping_directory_junction(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-junction"
    outside.mkdir()
    (outside / "secret.py").write_text("secret = True\n", encoding="utf-8")
    junction = tmp_path / "linked"
    created = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {created.stderr.strip()}")
    try:
        with pytest.raises(ValueError, match="symlink escapes"):
            WorkspaceSession(tmp_path)
    finally:
        junction.rmdir()


def test_workspace_mirror_rejects_in_root_file_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    link = tmp_path / "alias.py"
    try:
        link.symlink_to(target.name)
    except OSError:
        pytest.skip("file symlinks are not available")

    with pytest.raises(ValueError, match="file symlinks are not supported"):
        WorkspaceSession(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO behavior")
def test_workspace_mirror_rejects_nonregular_source_without_blocking(
    tmp_path: Path,
) -> None:
    os.mkfifo(tmp_path / "pipe.py")
    with pytest.raises(ValueError, match="not a regular file"):
        WorkspaceSession(tmp_path)


def test_workspace_mirror_rechecks_source_at_copy_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.py"
    source.write_text("safe = True\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    original = workspace_module._read_workspace_file
    raced = False

    def swap_before_read(path: Path, root: Path) -> bytes:
        nonlocal raced
        if path == source and not raced:
            raced = True
            source.unlink()
            try:
                source.symlink_to(outside)
            except OSError:
                pytest.skip("file symlinks are not available")
        return original(path, root)

    monkeypatch.setattr(workspace_module, "_read_workspace_file", swap_before_read)
    with pytest.raises(ValueError, match="symlink"):
        WorkspaceSession(tmp_path)
    assert raced


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_workspace_mirror_rejects_opened_parent_rename_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    parent = root / "nested"
    outside = tmp_path / "outside"
    moved = outside / "moved-parent"
    parent.mkdir(parents=True)
    outside.mkdir()
    source = parent / "source.py"
    source.write_text("inside = True\n", encoding="utf-8")

    original_open = os.open
    raced = False

    def rename_after_traversal(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "nested" and dir_fd is not None and not raced:
            raced = True
            parent.rename(moved)
            (moved / "source.py").write_text("outside = True\n", encoding="utf-8")
            parent.mkdir()
            source.write_text("replacement = True\n", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(os, "open", rename_after_traversal)
    with pytest.raises(ValueError, match="changed identity"):
        workspace_module._read_workspace_file(source, root)

    assert raced
    assert source.read_text(encoding="utf-8") == "replacement = True\n"
    assert (moved / "source.py").read_text(encoding="utf-8") == "outside = True\n"


def test_workspace_mirror_rechecks_source_at_sync_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.py"
    source.write_text("safe = True\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-sync.py"
    outside.write_text("secret = True\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        original = workspace_module._read_workspace_file
        raced = False

        def swap_before_read(path: Path, root: Path) -> bytes:
            nonlocal raced
            if path == source and not raced:
                raced = True
                source.unlink()
                try:
                    source.symlink_to(outside)
                except OSError:
                    pytest.skip("file symlinks are not available")
            return original(path, root)

        monkeypatch.setattr(workspace_module, "_read_workspace_file", swap_before_read)
        with pytest.raises(ValueError, match="symlink"):
            session.refresh_paths([source])
        assert raced
        assert (Path(session.mirror_root) / "source.py").read_text(
            encoding="utf-8"
        ) == "safe = True\n"


def test_workspace_mirror_follows_recursive_requirement_files(tmp_path: Path) -> None:
    nested = tmp_path / "requirements"
    nested.mkdir()
    (tmp_path / "requirements.txt").write_text(
        "-r base.in  # shared dependencies\n-c constraints.custom # deployment constraints\n",
        encoding="utf-8",
    )
    (tmp_path / "base.in").write_text(
        "audit-base>=2\n-r requirements/base.lock # nested lock\n", encoding="utf-8"
    )
    (nested / "base.lock").write_text(
        "audit-lock==3\n-r final # extensionless input\n", encoding="utf-8"
    )
    (nested / "final").write_text("audit-final~=4\n", encoding="utf-8")
    (tmp_path / "constraints.custom").write_text("constraint-only==1\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        mirror = Path(session.mirror_root)
        assert (mirror / "base.in").is_file()
        assert (mirror / "requirements" / "base.lock").is_file()
        assert (mirror / "requirements" / "final").is_file()
        assert (mirror / "constraints.custom").is_file()
        inputs = session._dependency_inputs()

    assert inputs.requirements is not None
    assert inputs.requirements.diagnostics == ()
    assert set(inputs.declared_dependencies) >= {
        "audit-base>=2",
        "audit-lock==3",
        "audit-final~=4",
    }
    assert "constraint-only==1" not in inputs.declared_dependencies


def test_workspace_mirror_preserves_nul_reference_diagnostic(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_bytes(b"-r invalid\x00name.in\nrequests>=2\n")

    with WorkspaceSession(tmp_path) as session:
        mirrored = Path(session.mirror_root) / "requirements.txt"
        assert mirrored.read_bytes() == requirements.read_bytes()
        inputs = session._dependency_inputs()

    assert inputs.requirements is not None
    assert inputs.requirements.diagnostics == (
        ("unparseable-line", "line 1: -r invalid\x00name.in"),
    )
    assert inputs.declared_dependencies == ("requests>=2",)


@pytest.mark.parametrize(
    "reference",
    (".", "requirements.txt/child.in"),
    ids=("workspace-directory", "file-as-directory"),
)
def test_workspace_mirror_defers_nonfile_reference_diagnostics(
    tmp_path: Path, reference: str
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"-r {reference}\nrequests>=2\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        mirrored = Path(session.mirror_root) / "requirements.txt"
        assert mirrored.read_bytes() == requirements.read_bytes()
        inputs = session._dependency_inputs()

    assert inputs.requirements is not None
    assert any(
        code == "missing-requirements-file" for code, _message in inputs.requirements.diagnostics
    )
    assert inputs.declared_dependencies == ("requests>=2",)


@pytest.mark.parametrize(
    ("requirements", "code", "message_prefix", "named_file"),
    [
        (
            "-r missing.in\n",
            "missing-requirements-file",
            "referenced requirements file is missing: ",
            "missing.in",
        ),
        ("-r cycle.in\n", "cycle", "circular -r reference: ", "requirements.txt"),
    ],
)
def test_workspace_surfaces_recursive_requirements_diagnostics(
    tmp_path: Path, requirements: str, code: str, message_prefix: str, named_file: str
) -> None:
    root_requirements = tmp_path / "requirements.txt"
    root_requirements.write_text(requirements, encoding="utf-8")
    if code == "cycle":
        (tmp_path / "cycle.in").write_text("-r requirements.txt\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        diagnostics = session.analyze_workspace().diagnostics

    matching = [diagnostic for diagnostic in diagnostics if diagnostic.code == code]
    assert len(matching) == 1
    assert matching[0].path == str(root_requirements)
    assert matching[0].source == "pyinc.requirements_txt"
    assert matching[0].severity == "error"
    # These messages name the file inline, so they need the same remapping the
    # `path` field gets.
    assert matching[0].message == f"{message_prefix}{tmp_path / named_file}"
    assert session.mirror_root not in matching[0].message


def test_workspace_remaps_out_of_project_requirements_reference(tmp_path: Path) -> None:
    """A `-r` target that escapes the root resolves *beside* the mirror, not under
    it, so remapping the mirror root alone leaves the temporary directory in the
    message.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "requirements.txt").write_text("-r ../outside.in\n", encoding="utf-8")

    with WorkspaceSession(root) as session:
        diagnostics = session.analyze_workspace().diagnostics

    matching = [diagnostic for diagnostic in diagnostics if diagnostic.code == "error"]
    assert len(matching) == 1
    assert matching[0].message == f"-r path outside project: {tmp_path / 'outside.in'}"
    assert session.mirror_root not in matching[0].message
    assert "pyinc-tools-" not in matching[0].message


def test_workspace_keeps_a_two_level_out_of_project_reference_deterministic(
    tmp_path: Path,
) -> None:
    """A `-r` target that escapes two levels resolves above the mirror's parent.

    The remap re-anchors the mirror root and its parent, so this message still
    names the path the mirror layout resolved to rather than the workspace one.
    What it must never do is vary between runs or leak the mirror's random
    component.
    """

    root = tmp_path / "nested" / "workspace"
    root.mkdir(parents=True)
    (root / "requirements.txt").write_text("-r ../../twoup.txt\n", encoding="utf-8")

    messages: list[tuple[str, ...]] = []
    for _ in range(2):
        with WorkspaceSession(root) as session:
            messages.append(
                tuple(
                    diagnostic.message
                    for diagnostic in session.analyze_workspace().diagnostics
                    if diagnostic.code == "error"
                )
            )
            assert session.mirror_root not in messages[-1][0]

    assert len(messages[0]) == 1
    assert messages[0] == messages[1]
    assert "pyinc-tools-" not in messages[0][0]


def test_workspace_mirror_rejects_referenced_file_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-requirements.in"
    outside.write_text("outside-package==1\n", encoding="utf-8")
    referenced = tmp_path / "base.in"
    try:
        referenced.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not available")
    (tmp_path / "requirements.txt").write_text("-r base.in\n", encoding="utf-8")

    with pytest.raises(ValueError, match="symlink"):
        WorkspaceSession(tmp_path)


def test_source_text_honors_pep_263_encoding(tmp_path: Path) -> None:
    path = tmp_path / "latin1.py"
    path.write_bytes(b"# -*- coding: latin-1 -*-\nvalue = 'caf\xe9'\n")

    with WorkspaceSession(tmp_path) as session:
        assert (
            session.source_text(path)
            == "# -*- coding: latin-1 -*-\nvalue = 'caf\N{LATIN SMALL LETTER E WITH ACUTE}'\n"
        )
        assert not session.analyze_file(path).diagnostics


def test_overlay_preserves_pep_263_encoding(tmp_path: Path) -> None:
    path = tmp_path / "latin1.py"
    path.write_bytes(b"# coding: latin-1\nvalue = 'caf\xe9'\n")
    overlay = "# coding: latin-1\nr\N{LATIN SMALL LETTER E WITH ACUTE}sum\N{LATIN SMALL LETTER E WITH ACUTE} = 'caf\N{LATIN SMALL LETTER E WITH ACUTE}'\n"

    with WorkspaceSession(tmp_path) as session:
        session.set_overlay(path, overlay)
        result = session.analyze_file(path)
        assert session.source_text(path) == overlay
        assert not result.diagnostics
        assert result.symbols is not None
        assert [symbol.qualified_name for symbol in result.symbols.symbols] == [
            "r\N{LATIN SMALL LETTER E WITH ACUTE}sum\N{LATIN SMALL LETTER E WITH ACUTE}"
        ]


@pytest.mark.parametrize(
    "source",
    [
        "def foo(value: int) -> int:\r    return value\rfoo(1)\r",
        "def foo(value: int) -> int:\r\n    return value\r# ignored(decoy)\rfoo(1)\n",
    ],
    ids=["cr-only", "mixed"],
)
def test_signature_scanner_handles_all_python_line_endings(tmp_path: Path, source: str) -> None:
    path = tmp_path / "mod.py"
    path.write_bytes(source.encode("utf-8"))
    call_line = len(source.splitlines()) - 1

    with WorkspaceSession(tmp_path) as session:
        signature = session.signature_help_at(path, call_line, len("foo(1"))

    assert signature is not None
    assert signature.label == "def foo(value: int) -> int"


def test_cr_only_selection_and_import_deletion_ranges(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_bytes(b"value = 1\r")
    consumer = tmp_path / "consumer.py"
    consumer.write_bytes(b"before = 1\rimport helper\rafter = 2\r")

    with WorkspaceSession(tmp_path) as session:
        selections = session.selection_ranges_at(consumer, 2, len("after"))
        edits = session.import_edits_for_file_deletions([helper])

    assert selections
    assert selections[0].range.start.line == 2
    assert len(edits) == 1
    assert edits[0].range == SourceRange(SourcePosition(1, 0), SourcePosition(2, 0))


def test_local_declaration_and_type_definition_use_lexical_binding(
    tmp_path: Path,
) -> None:
    source = (
        "class GlobalType:\n"
        "    pass\n\n"
        "class LocalType:\n"
        "    pass\n\n"
        "value: GlobalType\n\n"
        "def run(parameter: LocalType) -> LocalType:\n"
        "    value: LocalType = LocalType()\n"
        "    return value\n"
    )
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")
    server = LanguageServer(default_root=str(tmp_path))
    try:
        server._handle_request(
            "initialize",
            {
                "rootUri": tmp_path.as_uri(),
                "initializationOptions": {"pyinc.watcher.enabled": False},
            },
        )
        params = {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": 10, "character": len("    return ") + 1},
        }
        declaration = server._handle_request("textDocument/declaration", params)
        assert declaration == [
            {
                "uri": path.as_uri(),
                "range": {
                    "start": {"line": 9, "character": 4},
                    "end": {"line": 9, "character": 9},
                },
            }
        ]
        type_definition = server._handle_request("textDocument/typeDefinition", params)
        assert type_definition == [
            {
                "uri": path.as_uri(),
                "range": {
                    "start": {"line": 3, "character": len("class ")},
                    "end": {"line": 3, "character": len("class LocalType")},
                },
            }
        ]
        parameter_type = server._handle_request(
            "textDocument/typeDefinition",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {"line": 8, "character": len("def run(") + 1},
            },
        )
        assert parameter_type == type_definition
    finally:
        server._teardown_session()


def test_proven_deep_annotation_resolves_type_definition(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    provider = package / "sub.py"
    provider.write_text("class Foo:\n    pass\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text("import pkg.sub\nvalue: pkg.sub.Foo\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        symbol_id = session.symbol_at(consumer, SourcePosition(1, 1))
        assert symbol_id is not None
        locations = session.type_definitions_at(symbol_id)

    assert locations == (
        TypeDefinitionLocation(
            path=str(provider),
            range=SourceRange(
                SourcePosition(0, len("class ")),
                SourcePosition(0, len("class Foo")),
            ),
        ),
    )


@pytest.mark.parametrize(
    "prefix",
    ["", "import pkg.sub\npkg = object()\n"],
    ids=["unimported", "rebound"],
)
def test_unproven_deep_annotation_has_no_type_definition(tmp_path: Path, prefix: str) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text(prefix + "value: pkg.sub.Foo\n", encoding="utf-8")
    line = prefix.count("\n")

    with WorkspaceSession(tmp_path) as session:
        symbol_id = session.symbol_at(consumer, SourcePosition(line, 1))
        assert symbol_id is not None
        assert not session.type_definitions_at(symbol_id)


def test_local_rename_stays_inside_lexical_scope(tmp_path: Path) -> None:
    provider = tmp_path / "provider.py"
    provider.write_text("shared = 1\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text("from provider import shared\nprint(shared)\n", encoding="utf-8")
    local = tmp_path / "local.py"
    local.write_text(
        "def run() -> int:\n    shared = 2\n    return shared\n",
        encoding="utf-8",
    )

    with WorkspaceSession(tmp_path) as session:
        symbol_id = session.symbol_at(local, SourcePosition(2, len("    return ") + 1))
        assert symbol_id is not None
        result = session.rename_symbol(symbol_id, "renamed")

    assert result.status == "ok"
    assert {Path(edit.path).name for edit in result.edits} == {"local.py"}
    assert {edit.range.start.line for edit in result.edits} == {1, 2}


def test_completion_prefers_local_shadow_details(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    source = (
        "def helper(value: int) -> int:\n"
        "    return value\n\n"
        "def run() -> None:\n"
        "    helper: str = ''\n"
        "    hel\n"
    )
    path.write_text(source, encoding="utf-8")
    with WorkspaceSession(tmp_path) as session:
        items = session.completions_at(path, 5, len("    hel"))

    matching = [item for item in items if item.label == "helper"]
    assert len(matching) == 1
    assert matching[0].kind == "variable"
    assert matching[0].detail == "helper: str"


def test_attribute_completion_rejects_rebound_and_unimported_owners(
    tmp_path: Path,
) -> None:
    (tmp_path / "lib.py").write_text("member = 1\n", encoding="utf-8")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text("member = 1\n", encoding="utf-8")
    rebound = tmp_path / "rebound.py"
    rebound.write_text("import lib\nlib = object()\nlib.\n", encoding="utf-8")
    unimported = tmp_path / "unimported.py"
    unimported.write_text("pkg.sub.\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        assert session.completions_at(rebound, 2, len("lib.")) == ()
        assert session.completions_at(unimported, 0, len("pkg.sub.")) == ()


def test_dotted_class_completion_requires_the_matching_import(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text(
        "class Worker:\n    def execute(self) -> None:\n        pass\n",
        encoding="utf-8",
    )
    (package / "other.py").write_text("value = 1\n", encoding="utf-8")
    path = tmp_path / "app.py"
    path.write_text("import pkg.other\npkg.sub.Worker.\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        assert session.completions_at(path, 1, len("pkg.sub.Worker.")) == ()


def test_semantic_tokens_respect_local_shadow(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "def helper() -> int:\n"
        "    return 1\n\n"
        "def run() -> int:\n"
        "    helper = 2\n"
        "    return helper\n",
        encoding="utf-8",
    )
    with WorkspaceSession(tmp_path) as session:
        tokens = session.semantic_tokens_for_file(path)

    shadow_use = next(token for token in tokens if token.range.start.line == 5)
    assert shadow_use.token_type == "variable"


def test_normalized_function_geometry_is_exact_across_tool_features(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mod.py"
    path.write_text("def e\u0301():\n    return 1\n\ne\u0301()\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        tokens = [
            token
            for token in session.semantic_tokens_for_file(path)
            if token.token_type == "function"
        ]
        hierarchy = session.prepare_call_hierarchy(path, 0, 5)

    assert [token.range for token in tokens] == [
        SourceRange(SourcePosition(0, 4), SourcePosition(0, 6)),
        SourceRange(SourcePosition(3, 0), SourcePosition(3, 2)),
    ]
    assert len(hierarchy) == 1
    assert hierarchy[0].selection_range == SourceRange(SourcePosition(0, 4), SourcePosition(0, 6))


def test_proven_dotted_module_chain_navigates_references_and_renames(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target = package / "sub.py"
    target.write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
    consumer = tmp_path / "app.py"
    consumer.write_text("import pkg.sub\n\npkg.sub.run()\n", encoding="utf-8")
    server = LanguageServer(default_root=str(tmp_path))
    try:
        server._handle_request(
            "initialize",
            {
                "rootUri": tmp_path.as_uri(),
                "initializationOptions": {"pyinc.watcher.enabled": False},
            },
        )
        position = {
            "textDocument": {"uri": consumer.as_uri()},
            "position": {"line": 2, "character": len("pkg.sub.") + 1},
        }
        assert server._handle_request("textDocument/definition", position) == [
            {
                "uri": target.as_uri(),
                "range": {
                    "start": {"line": 0, "character": len("def ")},
                    "end": {"line": 0, "character": len("def run")},
                },
            }
        ]
        references = server._handle_request(
            "textDocument/references",
            {**position, "context": {"includeDeclaration": True}},
        )
        assert {entry["uri"] for entry in references} == {
            consumer.as_uri(),
            target.as_uri(),
        }
        renamed = server._handle_request("textDocument/rename", {**position, "newName": "execute"})
        assert set(renamed["changes"]) == {consumer.as_uri(), target.as_uri()}
    finally:
        server._teardown_session()


def test_proven_dotted_chain_serves_signature_and_hierarchies(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text(
        "def run(value: int) -> int:\n    return value\n\nclass Worker:\n    pass\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "app.py"
    consumer.write_text(
        "import pkg.sub\n"
        "def caller():\n"
        "    return pkg.sub.run(1)\n"
        "worker = pkg.sub.Worker\n"
        "class Apprentice(pkg.sub.Worker):\n"
        "    pass\n",
        encoding="utf-8",
    )

    with WorkspaceSession(tmp_path) as session:
        signature = session.signature_help_at(consumer, 2, len("    return pkg.sub.run(1"))
        calls = session.prepare_call_hierarchy(consumer, 2, len("    return pkg.sub.") + 1)
        types = session.prepare_type_hierarchy(consumer, 3, len("worker = pkg.sub.") + 1)
        outgoing = session.call_hierarchy_outgoing_calls(consumer, "caller")
        hints = session.inlay_hints_for_file(consumer)
        supertypes = session.type_hierarchy_supertypes(consumer, "Apprentice")
        subtypes = session.type_hierarchy_subtypes(package / "sub.py", "Worker")

    assert signature is not None
    assert signature.label == "def run(value: int) -> int"
    assert [item.qualified_name for item in calls] == ["run"]
    assert [item.qualified_name for item in types] == ["Worker"]
    assert [call.callee.qualified_name for call in outgoing] == ["run"]
    assert [hint.label for hint in hints] == ["value:"]
    assert [item.qualified_name for item in supertypes] == ["Worker"]
    assert [item.qualified_name for item in subtypes] == ["Apprentice"]


def test_hierarchy_prepare_resolves_nested_declarations(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "class Outer:\n"
        "    class Inner:\n"
        "        pass\n\n"
        "    def method(self) -> None:\n"
        "        pass\n",
        encoding="utf-8",
    )

    with WorkspaceSession(tmp_path) as session:
        types = session.prepare_type_hierarchy(path, 1, len("    class ") + 1)
        calls = session.prepare_call_hierarchy(path, 4, len("    def ") + 1)

    assert [item.qualified_name for item in types] == ["Outer.Inner"]
    assert [item.qualified_name for item in calls] == ["Outer.method"]


@pytest.mark.parametrize(
    "prefix",
    ["", "import pkg.sub\npkg = object()\n"],
    ids=["unimported", "rebound"],
)
def test_unproven_dotted_chain_is_rejected_by_signature_and_hierarchies(
    tmp_path: Path,
    prefix: str,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text(
        "def run(value: int) -> int:\n    return value\n\nclass Worker:\n    pass\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "app.py"
    consumer.write_text(
        prefix
        + "def caller():\n"
        + "    return pkg.sub.run(1)\n"
        + "worker = pkg.sub.Worker\n"
        + "class Apprentice(pkg.sub.Worker):\n"
        + "    pass\n",
        encoding="utf-8",
    )
    call_line = prefix.count("\n") + 1

    with WorkspaceSession(tmp_path) as session:
        assert (
            session.signature_help_at(consumer, call_line, len("    return pkg.sub.run(1")) is None
        )
        assert not session.prepare_call_hierarchy(
            consumer, call_line, len("    return pkg.sub.") + 1
        )
        assert not session.prepare_type_hierarchy(
            consumer, call_line + 1, len("worker = pkg.sub.") + 1
        )
        assert not session.call_hierarchy_outgoing_calls(consumer, "caller")
        assert not session.inlay_hints_for_file(consumer)
        assert not session.type_hierarchy_supertypes(consumer, "Apprentice")


def test_unproven_or_rebound_dotted_chain_does_not_navigate(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
    unimported = tmp_path / "unimported.py"
    unimported.write_text("pkg.sub.run()\n", encoding="utf-8")
    rebound = tmp_path / "rebound.py"
    rebound.write_text("import pkg.sub\npkg = object()\npkg.sub.run()\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        assert session.symbol_at(unimported, SourcePosition(0, len("pkg.sub.") + 1)) is None
        assert session.symbol_at(rebound, SourcePosition(2, len("pkg.sub.") + 1)) is None


def test_watcher_detects_same_stat_content_change(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("value = 1\n", encoding="utf-8")
    original = path.stat()

    with WorkspaceSession(tmp_path) as session:
        watcher = PollingWorkspaceWatcher(session, debounce_ms=0)
        path.write_text("value = 2\n", encoding="utf-8")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert watcher.poll() == (str(path.resolve()),)
        assert session.source_text(path) == "value = 2\n"


def test_watcher_detects_same_stat_recursive_requirement_change(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r base.in\n", encoding="utf-8")
    referenced = tmp_path / "base.in"
    referenced.write_text("audit-one==1\n", encoding="utf-8")
    original = referenced.stat()

    with WorkspaceSession(tmp_path) as session:
        watcher = PollingWorkspaceWatcher(session, debounce_ms=0)
        referenced.write_text("audit-two==2\n", encoding="utf-8")
        os.utime(referenced, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert watcher.poll() == (str(referenced.resolve()),)
        inputs = session._dependency_inputs()

    assert "audit-two==2" in inputs.declared_dependencies
    assert "audit-one==1" not in inputs.declared_dependencies


def test_thousand_module_workspace_has_bounded_incremental_work(
    tmp_path: Path,
) -> None:
    for index in range(1_000):
        (tmp_path / f"module_{index:04d}.py").write_text(f"value = {index}\n", encoding="utf-8")

    with WorkspaceSession(tmp_path) as session:
        initial = session.analyze_workspace()
        assert len(initial.python.modules) == 1_000
        before = session.db.statistics()
        assert before.node_count < 25_000

        changed = tmp_path / "module_0500.py"
        session.set_overlay(changed, "value = -1\n")
        updated = session.analyze_workspace()
        after = session.db.statistics()
        assert len(updated.python.modules) == 1_000
        assert after.query_executions - before.query_executions < 100
        assert after.node_count < 25_000
