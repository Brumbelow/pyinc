"""Demonstrate cross-run cache reuse via save_checkpoint / load_checkpoint.

The checkpoint API lets a process serialise its entire node-record cache to an
ArtifactStore and reload it in a subsequent process, skipping re-execution for
any query whose declared inputs and resource probes are unchanged.

This script simulates two "runs" in a single process to make the behaviour
visible without spawning a subprocess.  In real use, each run would be a
separate invocation sharing the same FileSystemArtifactStore path.
"""

from __future__ import annotations

import tempfile

from pyinc import (
    Database,
    FileResource,
    FileSystemArtifactStore,
    Input,
    query,
)

_FILES = FileResource()

MULTIPLIER = Input[int]("multiplier")


@query
def config_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def word_count(db: Database, path: str) -> int:
    text = config_text(db, path)
    return len(text.split())


@query
def scaled_word_count(db: Database, path: str) -> int:
    return word_count(db, path) * MULTIPLIER.read(db)


def main() -> None:
    with (
        tempfile.TemporaryDirectory() as store_root,
        tempfile.TemporaryDirectory() as file_root,
    ):
        data_path = f"{file_root}/data.txt"
        store = FileSystemArtifactStore(store_root)

        # Write initial content.
        with open(data_path, "w") as f:
            f.write("alpha beta gamma delta epsilon")

        # -----------------------------------------------------------------------
        # Run 1: compute from scratch and save a checkpoint.
        # -----------------------------------------------------------------------
        db1 = Database(store=store)
        db1.set(MULTIPLIER, 3)
        result1 = db1.get(scaled_word_count, data_path)
        print(f"run1_result={result1}")  # 5 words * 3 = 15

        ck_key = db1.save_checkpoint()
        print(f"checkpoint_key={ck_key[:10]}...  (content-addressed)")

        stats1 = db1.statistics()
        print(f"run1_executions={stats1.query_executions}")  # 3 queries executed

        # -----------------------------------------------------------------------
        # Run 2: load the checkpoint — same inputs, all queries are reused.
        # -----------------------------------------------------------------------
        db2 = Database(store=store)
        db2.set(MULTIPLIER, 3)  # same input as run 1
        db2.load_checkpoint(ck_key)
        result2 = db2.get(scaled_word_count, data_path)
        print(f"run2_result={result2}")  # same result: 15

        node2 = db2.inspect(scaled_word_count, data_path)
        print(f"run2_decision={node2.last_recompute}")  # "reused" — no re-execution
        stats2 = db2.statistics()
        print(f"run2_executions={stats2.query_executions}")  # 0

        # -----------------------------------------------------------------------
        # Run 3: load checkpoint, change the multiplier.  Only scaled_word_count
        # re-executes (it depends on MULTIPLIER); word_count is reused because
        # the file content and its own dependencies are unchanged.
        # -----------------------------------------------------------------------
        db3 = Database(store=store)
        db3.set(MULTIPLIER, 10)  # different multiplier
        db3.load_checkpoint(ck_key)
        result3 = db3.get(scaled_word_count, data_path)
        print(f"run3_result={result3}")  # 5 words * 10 = 50

        node3 = db3.inspect(scaled_word_count, data_path)
        print(f"run3_decision={node3.last_recompute}")  # "executed"
        stats3 = db3.statistics()
        print(
            f"run3_executions={stats3.query_executions}"
        )  # 1 (only scaled_word_count)

        assert result1 == 15
        assert result2 == 15
        assert result3 == 50
        assert stats2.query_executions == 0
        assert node2.last_recompute == "reused"
        assert stats3.query_executions == 1
        assert node3.last_recompute == "executed"


if __name__ == "__main__":
    main()
