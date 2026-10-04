# Frequently Asked Questions

The questions that come up before the [kernel contract](kernel-contract.md)
does. Where a number is unpublished, this page says so and gives no estimate.

## How does this relate to Salsa or Adapton?

pyinc borrows its vocabulary (demand-driven queries, red-green verification,
early cutoff) from Salsa, the Rust incremental-computation framework that
rust-analyzer is built on. It is not a port of Salsa, shares no code with it,
and does not implement Adapton's algorithm. The lineage is in the idea, and the
idea has a Python history of its own.
[IncPy](https://www.usenix.org/conference/tapp-10/towards-practical-incremental-recomputation-scientists-implementation-python)
explored it in 2010, [Adapton](https://plum-umd.github.io/adapton/) listed a
Python implementation, and [Loman](https://pypi.org/project/loman/),
[Darl](https://pypi.org/project/darl/), and
[Cascade Query](https://github.com/hmatt1/cascade-query) each cover parts of
this ground.

The graph algorithm is the familiar one. The differences that matter are in
what a Python implementation has to enforce at runtime:

- Ownership of cached values. Safe Rust APIs can encode much of this
  discipline in the type system: a holder that has given up ownership of a
  cached value cannot mutate it. Even Rust expresses that as API design, with
  interior mutability as the exception. Python offers none of it, so pyinc
  converts every value crossing a cached boundary into an owned snapshot.
  `freeze` maps `list` → `FrozenList`, `dict` → `FrozenDict`, `set` →
  `FrozenSet`, and dataclasses → `FrozenRecord`, with registered
  `ValueAdapter`s for everything else. That is condition 1 of the guarantee,
  and it exists because the language lacks it. One visible consequence: a
  mapping that crosses a boundary comes back in a
  [canonical order](kernel-contract.md#1-value-boundary-ownership) that is
  neither insertion order nor sorted order. That order is what makes one value
  one cache key.
- Ambient reads. Reading a file or an environment variable directly inside a
  query, bypassing the declared inputs, silently breaks any incremental engine
  in any language. pyinc enforces the rule. While a query runs, it intercepts
  the calls [condition 2](kernel-contract.md#2-tracked-ambient-reads)
  enumerates (file opens, environment access, directory listings) and raises
  `UntrackedReadError` when the read is outside a `Resource`.
  [Explicit Limitations](kernel-contract.md#explicit-limitations) lists the
  reads the guard cannot see.

rust-analyzer is the reason this model is widely known, and it is a *consumer*
of Salsa, separate from it. The same split holds here. The kernel in
`src/pyinc` is domain-agnostic. The language server, watcher, and CLI live in
`pyinc_tools`, built on the public integration surface. See the
[package map](README.md#packages).

pyinc also persists a graph beyond one process. `save_checkpoint` writes a
content-addressed manifest that a later process can load. The kernel contract
spells out the trust boundary.

## Why not `functools.lru_cache`?

If your function is a pure function of its arguments, use `lru_cache`. It is in
the standard library, it is far cheaper than anything pyinc does, and pyinc
offers nothing in exchange.

pyinc is for a result that depends on something the arguments do not name:

- Invalidation. `lru_cache` keys on arguments, so it cannot know that a result
  also depended on a file, an environment variable, or another cached
  function. Nothing ever tells it to drop an entry. pyinc records the
  dependency graph while the code runs and invalidates outward from the input
  that changed.
- Early cutoff. When an input changes but the recomputed result is
  semantically equal to the stored one, pyinc **backdates** the record. Its
  revision stays where it was, so dependents stay valid and skip
  re-verification. A downstream `lru_cache` can still hit when it happens to
  receive an equal argument. What it lacks is the dynamic dependency graph and
  revision metadata. So nothing decides *without recomputing the chain* that
  dependents are still current, and nothing ever tells it to drop the stale
  entries. The 52 backdated nodes in the [demo](demo.md) are that effect.
- Ownership. `lru_cache` hands every caller the same object, so mutating a
  cached list corrupts every later hit. pyinc snapshots values at the
  boundary, which keeps cached state out of a caller's reach.
- Visible failures. A wrong `lru_cache` hit looks the same as a right one.
  pyinc raises `UntrackedReadError` at the moment a query reads untracked
  state, and `db.explain(...)` shows why each node was reused, recomputed, or
  backdated.

The cost is real. Every value crossing a boundary is frozen, and every
execution is recorded.

## What is the overhead?

No per-query overhead figure is published, and this page will not invent one.

What is published:

- The [demo](demo.md) numbers. A 270-file pytest checkout was analyzed in
  109.08 s from cold, then re-analyzed in 632 ms after a single-file edit,
  executing 74 queries and reusing 9,722. The timings are one recorded run on
  one machine. The counts were measured separately against the current build.
  The demo page states the full provenance.
- The [benchmark and correctness harness](../bench/README.md), which runs a
  fixed 67-row matrix comparing pyinc against fresh recomputation, an
  intentionally incomplete naive cache, and `joblib.Memory`.

No wall-clock table is published. Correctness and deterministic work counts
are release gates. Timings are environment-specific diagnostics and never
thresholds, so a figure measured on one CI runner would tell you little about
your machine. Measure on your own hardware:

```console
python -m pip install -e '.[bench]'
python -m bench.run --output bench/results --repetitions 5
```

The demo shows the shape of the trade on any machine. The first pass pays to
record the graph, and each later request is charged for what changed.

## What about the GIL, free-threaded builds, and multiprocessing?

### Threads

`Database` is thread-safe both across independent instances and on a single
shared instance. Each instance holds a `threading.RLock` that serializes every
public read and mutation. Threads sharing one `Database` serialize on that
lock, and threads holding separate instances each use only their own lock.
pyinc is pure Python and does nothing special with the GIL. On a default build,
neither arrangement buys parallel speedup for CPU-bound Python work. Threading
such work can be slower than running it on one thread, and parallel speedup
for that work needs the separate processes described below.

What threads buy is correctness. The ambient-read guard is installed globally
once and dispatches per context through a `ContextVar` stack. A query on one
`Database` leaves enforcement on another undisturbed, and raw I/O from a
thread outside every query is unaffected. See
[Thread Safety](kernel-contract.md#thread-safety).

### Free-threaded builds

The test matrix runs the suite, and nightly the property suite, on the
free-threaded CPython 3.14t build with the GIL disabled (`PYTHON_GIL=0`), on
Linux, macOS, and Windows. The rules above still apply. One `Database` still
serializes on its lock, and threads holding separate instances can now execute
at the same time. pyinc claims no speedup, so measure it for your workload.

The package metadata says as much with the
`Programming Language :: Python :: Free Threading :: 2 - Beta` classifier.
Free-threaded use is supported and tested. The support is new, so the
constraints documented so far may be incomplete.

From 3.14, a free-threaded build starts every thread with a copy of its
starter's context (`sys.flags.thread_inherit_context`). A request, a
`request_span` included, belongs to the thread that opened it. So the copy
never lets one thread answer from another's validation.

The two builds stay distinct. Query, resource, adapter, and input identities
all embed an interpreter and build payload that includes `sys.flags.gil`,
`sys.abiflags`, and the SOABI tag. So a free-threaded interpreter derives
different identities. It misses safely on a record or a checkpoint written
under a different build, and recomputes. On a free-threaded build
`sys.flags.gil` is `None`, `0`, or `1` as `PYTHON_GIL` is unset, `0`, or `1`,
so those three settings miss each other's checkpoints as well.

### Processes

A built-in worker pool, scheduler, and distributed execution are
[out of scope](#what-is-out-of-scope) by design. Separate processes hold
separate databases and run fully in parallel. They can share completed work
through checkpoints and a `FileSystemArtifactStore`: save in one process, load
in another. A cross-process test matrix exercises that path.

## What is out of scope?

`Database` is synchronous and serialized, with explicit keyed inputs, queries,
and resources. Built-in query scheduling, worker pools, async queries,
distributed execution, and interception of every possible ambient read stay
out of scope. Custom equality policy purity and `fast`-mode mutation hazards
remain caller contracts.

The shipped Python analysis is conservative and declaration-driven. It is
neither a type checker nor a formatter. An unsupported attribute shape gets no
navigation or refactoring result, and the analysis never guesses one. Remote
JSON Schema references, combinators beyond the two spellings the codegen guide
names, conditionals, and instance validation stay outside the code generator's
narrow subset. Watcher loops, mirror workspaces, protocol-position conversion,
and the LSP/JSON-RPC adapter belong to `pyinc_tools`. JSON Schema analysis
belongs to `pyinc_codegen`. Neither widens the domain-agnostic kernel contract.

## When should I not use pyinc?

- Your function is a pure function of its arguments. Use
  `functools.lru_cache`.
- The work does not decompose. Reuse needs boundaries to reuse across. One
  long opaque step offers nothing to cut.
- The run is one-shot. Recording a dependency graph is a cost recovered from
  the second request on. A process that computes once and exits only pays.
- You want parallel speedup out of one shared cache. Requests on a single
  `Database` serialize on its lock.
- Your values cannot be snapshotted cheaply. Everything crossing a cached
  boundary is frozen or handled by a `ValueAdapter`. Live handles, sockets,
  and very large mutable buffers are a poor fit.
- You need async queries, a built-in scheduler, or distributed execution.
  Coroutine and generator queries are rejected at decoration time, and the
  rest is out of scope.
- A stale answer is acceptable. Most of the kernel exists to make staleness
  impossible under the three conditions. Without that requirement, a simpler
  cache is cheaper.

The [integration contract](integration-contract.md) states the limits of the
shipped analysis.
