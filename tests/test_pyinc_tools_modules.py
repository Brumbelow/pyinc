from __future__ import annotations

import ast
import pickle
from pathlib import Path

import pytest

import pyinc_tools
import pyinc_tools._analysis as analysis
import pyinc_tools._edits as edits
import pyinc_tools._models as models
import pyinc_tools._workspace as workspace
import pyinc_tools.session as session
from pyinc.integrations import SourcePosition, SourceRange

_INTERNAL_MODULES = (
    "_analysis.py",
    "_document.py",
    "_edits.py",
    "_jsonrpc.py",
    "_models.py",
    "_workspace.py",
)


def test_internal_modules_do_not_import_session() -> None:
    package = Path(session.__file__).parent
    for filename in _INTERNAL_MODULES:
        tree = ast.parse((package / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(name.name != "pyinc_tools.session" for name in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in {"session", "pyinc_tools.session"}


def test_tools_only_use_the_public_integration_surface() -> None:
    package = Path(session.__file__).parent
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not name.name.startswith("pyinc.integrations.") for name in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.startswith("pyinc.integrations.")
            ):
                pytest.fail(f"{path.name} imports private module {node.module}")


def test_session_reexports_extracted_public_models() -> None:
    for name in models.__all__:
        value = getattr(models, name)
        assert getattr(session, name) is value
        if hasattr(pyinc_tools, name):
            assert getattr(pyinc_tools, name) is value


def test_model_pickle_identity_remains_session_compatible() -> None:
    source_range = SourceRange(SourcePosition(1, 2), SourcePosition(1, 3))
    edit = session.RenameEdit("mod.py", source_range, "renamed")
    restored = pickle.loads(pickle.dumps(edit))
    assert restored == edit
    assert type(restored) is session.RenameEdit
    assert session.RenameEdit.__module__ == "pyinc_tools.session"


def test_session_uses_extracted_helper_objects() -> None:
    namespace = vars(session)
    assert namespace["_parse_python"] is analysis._parse_python
    assert namespace["_compute_semantic_tokens"] is analysis._compute_semantic_tokens
    assert namespace["_alias_list_deletion_edits"] is edits._alias_list_deletion_edits
    assert session.PollingWorkspaceWatcher is workspace.PollingWorkspaceWatcher
