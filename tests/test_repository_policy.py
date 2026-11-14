"""How the repository splits its suite across CI jobs."""

from __future__ import annotations

import pytest
from conftest import in_shard, parse_shard

# Shaped like the node ids pytest writes, which use forward slashes on every
# operating system.
NODE_IDS = tuple(
    f"tests/test_module_{number % 13}.py::test_case_{number % 29}[{mode}-{number}]"
    for number in range(600)
    for mode in ("strict", "checked", "fast")
)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_the_test_shards_partition_the_collection(count: int) -> None:
    shards = []
    for index in range(1, count + 1):
        parsed = parse_shard(f"{index}/{count}")
        shards.append({node for node in NODE_IDS if in_shard(node, *parsed)})

    assert set().union(*shards) == set(NODE_IDS)
    assert sum(len(shard) for shard in shards) == len(NODE_IDS)
    assert all(shards)


@pytest.mark.parametrize(
    "value", ["", "2", "/4", "0/4", "5/4", "1/0", "a/4", "1/4/2", " 1/4", "-1/4", "1.0/4", "²/4"]
)
def test_a_malformed_test_shard_is_refused(value: str) -> None:
    with pytest.raises(pytest.UsageError, match="PYINC_TEST_SHARD"):
        parse_shard(value)
