from __future__ import annotations

import tempfile
from pathlib import Path

from pyinc import (
    Database,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    Input,
    deserialize_snapshot,
    freeze,
    query,
    semantic_equal,
    serialize_snapshot,
    thaw,
)

WORDS = Input[tuple[str, ...]]("words")


@query
def upper_words(db: Database) -> tuple[str, ...]:
    return tuple(word.upper() for word in WORDS.read(db))


def main() -> None:
    in_memory = InMemoryArtifactStore()
    db = Database(store=in_memory)
    db.set(WORDS, ("alpha", "beta", "gamma"))

    result = db.get(upper_words)
    print(f"result={result}")
    print(f"in_memory_object_count={len(in_memory.keys())}")

    # The same database can target a filesystem store; layout is a git-style
    # two-character fan-out of the snapshot fingerprint digest.
    with tempfile.TemporaryDirectory() as root:
        on_disk = FileSystemArtifactStore(root)
        on_disk_db = Database(store=on_disk)
        on_disk_db.set(WORDS, ("alpha", "beta", "gamma"))
        on_disk_db.get(upper_words)

        fanout = sorted(
            f"{prefix.name}{suffix.name}"
            for prefix in (Path(root) / "objects").iterdir()
            for suffix in prefix.iterdir()
        )
        print(f"on_disk_object_count={len(fanout)}")

    # serialize_snapshot / deserialize_snapshot round-trip is independent of any
    # Database; external tools can persist or transfer the byte form directly.
    snapshot = freeze(("hello", "world"))
    payload = serialize_snapshot(snapshot)
    restored = thaw(deserialize_snapshot(payload))
    print(f"round_trip={restored}")
    print(f"round_trip_equal={semantic_equal(restored, ('hello', 'world'))}")


if __name__ == "__main__":
    main()
