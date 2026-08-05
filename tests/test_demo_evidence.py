from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from scripts import demo_evidence

VERSION = importlib.metadata.version("pyinc")
COMMIT = "a" * 40
STATUS_SHA256 = hashlib.sha256(b" M README.md\n").hexdigest()
GENERATED_AT = datetime(2026, 8, 4, 12, 34, 56, 789, tzinfo=UTC)


def _write_examples(root: Path, sources: dict[str, str]) -> Path:
    examples = root / "examples"
    examples.mkdir(parents=True)
    for name, source in sources.items():
        (examples / name).write_text(source, encoding="utf-8")
    return root


def _fixed_git(_: Path) -> demo_evidence.GitSnapshot:
    return demo_evidence.GitSnapshot(
        commit_sha=COMMIT,
        working_tree_dirty=True,
        status_sha256=STATUS_SHA256,
    )


def _parse_checksums(document: bytes) -> dict[str, str]:
    return {
        line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in document.decode("ascii").splitlines()
    }


def test_capture_runs_all_examples_and_builds_deterministic_safe_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_examples(
        tmp_path / "project",
        {
            "z_empty.py": "pass\n",
            "a_raw.py": (
                "import sys\n"
                "sys.stdout.buffer.write(b'out\\xff\\n')\n"
                "sys.stderr.buffer.write(b'err\\x00\\n')\n"
            ),
        },
    )
    monkeypatch.setattr(demo_evidence, "_git_snapshot", _fixed_git)

    first_bundle, first_checksum = demo_evidence.capture(
        root,
        tmp_path / "first",
        Path(sys.executable),
        VERSION,
        generated_at_utc=GENERATED_AT,
    )
    second_bundle, second_checksum = demo_evidence.capture(
        root,
        tmp_path / "second",
        Path(sys.executable),
        VERSION,
        generated_at_utc=GENERATED_AT,
    )

    assert first_bundle.name == f"pyinc-{VERSION}-demo-evidence.zip"
    assert first_checksum.name == "DEMO-SHA256SUMS"
    assert first_bundle.read_bytes() == second_bundle.read_bytes()
    assert first_checksum.read_bytes() == second_checksum.read_bytes()
    assert _parse_checksums(first_checksum.read_bytes()) == {
        first_bundle.name: hashlib.sha256(first_bundle.read_bytes()).hexdigest()
    }

    with zipfile.ZipFile(first_bundle) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert set(names) == {
            "SHA256SUMS",
            "metadata.json",
            "runs.json",
            "runs/000.stderr",
            "runs/000.stdout",
            "runs/001.stderr",
            "runs/001.stdout",
        }
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            assert not path.is_absolute()
            assert ".." not in path.parts
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100644

        internal = _parse_checksums(archive.read("SHA256SUMS"))
        assert set(internal) == set(names) - {"SHA256SUMS"}
        for name, digest in internal.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest

        metadata = json.loads(archive.read("metadata.json"))
        assert metadata["schema_version"] == 1
        assert metadata["evidence_kind"] == "pyinc-demo"
        assert metadata["release_version"] == VERSION
        assert metadata["commit_sha"] == COMMIT
        assert metadata["working_tree_dirty"] is True
        assert metadata["git_status_sha256"] == STATUS_SHA256
        assert metadata["generated_at_utc"] == "2026-08-04T12:34:56.000789Z"
        assert metadata["python"]["executable"] == sys.executable
        assert metadata["python"]["version"] == sys.version.split()[0]
        assert metadata["os"]["system"]
        assert any(
            item["normalized_name"] == "pyinc" and item["version"] == VERSION
            for item in metadata["distribution_snapshot"]
        )

        runs = json.loads(archive.read("runs.json"))
        assert [run["example"] for run in runs] == [
            "examples/a_raw.py",
            "examples/z_empty.py",
        ]
        assert [run["exit_code"] for run in runs] == [0, 0]
        assert runs[0]["argv"] == [
            os.path.abspath(sys.executable),
            "examples/a_raw.py",
        ]
        assert runs[0]["cwd"] == os.fspath(root.resolve())
        assert runs[0]["source_sha256"] == hashlib.sha256(
            (root / "examples/a_raw.py").read_bytes()
        ).hexdigest()
        assert runs[0]["environment"] == {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
        assert runs[0]["environment_removed"] == [
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONSTARTUP",
        ]
        assert archive.read(runs[0]["stdout"]["path"]) == b"out\xff\n"
        assert archive.read(runs[0]["stderr"]["path"]) == b"err\x00\n"
        assert runs[0]["stdout"]["byte_length"] == 5
        assert runs[0]["stderr"]["byte_length"] == 5


def test_capture_runs_remaining_examples_but_fails_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_examples(
        tmp_path / "project",
        {
            "01_before.py": "from pathlib import Path\nPath('order').write_text('1')\n",
            "02_failure.py": (
                "from pathlib import Path\n"
                "import sys\n"
                "Path('order').write_text(Path('order').read_text() + '2')\n"
                "sys.stdout.buffer.write(b'failed-out')\n"
                "sys.stderr.buffer.write(b'failed-err')\n"
                "raise SystemExit(7)\n"
            ),
            "03_after.py": (
                "from pathlib import Path\n"
                "Path('order').write_text(Path('order').read_text() + '3')\n"
            ),
        },
    )
    monkeypatch.setattr(demo_evidence, "_git_snapshot", _fixed_git)

    with pytest.raises(demo_evidence.DemoExecutionError, match="02_failure.py=7") as caught:
        demo_evidence.capture(
            root,
            tmp_path / "output",
            Path(sys.executable),
            VERSION,
            generated_at_utc=GENERATED_AT,
        )

    assert (root / "order").read_text(encoding="utf-8") == "123"
    assert [run.exit_code for run in caught.value.runs] == [0, 7, 0]
    assert caught.value.runs[1].stdout == b"failed-out"
    assert caught.value.runs[1].stderr == b"failed-err"
    assert not (tmp_path / "output").exists()


def test_capture_rejects_interpreter_without_release_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_examples(tmp_path / "project", {"example.py": "pass\n"})
    monkeypatch.setattr(demo_evidence, "_git_snapshot", _fixed_git)
    actual = demo_evidence._interpreter_snapshot(root, Path(sys.executable))
    mismatched = demo_evidence.InterpreterSnapshot(
        python=actual.python,
        os=actual.os,
        distributions=tuple(
            item for item in actual.distributions if item.normalized_name != "pyinc"
        ),
    )
    monkeypatch.setattr(demo_evidence, "_interpreter_snapshot", lambda *_: mismatched)

    with pytest.raises(demo_evidence.DemoEvidenceError, match="not installed"):
        demo_evidence.capture(
            root,
            tmp_path / "output",
            Path(sys.executable),
            VERSION,
            generated_at_utc=GENERATED_AT,
        )
