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
| pyinc (incremental) | 8.25 | 31.6 | ✅ yes | 1745.1× slower |
| full recompute | 0.00 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.01 | 0.5 | ✅ yes | 2.1× slower |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 0.74 | 4.5 | ✅ yes | 196.4× slower |
| full recompute | 0.00 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.00 | 0.1 | ✅ yes | 1.1× slower |

### Localized edit

_change one value used by one output — only that output recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 7.34 | 13.0 | ✅ yes | 1430.0× slower |
| full recompute | 0.01 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.01 | 0.1 | ✅ yes | 1.2× slower |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 7.83 | 14.5 | ✅ yes | 1114.0× slower |
| full recompute | 0.01 | 0.4 | ✅ yes | baseline |
| naive per-key cache | 0.01 | 0.1 | ⚠️ **STALE** | 1.3× faster |

### Checkpoint restore

_warm a fresh database from a saved checkpoint instead of recomputing_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1.99 | 32.3 | ✅ yes | — |

## calc-with-includes fixture

A small include-aware expression language reconciled to disk — a realistic workload where incremental reuse pays off.

### Cold build

_first run with an empty cache — everything computes from scratch_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 70.35 | 82.3 | ✅ yes | 1.2× slower |
| full recompute | 58.91 | 69.8 | ✅ yes | baseline |
| naive per-key cache | 60.46 | 70.0 | ✅ yes | ≈ baseline |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 4.59 | 9.8 | ✅ yes | 13.3× faster |
| full recompute | 61.07 | 69.6 | ✅ yes | baseline |
| naive per-key cache | 0.03 | 0.8 | ✅ yes | 2106.5× faster |

### Edit an unused file

_change a file nothing depends on — no downstream work should run_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 4.53 | 9.6 | ✅ yes | 12.8× faster |
| full recompute | 58.14 | 69.6 | ✅ yes | baseline |
| naive per-key cache | 0.03 | 0.8 | ✅ yes | 1938.2× faster |

### Comment-only edit

_edit only comments/whitespace of a referenced file — should backdate to zero downstream work_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 6.15 | 13.2 | ✅ yes | 9.4× faster |
| full recompute | 57.83 | 69.6 | ✅ yes | baseline |
| naive per-key cache | 58.45 | 69.9 | ✅ yes | ≈ baseline |

### Localized edit

_change one value used by one output — only that output recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 64.56 | 53.3 | ✅ yes | 1.1× slower |
| full recompute | 58.61 | 69.6 | ✅ yes | baseline |
| naive per-key cache | 58.10 | 69.9 | ✅ yes | ≈ baseline |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 61.19 | 53.5 | ✅ yes | 1.1× slower |
| full recompute | 57.78 | 69.7 | ✅ yes | baseline |
| naive per-key cache | 58.16 | 69.9 | ✅ yes | ≈ baseline |

### Remove an artifact

_stop declaring a previously emitted output — it is deleted from disk_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 44.55 | 43.9 | ✅ yes | 1.1× faster |
| full recompute | 48.92 | 69.2 | ✅ yes | baseline |
| naive per-key cache | 49.99 | 69.4 | ✅ yes | ≈ baseline |

### Tampered output

_an out-of-band edit corrupts a generated file — content-hash repair restores it_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 3.72 | 9.6 | ✅ yes | 13.3× faster |
| full recompute | 49.62 | 69.2 | ✅ yes | baseline |
| naive per-key cache | 0.03 | 0.8 | ⚠️ **STALE** | 1699.1× faster |

### Checkpoint restore

_warm a fresh database from a saved checkpoint instead of recomputing_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 9.76 | 72.4 | ✅ yes | — |

## JSON-Schema codegen

The JSON-Schema → typed-Python compiler. Edits touch only the affected models, so incremental runs stay well under a full recompile.

### Cold build

_first run with an empty cache — everything computes from scratch_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 76.24 | 76.6 | ✅ yes | ≈ baseline |
| full recompute | 73.38 | 70.6 | ✅ yes | baseline |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 14.43 | 14.0 | ✅ yes | 5.0× faster |
| full recompute | 72.24 | 69.7 | ✅ yes | baseline |

### Comment-only edit

_edit only comments/whitespace of a referenced file — should backdate to zero downstream work_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 15.12 | 16.3 | ✅ yes | 4.8× faster |
| full recompute | 72.00 | 70.1 | ✅ yes | baseline |

### Localized edit

_change one value used by one output — only that output recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 37.75 | 42.2 | ✅ yes | 1.9× faster |
| full recompute | 72.06 | 69.9 | ✅ yes | baseline |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 46.68 | 44.8 | ✅ yes | 1.6× faster |
| full recompute | 76.24 | 69.9 | ✅ yes | baseline |

### Remove an artifact

_stop declaring a previously emitted output — it is deleted from disk_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 58.97 | 54.1 | ✅ yes | 1.1× slower |
| full recompute | 55.00 | 60.8 | ✅ yes | baseline |

### Tampered output

_an out-of-band edit corrupts a generated file — content-hash repair restores it_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 11.65 | 13.5 | ✅ yes | 4.7× faster |
| full recompute | 54.93 | 60.7 | ✅ yes | baseline |

### Checkpoint restore

_warm a fresh database from a saved checkpoint instead of recomputing_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 17.72 | 92.4 | ✅ yes | — |

## Action reconciliation

Declared-output reconciliation: only changed files are written, and tampered outputs are repaired via content hash.

### Cold build

_first run with an empty cache — everything computes from scratch_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 3.31 | 14.7 | ✅ yes | 1.1× slower |
| full recompute | 3.01 | 14.3 | ✅ yes | baseline |

### No-op rebuild

_re-run with nothing changed — everything should be reused_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 2.10 | 7.0 | ✅ yes | 1.4× faster |
| full recompute | 3.00 | 14.3 | ✅ yes | baseline |

### Shared edit, high fan-out

_change one input many outputs depend on — every dependent recomputes_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 3.59 | 11.9 | ✅ yes | 1.2× slower |
| full recompute | 3.02 | 14.7 | ✅ yes | baseline |

### Remove an artifact

_stop declaring a previously emitted output — it is deleted from disk_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1.95 | 10.2 | ✅ yes | 1.1× faster |
| full recompute | 2.20 | 12.9 | ✅ yes | baseline |

### Tampered output

_an out-of-band edit corrupts a generated file — content-hash repair restores it_

| engine | wall (ms) | peak (KiB) | correct? | speedup |
|---|---|---|---|---|
| pyinc (incremental) | 1.81 | 6.8 | ✅ yes | 1.3× faster |
| full recompute | 2.35 | 10.9 | ✅ yes | baseline |
