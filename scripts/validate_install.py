"""Smoke-test an installed wheel and a generated cyclic model package."""

from __future__ import annotations

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
from collections.abc import MutableSequence
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


def _validate_sdist(version: str) -> None:
    archives = tuple(Path("dist").glob(f"pyinc-{version}.tar.gz"))
    assert len(archives) == 1
    prefix = f"pyinc-{version}"
    required = {
        f"{prefix}/bench/__init__.py",
        f"{prefix}/bench/harness.py",
        f"{prefix}/scripts/__init__.py",
        f"{prefix}/scripts/validate_install.py",
        f"{prefix}/scripts/verify_release_metadata.py",
        f"{prefix}/tests/test_bench_smoke.py",
        f"{prefix}/tests/test_release_metadata.py",
    }
    with tarfile.open(archives[0], mode="r:gz") as archive:
        names = frozenset(archive.getnames())
    assert required <= names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def main() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    installed = importlib.metadata.distribution("pyinc")
    assert installed.version == project["version"]
    assert all("extra ==" in requirement for requirement in installed.requires or ())
    _validate_sdist(installed.version)

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


if __name__ == "__main__":
    main()
