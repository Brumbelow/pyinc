"""Fixed-seed fuzz of every text integration's file entrypoint.

Each integration gets 100 inputs built from one small valid document:
truncations, byte flips, repeated nesting, random printable text, and bytes
that are not UTF-8. One database per integration reads every input from the
same file, so each input after the first is an edit the kernel revalidates.

The entrypoint must return its result record. The integration contract names
a few inputs that raise out of an entrypoint called outside a query. The
generator leaves those inputs out, with a comment at each place, so any other
exception fails the test.
"""

from __future__ import annotations

import json
import random
import string
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from pyinc import Database
from pyinc.integrations import (
    ConfigAnalysis,
    CsvAnalysis,
    EnvFileAnalysis,
    JsonAnalysis,
    NotebookAnalysis,
    PythonFileAnalysis,
    RequirementsAnalysis,
    XmlAnalysis,
    config_analysis,
    csv_analysis,
    env_analysis,
    file_analysis,
    json_analysis,
    notebook_analysis,
    requirements_analysis,
    xml_analysis,
)

_SEED = 20261114
_INPUTS_PER_INTEGRATION = 100

# Depths straddle the TOML and JSON cap of 200 levels, the XML cap of 256, the
# tokenizer's 100 indentation levels and 200 parentheses, and the parser stack
# on 3.11.
_DEPTHS = (1, 2, 7, 50, 99, 100, 101, 199, 200, 201, 255, 256, 257, 300, 1000, 3000, 6000)

# Insertions stay at 64 characters, so no number exceeds the 4,300 digits int() converts.
_PRINTABLE = string.printable + "\u00e9\u20ac\U0001d518"

_NOT_UTF8 = (b"\xff", b"\xfe", b"\x80", b"\xc0\xaf", b"\xed\xa0\x80", b"\xe2\x82", b"\xf8\x88")


_PYTHON_SEED = '''\
"""Module docstring."""
from __future__ import annotations

import os
from collections import OrderedDict as OD
from . import sibling

__all__ = ["Point", "load"]


class Point:
    x: int = 0
    label: "str | None" = None

    def norm(self) -> float:
        return (self.x ** 2) ** 0.5


def load(path: str, *, mode: str = "r") -> dict[str, int]:
    with open(path, mode) as handle:
        return {line: len(line) for line in handle if line}
'''

_NOTEBOOK_SEED = json.dumps(
    {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n", "Some text.\n"]},
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "outputs": [],
                "source": ["import os\n", "%matplotlib inline\n", "def f(x):\n", "    return x\n"],
            },
            {"cell_type": "raw", "metadata": {}, "source": "raw text"},
        ],
        "metadata": {
            "kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    },
    indent=1,
)


def _notebook_with_cell(source: str) -> str:
    return json.dumps(
        {
            "cells": [{"cell_type": "code", "metadata": {}, "outputs": [], "source": source}],
            "metadata": {"kernelspec": {"name": "python3", "language": "python"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


_JSON_SEED = json.dumps(
    {
        "name": "example",
        "version": "0.1.0",
        "private": True,
        "scripts": {"build": "tsc", "test": "jest"},
        "dependencies": {"express": "^4.18.0"},
        "files": ["dist", "README.md"],
        "config": {"port": 3000, "ratio": 0.5, "debug": False, "token": None, "name": "caf\u00e9"},
    },
    indent=2,
)

_TOML_SEED = """\
[project]
name = "example"
version = "0.1.0"
dependencies = ["requests>=2.0", "click"]
released = 2026-11-14T08:30:00Z

[project.optional-dependencies]
dev = ["pytest", "mypy"]

[tool.ruff]
line-length = 100
inline = { a = 1, b = [1, 2] }

[[tool.matrix]]
python = "3.11"
"""

# The declaration names no encoding. A name expat cannot use raises LookupError or ValueError out.
_XML_SEED = """\
<?xml version="1.0"?>
<project xmlns:m="urn:example">
  <modelVersion>4.0.0</modelVersion>
  <m:groupId kind="org">com.example</m:groupId>
  <dependencies>
    <dependency scope="test">
      <artifactId>junit</artifactId>
      <version>4.13</version>
    </dependency>
  </dependencies>
</project>
"""

_CSV_SEED = """\
name,age,city
Alice,30,Portland
"Bob ""B"" Smith",25,"Seattle, WA"
Charlie,35,Denver
"""

_ENV_SEED = """\
# Database config
DB_HOST=localhost
export DB_PORT=5432
DB_NAME="myapp_dev"
SECRET_KEY='s3cr3t-value'
API_URL=${DB_HOST}/api # trailing comment
"""

_REQUIREMENTS_SEED = """\
# Core dependencies
requests>=2.28,<3.0
flask[async]>=2.3 ; python_version >= "3.8"
pywin32; sys_platform == "win32"
-e .
-r dev-requirements.txt
-c constraints.txt
--index-url https://pypi.org/simple/
numpy==1.26.4 \\
    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000000
pkg @ https://example.com/pkg-1.0.tar.gz
"""


def _python_indented(depth: int) -> str:
    # Indentation grows with the square of the depth, so it stops at 300 levels.
    levels = min(depth, 300)
    return (
        "".join("    " * level + "if x:\n" for level in range(levels)) + "    " * levels + "pass\n"
    )


@dataclass(frozen=True)
class _Integration:
    name: str
    filename: str
    seed: str
    nestings: tuple[Callable[[int], str], ...]
    analyze: Callable[[Database, str], object]
    result_type: type


_INTEGRATIONS = (
    _Integration(
        name="python_source",
        filename="module.py",
        seed=_PYTHON_SEED,
        nestings=(
            lambda n: _PYTHON_SEED + "x = " + "(" * n + "1" + ")" * n + "\n",
            lambda n: _PYTHON_SEED + "x = " + "1 + " * n + "1\n",
            lambda n: _PYTHON_SEED + "x = " + "[" * n + "]" * n + "\n",
            lambda n: _PYTHON_SEED + "x = " + "not " * n + "y\n",
            lambda n: _PYTHON_SEED + "x = " + "lambda: " * n + "0\n",
            lambda n: _PYTHON_SEED + "x = " + 'f"{' * n + "1" + '}"' * n + "\n",
            lambda n: _PYTHON_SEED + "x: '" + "list[" * n + "int" + "]" * n + "' = []\n",
            lambda n: _PYTHON_SEED + _python_indented(n),
        ),
        analyze=file_analysis,
        result_type=PythonFileAnalysis,
    ),
    _Integration(
        name="notebook",
        filename="analysis.ipynb",
        seed=_NOTEBOOK_SEED,
        nestings=(
            # JSON stops at 200 levels, as json.loads deeper can raise RecursionError out.
            lambda n: (
                '{"cells": [], "metadata": '
                + '{"a": ' * min(n, 200)
                + "1"
                + "}" * min(n, 200)
                + "}"
            ),
            lambda n: _notebook_with_cell("x = " + "(" * n + "1" + ")" * n + "\n"),
            lambda n: _notebook_with_cell("x = " + "1 + " * n + "1\n"),
            lambda n: _notebook_with_cell("%%time\n" + "x = " + "[" * n + "]" * n + "\n"),
            lambda n: _notebook_with_cell("!echo " + "(" * n + "\n" + _python_indented(n)),
        ),
        analyze=notebook_analysis,
        result_type=NotebookAnalysis,
    ),
    _Integration(
        name="json",
        filename="package.json",
        seed=_JSON_SEED,
        nestings=(
            lambda n: '{"name": "x", "config": ' + "[" * n + "]" * n + "}",
            lambda n: '{"a": ' * n + "1" + "}" * n,
            lambda n: "[" * n + "]" * n,
            lambda n: '{"a": ' + '[{"b": ' * n + "null" + "}]" * n + "}",
        ),
        analyze=json_analysis,
        result_type=JsonAnalysis,
    ),
    _Integration(
        name="toml",
        filename="pyproject.toml",
        seed=_TOML_SEED,
        nestings=(
            lambda n: _TOML_SEED + "nested = " + "[" * n + "]" * n + "\n",
            lambda n: _TOML_SEED + "nested = " + "{ b = " * n + "1" + " }" * n + "\n",
            lambda n: _TOML_SEED + "[" + ".".join(["t"] * n) + "]\nk = 1\n",
            lambda n: _TOML_SEED + ".".join(["k"] * n) + " = 1\n",
            lambda n: _TOML_SEED + "[[" + ".".join(["a"] * n) + "]]\nk = 1\n",
        ),
        analyze=config_analysis,
        result_type=ConfigAnalysis,
    ),
    _Integration(
        name="xml",
        filename="pom.xml",
        seed=_XML_SEED,
        nestings=(
            lambda n: _XML_SEED.replace("</project>", "<a>" * n + "</a>" * n + "</project>"),
            lambda n: _XML_SEED.replace(
                "</project>", '<m:a k="v">' * n + "text" + "</m:a>" * n + "</project>"
            ),
            lambda n: "<a>" * n + "</a>" * n,
            lambda n: "<a>" * n,
        ),
        analyze=xml_analysis,
        result_type=XmlAnalysis,
    ),
    _Integration(
        name="csv",
        filename="data.csv",
        seed=_CSV_SEED,
        nestings=(
            lambda n: _CSV_SEED + 'a,"' + '""' * n + '",c\n',
            lambda n: _CSV_SEED + '"' * n + "\n",
            lambda n: _CSV_SEED + '"' + "x,\n" * n + '",c\n',
            lambda n: _CSV_SEED + "," * n + "\n",
        ),
        analyze=csv_analysis,
        result_type=CsvAnalysis,
    ),
    _Integration(
        name="env",
        filename=".env",
        seed=_ENV_SEED,
        nestings=(
            lambda n: _ENV_SEED + "NESTED=" + "${" * n + "X" + "}" * n + "\n",
            lambda n: _ENV_SEED + "export " * n + "A=1\n",
            lambda n: _ENV_SEED + "QUOTED=" + '"' * n + "\n",
            lambda n: _ENV_SEED + "MIXED=" + "\"'" * n + "\n",
        ),
        analyze=env_analysis,
        result_type=EnvFileAnalysis,
    ),
    _Integration(
        name="requirements",
        filename="requirements.txt",
        seed=_REQUIREMENTS_SEED,
        nestings=(
            lambda n: _REQUIREMENTS_SEED + "pkg" + "[" * n + "]" * n + "\n",
            lambda n: (
                _REQUIREMENTS_SEED + "pkg; " + "(" * n + 'python_version >= "3"' + ")" * n + "\n"
            ),
            lambda n: _REQUIREMENTS_SEED + "pkg" + " \\\n" * n + ">=1.0\n",
            lambda n: (
                _REQUIREMENTS_SEED + "pkg; " + " and ".join(['os_name == "posix"'] * n) + "\n"
            ),
        ),
        analyze=requirements_analysis,
        result_type=RequirementsAnalysis,
    ),
)


def _truncate(rng: random.Random, integration: _Integration) -> tuple[str, bytes]:
    data = integration.seed.encode("utf-8")
    return "truncate", data[: rng.randrange(len(data))]


def _flip(rng: random.Random, integration: _Integration) -> tuple[str, bytes]:
    data = bytearray(integration.seed.encode("utf-8"))
    for _ in range(rng.randint(1, 8)):
        index = rng.randrange(len(data))
        # Only the low seven bits flip, so an ASCII byte stays ASCII and the
        # input still decodes as far as the seed did.
        data[index] ^= 1 << rng.randrange(7)
    return "flip", bytes(data)


def _nest(rng: random.Random, integration: _Integration) -> tuple[str, bytes]:
    text = rng.choice(integration.nestings)(rng.choice(_DEPTHS))
    if rng.random() < 0.25:
        text = text[: rng.randrange(len(text))]
    return "nest", text.encode("utf-8")


def _printable(rng: random.Random, integration: _Integration) -> tuple[str, bytes]:
    text = integration.seed
    start = rng.randrange(len(text) + 1)
    end = start if rng.random() < 0.5 else min(len(text), start + rng.randint(1, 64))
    inserted = "".join(rng.choice(_PRINTABLE) for _ in range(rng.randint(1, 64)))
    return "printable", (text[:start] + inserted + text[end:]).encode("utf-8")


def _not_utf8(rng: random.Random, integration: _Integration) -> tuple[str, bytes]:
    data = integration.seed.encode("utf-8")
    for _ in range(rng.randint(1, 4)):
        index = rng.randrange(len(data) + 1)
        high_byte = bytes([rng.randrange(0x80, 0x100)])
        inserted = rng.choice(_NOT_UTF8) if rng.random() < 0.5 else high_byte
        data = data[:index] + inserted + data[index:]
    return "not-utf8", data


_MUTATIONS = (_truncate, _flip, _nest, _printable, _not_utf8)


def _inputs(integration: _Integration) -> list[tuple[str, bytes]]:
    rng = random.Random(f"{_SEED}:{integration.name}")
    return [
        _MUTATIONS[index % len(_MUTATIONS)](rng, integration)
        for index in range(_INPUTS_PER_INTEGRATION)
    ]


def test_inputs_are_fixed_by_the_seed() -> None:
    for integration in _INTEGRATIONS:
        first = _inputs(integration)
        assert first == _inputs(integration)
        assert len(first) == _INPUTS_PER_INTEGRATION
        assert len(set(first)) > _INPUTS_PER_INTEGRATION * 0.9


# Mutated source with an invalid escape or number literal makes ast.parse warn.
@pytest.mark.filterwarnings("ignore::SyntaxWarning")
@pytest.mark.parametrize("integration", _INTEGRATIONS, ids=lambda item: item.name)
def test_entrypoint_returns_a_result_for_every_input(
    integration: _Integration, tmp_path: Path
) -> None:
    path = tmp_path / integration.filename
    db = Database()
    failures: list[str] = []
    for index, (kind, data) in enumerate(_inputs(integration)):
        path.write_bytes(data)
        try:
            result = integration.analyze(db, str(path))
        except Exception as exc:
            failures.append(f"input {index} ({kind}): {type(exc).__name__}: {exc}"[:300])
            continue
        if type(result) is not integration.result_type:
            failures.append(f"input {index} ({kind}): returned {type(result).__name__}")
    assert failures == []
