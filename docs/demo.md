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
1300 diagnostics are findings about pytest rather than tool noise: those visible
in the clip are `undeclared-import`, reporting imports of installed
distributions that pytest's own dependency metadata does not declare.

![Two edits under pyinc's watcher](assets/demo-beats.gif)

Two edits back to back: a comment the engine absorbs as backdates, then an
unresolvable import that surfaces as a new diagnostic.

Point it at a tree of your own:

```console
pyinc-tools analyze /path/to/workspace --watch --format text
```

The [tooling guide](pyinc-tools-guide.md) covers installation, overlays, output
shapes, and exit statuses. What this analysis does and does not do — nothing is
executed, resolution is static and conservative, and it is not a type checker —
is stated in the [integration contract](integration-contract.md).
