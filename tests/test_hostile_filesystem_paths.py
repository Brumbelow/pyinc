"""Filesystem shapes a caller can hand the library that no read should hang on."""

from __future__ import annotations

from pathlib import Path

from _hostile_paths import (
    make_fifo,
    posix_only,
    within_budget,
)

from pyinc.resources import FileResource


@posix_only
def test_a_readable_source_answers_within_the_budget(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    assert within_budget(lambda: FileResource().probe(str(source))) == "returned"
    assert FileResource().probe(str(source))[0] == "present"


@posix_only
def test_a_named_pipe_source_is_answered_rather_than_waited_on(tmp_path: Path) -> None:
    # A pipe with no writer never delivers a byte. The read has to answer
    # from the kind of the path instead of waiting for one.
    pipe = make_fifo(tmp_path / "pipe.py")
    assert within_budget(lambda: FileResource().probe(str(pipe))) == "BLOCKED"
