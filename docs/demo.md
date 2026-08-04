# Demo — pyinc on a Real Workspace

This page shows `pyinc-tools` running against a workspace nobody wrote for
`pyinc`: a checkout of pytest pinned at
`56b196e921acec0259d84622a570fde6032e15b5`, with nothing adapted for `pyinc` and
no configuration added.

![Editing pytest under pyinc's watcher](assets/demo.gif)

The watcher analyzes the tree once, then re-analyzes when edited files settle.
The stats pane shows both halves of that split: 109.08 s to analyze all 270
files from cold, then 632 ms to bring the graph back up to date after one edit
to `src/_pytest/warning_types.py` — 73 queries executed, 9,767 reused, and 47
backdated because recomputing them produced a semantically equal result. The
1,300 diagnostics are reported `undeclared-import` findings: imports of
installed distributions that pytest's own dependency metadata does not
declare. The count says what the tool reported, not that each finding is a
pytest defect.

![Two edits under pyinc's watcher](assets/demo-beats.gif)

Two edits back to back: a comment the engine absorbs as backdates, then an
unresolvable import that surfaces as a new diagnostic.

## Provenance

The clips are a single live recording, not a benchmark: one take, one run,
no repetitions. The wall-clock figures above are what the stats pane showed
during that take and are specific to the machine below; treat them as an
illustration of the cold/warm split, not as expected timings. Deterministic
work counts (executed / reused / backdated) are properties of the engine and
the edit, and are reproducible.

- **Workspace:** pytest pinned at `56b196e921acec0259d84622a570fde6032e15b5`,
  270 `.py` files, no configuration added.
- **Command:** `pyinc-tools analyze . --watch --format text`, edits made to
  `src/_pytest/warning_types.py` in a separate pane.
- **"Cold"** means a freshly started watcher process analyzing the tree for
  the first time: no prior in-memory state and no durable checkpoint.
- **Environment:** the 3.1 release lineage of `pyinc`, CPython 3.14.4 on
  Linux, Intel Core 7 240H (16 CPUs), local ext4 disk, machine otherwise
  idle.

For controlled, repeated measurements use the benchmark harness instead:
`python -m bench.run --output bench/results --repetitions 5` records exact
commit, Python build, OS, CPU, and repetition metadata alongside its results
(see [bench/README.md](../bench/README.md)).

Point it at a tree of your own:

```console
pyinc-tools analyze /path/to/workspace --watch --format text
```

The [tooling guide](pyinc-tools-guide.md) covers installation, overlays, output
shapes, and exit statuses. What this analysis does and does not do — nothing is
executed, resolution is static and conservative, and it is not a type checker —
is stated in the [integration contract](integration-contract.md).
