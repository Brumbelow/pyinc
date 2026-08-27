# Frequently Asked Questions

The questions that come up before the [kernel contract](kernel-contract.md)
does. Where a number is not published, this page says so instead of estimating
one.

## How does this relate to Salsa or Adapton?

pyinc borrows its vocabulary — demand-driven queries, red-green verification,
early cutoff — from Salsa, the Rust incremental-computation framework that
rust-analyzer is built on. It is not a port of Salsa, shares no code with it,
and does not implement Adapton's algorithm. The lineage is in the idea, not the
implementation.

The differences that matter are not in the graph algorithm, which is the
familiar one. They are in what a Python implementation has to enforce at
runtime:

- **Ownership of cached values.** Safe Rust APIs can encode much of this
  discipline in the type system — a value handed out of a cache is not
  mutable by a holder that no longer owns it, though interior mutability
  means even Rust expresses that as API design rather than an absolute.
  Python offers none of it, so pyinc converts every value crossing a
  cached boundary into an owned snapshot — `freeze` maps `list` →
  `FrozenList`, `dict` → `FrozenDict`, `set` → `FrozenSet`, and dataclasses →
  `FrozenRecord`, with registered `ValueAdapter`s for everything else. That is
  condition 1 of the guarantee, and it exists because the language does not
  provide it. One visible consequence: a mapping that crosses a boundary comes
  back in a canonical order derived from each key's snapshot digest —
  deterministic across processes and platforms, but neither insertion order nor
  sorted order — because the order is what makes one value one cache key. Sets
  are ordered by the same rule inside the snapshot, but a thawed `set` is an
  ordinary unordered Python set and carries no order back out.
- **Ambient reads.** Reading a file or an environment variable directly inside
  a query, rather than through a declared input, silently breaks any
  incremental engine in any language. pyinc does not only document the rule:
  while a query runs it intercepts `builtins.open`, `io.open`, `os.getenv`,
  `os.environ`, `os.listdir`, `os.scandir`, and `Path.iterdir`, and raises
  `UntrackedReadError` when the read is not inside a `Resource`. That is
  condition 2. The reads the guard cannot see are enumerated in
  [Explicit Limitations](kernel-contract.md#explicit-limitations) rather than
  left implied.

rust-analyzer is the reason this model is widely known, and it is a *consumer*
of Salsa rather than part of it. The same split holds here: the kernel in
`src/pyinc` is domain-agnostic, and the language-server, watcher, and CLI live
in `pyinc_tools`, built on the public integration surface. See
[Architecture](architecture.md).

pyinc also persists a graph beyond one process: `save_checkpoint` writes a
content-addressed manifest that a later process can load, with the trust
boundary spelled out in the kernel contract.

## Why not `functools.lru_cache`?

If your function is a pure function of its arguments, use `lru_cache`. It is in
the standard library, it is far cheaper than anything pyinc does, and pyinc
offers nothing in exchange.

pyinc is for the case where that is not true — where a result depends on
something the arguments do not name:

- **Invalidation.** `lru_cache` keys on arguments, so it cannot know that a
  result also depended on a file, an environment variable, or another cached
  function. Nothing ever tells it to drop an entry. pyinc records the
  dependency graph while the code runs and invalidates outward from the input
  that changed.
- **Early cutoff.** When an input changes but the recomputed result is
  semantically equal to the stored one, pyinc **backdates** the record: its
  revision does not advance, so dependents stay valid and are never even
  re-verified. A downstream `lru_cache` can still hit when it happens to
  receive an equal argument — what it lacks is the dynamic dependency graph
  and revision metadata, so nothing decides *without recomputing the chain*
  that dependents are still current, and nothing ever tells it to drop the
  entries that are not. The 52 backdated nodes in the [demo](demo.md) are
  that effect.
- **Ownership.** `lru_cache` hands every caller the same object; mutating a
  cached list corrupts every later hit. pyinc snapshots values at the boundary,
  so a caller cannot reach back into cached state.
- **Failures that are visible.** A wrong `lru_cache` hit looks exactly like a
  right one. pyinc raises `UntrackedReadError` at the moment a query reads
  untracked state, and `db.explain(...)` shows why each node was reused,
  recomputed, or backdated.

The cost is real: every value crossing a boundary is frozen, and every
execution is recorded.

## What is the overhead?

There is no published per-query overhead figure, and this page will not invent
one.

What is published:

- The [demo](demo.md) numbers — a 270-file pytest checkout analyzed in 109.08 s
  from cold, then re-analyzed in 632 ms after a single-file edit, executing 74
  queries and reusing 9,830. The timings are one recorded run on one machine;
  the counts were measured separately against the current build. The demo page
  states the full provenance.
- The [benchmark and correctness harness](../bench/README.md), which runs a
  fixed 67-row matrix comparing pyinc against fresh recomputation, an
  intentionally incomplete naive cache, and `joblib.Memory`.

What is deliberately not published is a wall-clock table. Correctness and
deterministic work counts are release gates; timings are treated as
environment-specific diagnostics and are never thresholds, so a figure measured
on one CI runner would not tell you much about your machine. Measure on your
own hardware:

```console
python -m pip install -e '.[bench]'
python -m bench.run --output bench/results --repetitions 5
```

The shape of the trade is visible in the demo regardless of the machine: the
first pass pays to record the graph, and each later request is charged for what
actually changed.

## What about the GIL, free-threaded builds, and multiprocessing?

**Threads.** `Database` is thread-safe both across independent instances and on
a single shared instance. Each instance holds a `threading.RLock` that
serializes every public read and mutation, so threads sharing one `Database`
serialize on that lock while threads holding separate instances run in
parallel. The ambient-read guard is installed globally exactly once and
dispatches per context through a `ContextVar` stack, so a query on one
`Database` does not disturb enforcement on another, and raw I/O from a thread
that is not inside a query is unaffected. See
[Thread Safety](kernel-contract.md#thread-safety).

pyinc is pure Python and does nothing special with the GIL. Sharing one
`Database` across threads buys correctness, not parallel speedup.

**Free-threaded builds.** The test matrix covers CPython 3.11–3.14 on the
default build; it does not currently include a free-threaded build, so pyinc
does not claim to be verified there. What is guaranteed is that the two are
never confused. Query, resource, adapter, and input identities all embed an
interpreter and build payload that includes `sys.flags.gil`, `sys.abiflags`,
and the SOABI tag, so a free-threaded interpreter derives different identities:
it misses safely and recomputes rather than reusing a record — or a checkpoint
— written under a different build.

**Processes.** There is no built-in worker pool, scheduler, or distributed
execution; that is [out of scope](architecture.md#scope) by design. Separate
processes hold separate databases and run fully in parallel, and they can share
completed work through checkpoints and a `FileSystemArtifactStore`: save in one
interpreter, load in another. That path is exercised by a cross-process test
matrix.

## When should I not use pyinc?

- **Your function is a pure function of its arguments.** Use
  `functools.lru_cache`.
- **The work does not decompose.** Reuse needs boundaries to reuse across. One
  long opaque step offers nothing to cut.
- **The run is one-shot.** Recording a dependency graph is a cost recovered on
  the second request, not the first. A process that computes once and exits
  only pays.
- **You want parallel speedup out of one shared cache.** Requests on a single
  `Database` serialize on its lock.
- **Your values cannot be snapshotted cheaply.** Everything crossing a cached
  boundary is frozen or handled by a `ValueAdapter`; live handles, sockets, and
  very large mutable buffers are a poor fit.
- **You need async queries, a built-in scheduler, or distributed execution.**
  Coroutine and generator queries are rejected at decoration time, and the rest
  is out of scope.
- **A stale answer is acceptable.** Most of the kernel exists to make staleness
  impossible under the three conditions. Without that requirement, a simpler
  cache is cheaper.

Separately, the shipped Python analysis in `pyinc.integrations` is conservative
and declaration-driven. It is not a type checker, and it returns no result
rather than guessing; the
[integration contract](integration-contract.md) states the limits.
