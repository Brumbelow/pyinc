# Related Work and Positioning

Survey date: **2026-08-04**.

This is a bounded comparison of the primary paper, project, and release sources
listed below. It is not a systematic literature search or a complete package
index, so it supports no priority claim. A capability not described by a cited
source is recorded as *not stated*, not inferred to be absent.

## Survey targets

| System | Pinned survey target | What the primary source establishes |
|---|---|---|
| [IncPy](https://www.usenix.org/conference/tapp-10/towards-practical-incremental-recomputation-scientists-implementation-python) | TaPP 2010 paper and implementation snapshot. The proceedings page publishes no software release, tag, or commit, so no software version is assigned here. | A modified Python interpreter memoizes long-running function results to disk, records runtime dependencies on code, globals, and files, and reuses results across runs when its checks permit. |
| [Adapton](https://plum-umd.github.io/adapton/) | PLDI 2014 [paper and artifact](https://www.cs.umd.edu/projects/PL/adapton/). The official project index dates the legacy Python implementation to circa 2014 but supplies no release or commit pin for it. | A demanded computation graph records dependencies and repairs prior computations lazily in response to demand; the paper gives a from-scratch-consistency result for its formal incremental semantics. |
| [Loman](https://pypi.org/project/loman/0.6.0/) | Release **0.6.0**, source commit [`82670779ba7c48113c46b2fe4c583a9827ce2a84`](https://github.com/janushendersonassetallocation/loman/tree/82670779ba7c48113c46b2fe4c583a9827ce2a84), published 2026-04-26. PyPI provenance binds the sdist to that tag commit. | A Python computation DAG tracks node state and declared dependencies, supports selective on-demand full or partial recalculation, and can serialize computation state for later inspection or recovery. |
| [Cascade Query](https://pypi.org/project/query-cascade/0.2.4/) | Release **0.2.4**, source commit [`52bb5b40b249cccae507dfc884b33646062f1121`](https://github.com/hmatt1/cascade-query/tree/52bb5b40b249cccae507dfc884b33646062f1121), published 2026-04-27. PyPI provenance binds the sdist to that commit. | A demand-driven Python query graph records runtime dependencies, invalidates affected downstream queries, stops propagation when a recomputed value is equal, and persists inputs and cached results in SQLite. |
| [Calyxos](https://pypi.org/project/calyxos/0.4.1/) | Release **0.4.1**, sdist SHA-256 `90d7d5216e84752930bc974a00e820a397991a662447ae97aea7bce4da96b933`, published 2026-05-29. PyPI records no trusted-publishing source commit for this release, so the archive digest is the pin. | A Python object graph records runtime dependencies, invalidates lazily, applies equal-result early cutoff, and persists stored nodes while recomputing derived nodes after load. |

The pins identify exactly what was read; they do not imply that a newer branch,
an older release, or an uncited implementation has the same surface.

## Established algorithmic ground

Pull evaluation, dependency recording and verification, and equal-result early
cutoff/backdating are established incremental-computation techniques. Adapton's
PLDI 2014 paper documents demand-driven repair over a dependency graph. Salsa's
official [red-green algorithm](https://salsa-rs.github.io/salsa/reference/algorithm.html)
documents top-down dependency verification and backdating when recomputation
produces the prior value. The pinned Cascade Query and Calyxos releases also
document equal-result cutoff in Python systems.

`pyinc` therefore does not present the graph algorithm as its contribution.
The proposed contribution is the integration of that established algorithm
with a Python-specific assurance envelope.

## Scoped pyinc position

Among systems surveyed as of 2026-08-04, `pyinc` combines deep owned snapshots
including shared and cyclic graphs, strict/checked/fast enforcement modes,
enumerated ambient-read guards with explicit `Resource`s, implementation and
interpreter-build identities, trust-bounded content-addressed checkpoints,
declared-output `Action` reconciliation, and differential from-scratch-
consistency testing.

That sentence describes the integrated surface exercised by pyinc's contracts
and tests. It is not an exhaustive feature-absence statement about the other
systems. The relevant pyinc boundaries are specified in the
[kernel contract](kernel-contract.md), [action contract](action-contract.md),
and [architecture](architecture.md); benchmark correctness checks are described
in the [benchmark README](../bench/README.md).
