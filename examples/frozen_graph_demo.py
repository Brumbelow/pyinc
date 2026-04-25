from __future__ import annotations

from pyinc import FrozenGraph, freeze, thaw


def main() -> None:
    # 1. Pure tree: no shared identity, no cycles → flat snapshot shape, zero overhead.
    tree = {"name": "root", "items": [1, 2, 3]}
    tree_snapshot = freeze(tree)
    print(f"tree_is_FrozenGraph={isinstance(tree_snapshot, FrozenGraph)}")

    # 2. Shared identity: the same list appears at two slots of a parent dict.
    #    The flat v1 shape would silently turn this into two independent copies;
    #    FrozenGraph preserves identity across the boundary.
    shared_inner: list[int] = [10, 20]
    shared_parent = {"left": shared_inner, "right": shared_inner}
    shared_snapshot = freeze(shared_parent)
    shared_thawed = thaw(shared_snapshot)
    print(f"shared_is_FrozenGraph={isinstance(shared_snapshot, FrozenGraph)}")
    print(f"shared_left_is_right={shared_thawed['left'] is shared_thawed['right']}")
    shared_thawed["left"].append(30)
    print(f"shared_after_mutation_right={shared_thawed['right']}")

    # 3. Self-referential cycle: a list that contains itself round-trips faithfully.
    cyclic: list[object] = ["before"]
    cyclic.append(cyclic)
    cycle_snapshot = freeze(cyclic)
    cycle_thawed = thaw(cycle_snapshot)
    print(f"cycle_is_FrozenGraph={isinstance(cycle_snapshot, FrozenGraph)}")
    print(f"cycle_self_referential={cycle_thawed[1] is cycle_thawed}")
    print(f"cycle_first_element={cycle_thawed[0]}")


if __name__ == "__main__":
    main()
