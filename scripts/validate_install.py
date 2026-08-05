"""Smoke-test an installed wheel and a generated cyclic model package."""

from __future__ import annotations

import argparse
import compileall
import importlib
import importlib.metadata
import json
import pkgutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
from collections.abc import MutableSequence, Sequence
from pathlib import Path
from typing import Protocol

import pyinc
import pyinc_codegen
import pyinc_tools
from pyinc import Database
from pyinc_codegen import generate
from pyinc_tools.lsp import LanguageServer


class _Package(Protocol):
    __name__: str
    __path__: MutableSequence[str]


def _import_package_tree(package: _Package) -> None:
    prefix = f"{package.__name__}."
    for module in pkgutil.walk_packages(package.__path__, prefix):
        importlib.import_module(module.name)


def _validate_sdist(archive_path: Path, version: str) -> None:
    assert archive_path.name == f"pyinc-{version}.tar.gz"
    prefix = f"pyinc-{version}"
    project_root = Path(__file__).resolve().parents[1]
    included_roots = ("src", "tests", "examples", "docs", "bench", "scripts")
    required_paths = {
        path.relative_to(project_root).as_posix()
        for root_name in included_roots
        for path in (project_root / root_name).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    required_paths.update({"README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml"})
    required = {f"{prefix}/{path}" for path in required_paths}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        names = frozenset(archive.getnames())
    missing = sorted(required - names)
    assert not missing, f"sdist is missing configured source files: {missing}"
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version")
    parser.add_argument("--sdist", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    expected_version = arguments.expected_version
    if expected_version is None:
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
        expected_version = project["version"]
    assert isinstance(expected_version, str)
    installed = importlib.metadata.distribution("pyinc")
    assert installed.version == expected_version
    assert all("extra ==" in requirement for requirement in installed.requires or ())
    if arguments.sdist is not None:
        _validate_sdist(arguments.sdist, installed.version)

    executable_name = "pyinc-tools.exe" if sys.platform == "win32" else "pyinc-tools"
    executable = Path(sysconfig.get_path("scripts")) / executable_name
    version_result = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert version_result.stdout.strip() == f"pyinc-tools {installed.version}"

    for package in (pyinc, pyinc_codegen, pyinc_tools):
        _import_package_tree(package)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        server = LanguageServer(default_root=str(root))
        try:
            initialized = server._handle_request(
                "initialize",
                {
                    "rootUri": root.as_uri(),
                    "capabilities": {},
                    "initializationOptions": {"pyinc.watcher.enabled": False},
                },
            )
            assert initialized["serverInfo"] == {
                "name": "pyinc-tools",
                "version": installed.version,
            }
        finally:
            server._teardown_session()

        schema_path = root / "schema.json"
        output = root / "generated"
        schema_path.write_text(
            json.dumps(
                {
                    "$defs": {
                        "A": {
                            "type": "object",
                            "properties": {"b": {"$ref": "#/$defs/B"}},
                        },
                        "B": {
                            "type": "object",
                            "properties": {"a": {"$ref": "#/$defs/A"}},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        generate(Database(), schema_path, output)
        assert compileall.compile_dir(output, quiet=1)
        sys.path.insert(0, str(root))
        try:
            importlib.import_module("generated")
            importlib.import_module("generated.a")
            importlib.import_module("generated.b")
        finally:
            sys.path.pop(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
