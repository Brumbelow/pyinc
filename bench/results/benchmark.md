# pyinc benchmark

Each scenario applies one canonical edit and times every engine on it. The **correct?** column compares that engine's output against a fresh, cache-free recomputation of the same scenario.

**pyinc is correct in every scenario below.** The comparators (`full` recompute, naive per-key cache, `joblib.Memory`) are included to show the trade-off: a naive cache can be faster than pyinc yet serve a **stale** result where a real dependency changed.

## ⚠️ Stale results (fast but wrong)

These comparator runs finished quickly but returned output that does **not** match a fresh recomputation — the exact failure pyinc prevents:

- **naive per-key cache** on *Synthetic query graph → Shared edit, high fan-out* (change one input many outputs depend on — every dependent recomputes)
- **naive per-key cache** on *calc-with-includes fixture → Tampered output* (an out-of-band edit corrupts a generated file — content-hash repair restores it)

## Scenarios

| scenario | what it does |
|---|---|
| **Cold build** | first run with an empty cache — everything computes from scratch |
| **No-op rebuild** | re-run with nothing changed — everything should be reused |
| **Edit an unused file** | change a file nothing depends on — no downstream work should run |
| **Comment-only edit** | edit only comments/whitespace of a referenced file — should backdate to zero downstream work |
| **Localized edit** | change one value used by one output — only that output recomputes |
| **Shared edit, high fan-out** | change one input many outputs depend on — every dependent recomputes |
| **Remove an artifact** | stop declaring a previously emitted output — it is deleted from disk |
| **Tampered output** | an out-of-band edit corrupts a generated file — content-hash repair restores it |
| **Checkpoint restore** | warm a fresh database from a saved checkpoint instead of recomputing |

## Metrics

| column | meaning |
|---|---|
| wall (ms) | wall-clock time for the run (CSV in seconds, table in milliseconds) |
| peak (KiB) | peak traced memory during the run, in KiB |
| graph edges | edges in pyinc's dependency graph (pyinc only) |
| memo nodes | memoized nodes pyinc is holding — inputs, resources, and queries (pyinc only) |
| correct? | does the engine's output equal a fresh, cache-free run? pyinc is always yes |
| speedup | wall time relative to the `full` recompute for that scenario |

Engines: `pyinc` — pyinc (incremental), `full` — full recompute, `naive` — naive per-key cache.

## Synthetic query graph

A minimal query graph. Its full-recompute baseline is a trivial arithmetic sum measured in microseconds, so pyinc is *slower* in absolute terms here — this target checks graph mechanics and correctness, not raw speed.

### Cold build

_first run with an empty cache — everything computes from scratch_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2.59 | 46.6 | ✅ yes | 1334.6× slower |
| full recompute | 0.00 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.00 | 0.4 | ✅ yes | 1.4× slower |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 0.14 | 3.8 | ✅ yes | 92.4× slower |
| full recompute | 0.00 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.00 | 0.0 | ✅ yes | 1.3× faster |

### Localized edit

_change one value used by one output — only that output recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 0.71 | 25.5 | ✅ yes | 72.7× slower |
| full recompute | 0.01 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.00 | 0.0 | ✅ yes | 7.3× faster |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1.01 | 48.8 | ✅ yes | 577.7× slower |
| full recompute | 0.00 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.00 | 0.0 | ⚠️ **STALE** | 1.7× faster |

### Checkpoint restore

_warm a fresh database from a saved checkpoint instead of recomputing_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 10.07 | 75.2 | ✅ yes | — |

## calc-with-includes fixture

A small include-aware expression language reconciled to disk — a realistic workload where incremental reuse pays off.

### Cold build

_first run with an empty cache — everything computes from scratch_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1643.79 | 1676.2 | ✅ yes | 1.1× slower |
| full recompute | 1520.74 | 1457.0 | ✅ yes | baseline |
| naive per-key cache | 1527.19 | 1526.8 | ✅ yes | ≈ baseline |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2.74 | 280.1 | ✅ yes | 557.3× faster |
| full recompute | 1528.37 | 1552.2 | ✅ yes | baseline |
| naive per-key cache | 0.01 | 0.6 | ✅ yes | 113120.8× faster |

### Edit an unused file

_change a file nothing depends on — no downstream work should run_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2.72 | 280.0 | ✅ yes | 561.3× faster |
| full recompute | 1524.30 | 1526.0 | ✅ yes | baseline |
| naive per-key cache | 0.01 | 0.6 | ✅ yes | 105436.9× faster |

### Comment-only edit

_edit only comments/whitespace of a referenced file — should backdate to zero downstream work_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 120.73 | 587.2 | ✅ yes | 12.6× faster |
| full recompute | 1520.54 | 1451.2 | ✅ yes | baseline |
| naive per-key cache | 1525.12 | 1462.8 | ✅ yes | ≈ baseline |

### Localized edit

_change one value used by one output — only that output recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 126.96 | 588.8 | ✅ yes | 12.0× faster |
| full recompute | 1526.01 | 1525.9 | ✅ yes | baseline |
| naive per-key cache | 1525.47 | 1526.2 | ✅ yes | ≈ baseline |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 129.41 | 591.8 | ✅ yes | 11.8× faster |
| full recompute | 1530.38 | 1525.8 | ✅ yes | baseline |
| naive per-key cache | 1537.99 | 1526.1 | ✅ yes | ≈ baseline |

### Remove an artifact

_stop declaring a previously emitted output — it is deleted from disk_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 125.73 | 588.7 | ✅ yes | 12.2× faster |
| full recompute | 1538.61 | 1462.5 | ✅ yes | baseline |
| naive per-key cache | 1542.57 | 1526.2 | ✅ yes | ≈ baseline |

### Tampered output

_an out-of-band edit corrupts a generated file — content-hash repair restores it_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2.22 | 275.5 | ✅ yes | 690.3× faster |
| full recompute | 1535.63 | 1525.9 | ✅ yes | baseline |
| naive per-key cache | 0.01 | 0.6 | ⚠️ **STALE** | 116388.6× faster |

### Checkpoint restore

_warm a fresh database from a saved checkpoint instead of recomputing_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 868.56 | 1410.0 | ✅ yes | — |

## JSON-Schema codegen

The JSON-Schema → typed-Python compiler. Edits touch only the affected models, so incremental runs stay well under a full recompile.

### Cold build

_first run with an empty cache — everything computes from scratch_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2975.44 | 1844.1 | ✅ yes | ≈ baseline |
| full recompute | 2940.69 | 1877.5 | ✅ yes | baseline |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 9.53 | 338.4 | ✅ yes | 309.1× faster |
| full recompute | 2944.56 | 1874.0 | ✅ yes | baseline |

### Comment-only edit

_edit only comments/whitespace of a referenced file — should backdate to zero downstream work_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 128.47 | 468.0 | ✅ yes | 23.0× faster |
| full recompute | 2960.23 | 1882.2 | ✅ yes | baseline |

### Localized edit

_change one value used by one output — only that output recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 137.78 | 573.2 | ✅ yes | 21.5× faster |
| full recompute | 2957.11 | 1867.6 | ✅ yes | baseline |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 139.22 | 607.0 | ✅ yes | 21.1× faster |
| full recompute | 2940.22 | 1868.2 | ✅ yes | baseline |

### Remove an artifact

_stop declaring a previously emitted output — it is deleted from disk_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 136.22 | 577.7 | ✅ yes | 21.5× faster |
| full recompute | 2928.00 | 1844.1 | ✅ yes | baseline |

### Tampered output

_an out-of-band edit corrupts a generated file — content-hash repair restores it_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 8.09 | 323.9 | ✅ yes | 361.0× faster |
| full recompute | 2921.56 | 1843.2 | ✅ yes | baseline |

### Checkpoint restore

_warm a fresh database from a saved checkpoint instead of recomputing_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 3596.32 | 1896.5 | ✅ yes | — |

## Action reconciliation

Declared-output reconciliation: only changed files are written, and tampered outputs are repaired via content hash.

### Cold build

_first run with an empty cache — everything computes from scratch_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2.47 | 277.5 | ✅ yes | ≈ baseline |
| full recompute | 2.37 | 277.4 | ✅ yes | baseline |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1.45 | 268.8 | ✅ yes | 1.6× faster |
| full recompute | 2.32 | 277.4 | ✅ yes | baseline |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2.23 | 285.0 | ✅ yes | ≈ baseline |
| full recompute | 2.31 | 277.4 | ✅ yes | baseline |

### Remove an artifact

_stop declaring a previously emitted output — it is deleted from disk_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1.51 | 269.3 | ✅ yes | 1.3× faster |
| full recompute | 1.89 | 272.4 | ✅ yes | baseline |

### Tampered output

_an out-of-band edit corrupts a generated file — content-hash repair restores it_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1.28 | 267.1 | ✅ yes | 1.5× faster |
| full recompute | 1.95 | 272.4 | ✅ yes | baseline |
