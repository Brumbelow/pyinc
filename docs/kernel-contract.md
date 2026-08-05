# Kernel Contract — Soundness Envelope

`pyinc` is a correctness-first, in-memory incremental query kernel. This
document defines the guarantee it makes and the exact conditions under which the
guarantee holds; it is the stable semver contract for `src/pyinc`.

## The Guarantee

pyinc guarantees **from-scratch consistency** — the result of incremental
evaluation matches a fresh evaluation on the same declared inputs and resources —
provided the three conditions below hold. Outside those conditions no
guarantee is made.

When a recomputed value is semantically equal to the previously stored value,
the record is **backdated** (also called **early cutoff**): its `changed_at`
revision is not advanced, so downstream dependents remain green and avoid
unnecessary recomputation. For queries without an `eq=`/`cutoff=` policy, that
equality decision is a deep typed comparison of the canonical stored snapshots
and is identical in `strict`, `checked`, and `fast`. It requires both equal
structure and equal scalar representation: Python-equal values such as `1` and
`1.0`, `True` and `1`, and positive and negative floating-point zero are
different. NaN is never equal for cutoff purposes. `eq=` policies continue to
receive mode-exposed values; `cutoff=` tokens use the same typed snapshot
relation as the default policy.

## Conditions for From-Scratch Consistency

**1. Value boundary ownership.**
All values crossing cached boundaries (query arguments, query return values,
`Input` values) must be snapshot-safe: either immutable scalars, containers
that `freeze` can deep-convert to owned snapshot representations, or values
handled by registered `ValueAdapter` instances. `freeze` converts
`list` → `FrozenList`, `dict` → `FrozenDict`, `set` → `FrozenSet`
(kind `"set"`), `frozenset` → `FrozenSet` (kind `"frozenset"`), and
dataclasses → `FrozenRecord`; tuples are a native member of the `Snapshot`
union and are frozen element-wise.

Mapping insertion order is not part of the value-boundary semantics.
`freeze()` orders entries by each frozen typed key's canonical fingerprint;
this is not ordinary Python insertion order and is not promised to match key
comparison order. `FrozenDict` iteration in `strict`, and the insertion order
of dictionaries thawed in `checked` or `fast`, follow that fingerprint order.
Fresh execution, warm reuse, and same-mode checkpoint reload therefore expose
the same order, but code that needs an order-bearing value must use a tuple of
key/value pairs or a `ValueAdapter` that explicitly models order.
(See: `test_mapping_boundaries_use_canonical_iteration_order_warm_fresh_and_checkpoint`)

As a standalone value utility, `freeze()` preserves the identity of an
already-canonical, tree-shaped `Frozen*` value. A `Database` does not retain
that caller-owned shell: inputs, query calls and results, resource parameters,
resource values and probes, and nested `ValueAdapter` payloads are recursively
detached before they enter a record or checkpoint hint. Graph node tables and
`FrozenRef` indexes are copied without changing their alias/cycle topology or
content digest. When a resource parameter cannot be reconstructed with the
same type and frozen digest, dependency verification conservatively re-executes
the parent so the resource receives a fresh live parameter.

Dataclasses thaw to dictionaries because the kernel does not import and
reconstruct arbitrary user classes. A dataclass, frozen wrapper, or composite
containing one cannot therefore be used as a mapping key or set member unless a
`ValueAdapter` reconstructs a hashable value; `freeze` rejects such positions
before they can produce a snapshot that later fails to thaw.
`FrozenDict` keys and `FrozenSet` members must also retain cardinality and
lookup semantics after thaw. Typed-distinct snapshots that Python would merge,
such as `1` with `1.0`, `True` with `1`, or positive with negative zero, are
rejected in hash positions. Adapter-produced collisions are detected during
thaw before a collapsed mapping or set can escape.

`Database.set()` and `set_many()` validate registrations, freeze every pending
value, and run custom comparison policies before committing input registries,
records, counters, revisions, or artifact objects. A rejected value or raising
comparator therefore leaves no phantom key registration and no partial input
transaction.

`freeze()` and `thaw()` are boundary utilities, not a general object
serializer. Passing an adapter registry to `freeze()` does not embed executable
reconstruction logic in the snapshot; the matching registry must also be
available to `thaw()`. Without an adapter, a dataclass's class identity is not
reconstructed.

The kernel stores frozen snapshots internally. `strict` exposes read-only
`Frozen*` views themselves (a query receives, for example, a `FrozenDict` where
the other modes hand it a `dict`); `checked` and `fast` expose owned thawed
values. The strict views reject ordinary mutation but are not capability-level
immutable: reflection such as `object.__setattr__` can bypass a frozen
dataclass wrapper. No external alias or reflectively changed public view can
influence the stored snapshot because boundary shells are detached.

Cyclic and shared object graphs are supported via the `FrozenGraph` /
`FrozenRef` snapshot variants: `freeze` memoizes mutable containers (`list`,
`dict`, `set`, dataclass) by id and emits a `FrozenGraph(nodes, root)` envelope
when shared identity or back-edges are detected. `thaw` reconstructs identity
faithfully via two-pass allocate-then-fill so a list-with-itself round-trips to
an actual self-referential list. Pure trees avoid only the `FrozenGraph`
envelope; they still incur the ordinary deep traversal and snapshot allocation
performed by `freeze`.

Memoization is by **mutable container** identity, so the graph support above
covers exactly those four types. Values crossing through a `ValueAdapter`, a
`tuple`, or a `frozenset` are not memoized and cannot be the target of a
back-edge: `freeze` rejects such a graph with `UnsupportedValueError` rather
than emitting a node, and `FrozenAdapterValue` is not a legal `FrozenGraph`
node. A cyclic adapted object (for example `obj.child.parent is obj`) must
therefore route its cycle through a `list`, `dict`, `set`, or dataclass — or be
decomposed by the adapter into one.

**2. Tracked ambient reads.**
All reads of external state within a query must go through the Resource API
(`FileResource`, `BinaryFileResource`, `FileStatResource`, `EnvResource`,
`DirectoryResource`, `ResolvedPathResource`) or a user-defined `Resource`. The public hooks are `read`,
`probe`, `load`, `probe_and_load`, `identity`, and `label`; built-ins derive
probe/value pairs from one observed state. On a warm request the kernel may
first check for an unchanged world with `probe` alone and calls
`probe_and_load` only when that probe misses or the record cannot answer, so a
resource's `probe` and the probe component of its `probe_and_load` must agree
on an unchanged world; stored probe/value pairs always originate from one
`probe_and_load` observation.

Resource hooks observe external state; they are not query-composition frames.
The `identity`, `label`, `probe`, `load`, and `probe_and_load` hooks must not
call any `Database` observation API or read an `Input`, query, or another
resource, whether through the same database or a different one. Put those
managed reads beside the resource read in a `@query` so the query owns every
dependency edge. A violation raises `ResourceDependencyError` in all modes.
The violation is sticky for the hook invocation: catching it inside the hook
cannot publish a dependency-free value. Direct external I/O remains allowed
inside state-observation hooks.

Query evaluation is synchronous. The runtime rejects the following launch
families before their worker or child process starts and raises
`QueryConcurrencyError` in every mode:

- `threading.Thread.start` and the available low-level `_thread` start aliases
- `ThreadPoolExecutor.submit` and `ProcessPoolExecutor.submit`
- `multiprocessing.Process.start`, `multiprocessing.Pool` construction, and
  Pool submission APIs
- `subprocess.Popen`
- available `os.fork*`, `os.exec*`, `os.posix_spawn*`, `os.spawn*`,
  `os.system`, `os.popen`, and `os.startfile` entry points

The violation is sticky across nested query frames and Resource-hook scopes:
catching the immediate exception cannot publish a result. Resource hooks may
use `subprocess.Popen` and the external-command `os.posix_spawn*`, `os.spawn*`,
`os.system`, `os.popen`, or `os.startfile` families as direct external I/O, but
they may not create threads, executors, multiprocessing workers, or fork/exec
the live database process. The Resource's probe/value contract must account for
the command observation. Top-level concurrency outside query or hook execution
is unaffected.

The database API is deliberately narrower while a query or its equality/cutoff
policy is executing. The active query may compose another query with the same
`Database`, read an `Input` or `Resource` through that same database, and call
`db.report_untracked_read(reason)`. No other public `Database` operation is
legal there: construction, mutation, inspection, statistics/profile/graph
access, mode/configuration access, observer subscription or unsubscription,
request control, and checkpoint save/load raise `QueryContextError` before
validating arguments or changing state. Managed reads through another
`Database` raise the same error because the active database cannot publish a
dependency edge into the other graph. This rule is identical in `strict`,
`checked`, and `fast` modes.

The same boundary applies to integration composition. Layer-3 integration
entrypoints and the request/decode memo helpers are top-level APIs; invoking
one from a query raises `QueryContextError` before argument or path handling,
external reads, or integration memo mutation. Query authors compose stable
Layer-2 `@query` payload APIs from the defining integration module instead.

The kernel intercepts the following during query execution and raises
`UntrackedReadError` if they are called outside a resource scope:

- `builtins.open` and `io.open`
- `os.getenv` and `os.environ` access
- `os.listdir` and `os.scandir`
- `Path.iterdir`

Reads not intercepted by this mechanism (see limitation 1) must be declared
via `db.report_untracked_read()` ([Escape Hatches](#escape-hatches)). The
declaration prevents reuse of that node; it does not convert the read into a
tracked dependency.

**3. Deterministic queries w.r.t. tracked dependencies.**
Given the same tracked inputs, resources, and sub-query results, a query function
must return a semantically equal value. Nondeterminism (timestamps, random
numbers, process state) must be routed through a Resource for this condition to
hold. `report_untracked_read()` can prevent stale memo reuse when such a read
cannot be tracked, but a separately timed fresh evaluation may still observe a
different world. Query bodies and equality/cutoff policies must have
fingerprintable implementations and snapshot-safe statically discovered
captures. Dynamically scoped local classes are rejected; define stable
implementation types at module scope.

Custom `eq=`/`cutoff=` policies must also be **substitutive** for every
dependent computation: when a policy reports two values unchanged, each
dependent must produce a semantically equal result from either value. A
coarser policy is permitted, but the guarantee it buys is correspondingly
coarser — backdating keeps dependents at results computed from the earlier
representative, so from-scratch consistency then holds *modulo the declared
equivalence* rather than on exact values.
(See: `test_non_substitutive_cutoff_keeps_dependents_at_the_earlier_representative`)

Policies receive detached operands in every mode. In strict mode those are
detached `Frozen*` snapshots; Input policies retain their documented thawed
operands. Reflective or ordinary mutation by a buggy comparator therefore
cannot alter a stored record or the candidate that will be published. This is
defense in depth, not permission to mutate: policies must remain deterministic,
pure, and side-effect-free.

### Static capture analysis

Query and policy identity uses static capture analysis. It inspects code,
defaults, annotations, function state, and the globals/nonlocals discoverable
from the function's bytecode, then recursively fingerprints supported captured
queries, functions, modules, methods, and immutable values. A directly named
mutable global or closure is rejected in all three modes.

Python can observe object identity through `id`, `is`, protocol methods, and
extension callables. Every directly captured global/nonlocal — including an
`Input`, `Query`, `Resource`, function, module, method, or type — and every
directly accessible default, reflected annotation, or function attribute
therefore carries a process-local site incarnation in addition to its
structural payload. Replacing a site with a distinct structurally equal object
moves the query identity. The site registry retains the prior object until the
identity comparison, so allocator address reuse cannot hide the replacement.
Because the incarnation has no cross-process meaning, a checkpoint reader in
another process safely executes such a query. A capture-free query whose
behavior-bearing data enters through explicit query arguments can reuse a
checkpoint across processes; queries that capture managed handles remain
correct but conservatively execute there.

This is not a runtime namespace trace or capability sandbox. Dynamic lookups
such as `globals()[name]`, `locals()[name]`, `vars(namespace)[name]`,
`getattr(owner, name)`, `eval`/`exec`, runtime imports, and similar reflection
can read behavior-bearing state that the static walk does not name. Such state
is outside the soundness envelope and can leave both warm and checkpoint-loaded
results stale. Put the state behind an `Input` or `Resource`; when it genuinely
cannot be tracked, call `db.report_untracked_read(reason)` before the read so
the node is re-executed and omitted from checkpoints. The public
`explain_query_captures()` helper reports only this statically discoverable
capture set.
(See: `test_dynamic_globals_lookup_is_outside_static_capture_analysis_and_checkpoint_trust`)

## Mode-Specific Enforcement

| Mechanism | `strict` | `checked` | `fast` |
|---|---|---|---|
| Values exposed as frozen | Yes | No (owned copies) | No (owned copies) |
| Mutation detection at boundary | `TypeError` on write | Fingerprint before/after | None |
| Untracked read interception | Yes | Yes | Yes |
| Resource-hook dependency rejection | Yes | Yes | Yes |
| Query/Resource worker-launch rejection | Yes | Yes | Yes |
| Caught child-failure fallback invalidation | Yes | Yes | Yes |
| Direct mutable captures found by static analysis rejected | Yes | Yes | Yes |
| Semantic equality for cutoffs | Yes | Yes | Yes |
| Backdating on equal recomputation | Yes | Yes | Yes |

## Failing Resource Loads

A resource whose `load` (or `probe_and_load`) raises is an **observation**, not
the absence of one. The kernel stores a *failure record* for that resource node,
carrying the probe observed alongside the failure, and lets the ordinary
probe-comparison machinery drive invalidation:

- The reading query records its dependency edge on the failing resource before
  the exception propagates, so a later `get()` re-checks that node instead of
  treating the reader as dependency-free.
- The exception surfaces **inside the query body**, where the query's own
  `try`/`except` can see it. If the query does not catch it, the exception
  propagates from `get()`; dependent verification re-enters the reader so its
  handler sees the same live failure rather than swallowing it in the kernel.
- An unchanged failing probe does not move the revision, so a query that handled
  the failure stays green across repeated requests. A changed probe — or a
  transition between success and failure in either direction — invalidates the
  readers.
- A failure record never satisfies a read with a value — it holds none. The
  first read in a request re-runs the load, so the exception is a live one; the
  reads that follow it *within that request* re-raise the exception that load
  produced, exactly as a successful load's value is reused for the rest of the
  request. A failing resource costs one load per request, not one per reader.
  The exception is dropped when that request ends — nothing outside it may
  re-raise it — so a node that keeps failing never pins the frames, or the
  allocations, of the load that raised. A `request_span` moves the request
  boundary with it: a failing load's exception is re-raised by reads
  throughout the span and dropped when the outermost span closes, exactly as
  it is for a single `get`.
  (See: `test_failing_resource_loads_once_per_request_across_a_fan_out`,
  `test_repeated_failing_reads_within_one_query_body_load_once`,
  `test_failing_load_exception_is_reused_only_inside_its_own_request`,
  `test_failing_load_frames_are_released_when_the_request_ends`)
- Behaviour is identical in `strict`, `checked`, and `fast`.

Optional external state is therefore from-scratch consistent: a query that
returns a default when a file is missing returns the file's contents once it
appears, and the default again once it is removed, matching a fresh `Database`
at every step.
(See: `test_appearing_resource_invalidates_the_query_that_handled_its_absence`,
`test_disappearing_resource_raises_inside_the_query_body`,
`test_optional_resource_queries_match_fresh_recomputation`)

Two boundaries apply:

- **The probe must be total.** This rests on `probe()` modelling failure instead
  of raising — `FileResource.probe` returns `("missing",)` for an absent file. A
  resource whose `probe` *also* raises is outside the contract, and what the
  kernel does then depends on what it already knows about that node.

  `FileResource` and `BinaryFileResource` apply that missing probe to every
  non-regular target as well. They open nonblockingly and use `fstat` on the
  opened descriptor before reading, so a FIFO, Unix socket, device, directory,
  or a symlink retargeted to one cannot block in a pre-stat/read race.
  Embedded-NUL paths and symbolic-link loops are likewise conservative path
  states rather than raw `ValueError`, `OSError`, or version-dependent
  `RuntimeError` escapes. File resources use their missing state,
  `DirectoryResource` returns an absent empty listing, `FileStatResource`
  returns `exists=False`, and `ResolvedPathResource` returns `None`. These
  probes remain tracked and are rechecked after warm and checkpoint reuse.

  - **No record yet:** the read is the node's first, nothing is recorded, the
    exception propagates unchanged, and a query that catches it is cached as if
    it had no dependency at all — a later `get()` in that same process does not
    re-check it, and neither does anything that depends on it (it is still
    refused a checkpoint, see below).
  - **Record present:** an earlier success, or an earlier recorded failure,
    describes a world the kernel can no longer confirm, so the node is reported
    as *changed*: the queries that read it directly re-execute and the exception
    surfaces inside their query bodies again. The record is also marked
    unconfirmed, which retires its stored probe until a real observation
    rewrites the record: a world that returns to exactly the state that probe
    describes — an undo, a branch switch back — re-loads instead of reusing, and
    the readers that consumed the raise re-execute rather than staying green on
    a value only the failure explains. A permission denial that neither the
    probe nor the load survives is the ordinary way to reach this state; the
    shipped file and listing resources read a path whose kind changed as a
    missing file or an absent listing rather than raising.
  - **Revision accounting:** entering that unconfirmed state **moves the
    revision**, exactly as a recorded failure does. A direct reader that handles
    the exception is otherwise the end of the story: it would return at the
    revision its own dependents had already verified, so nothing above it would
    ever learn that the world moved, and a transitive dependent would keep a
    pre-failure value permanently. The bump is per *transition* — one on the way
    in, one on the way out — plus one for each re-executed query whose
    recomputed value actually changed, never one per observation or request, so
    `revision` settles while a resource stays unprobeable instead of churning on
    every `get()`, and a resource that heals and breaks again bumps again. That
    stays consistent with a fresh `Database` throughout, and it settles as soon
    as a load succeeds again; while the probe keeps raising, the queries that
    read it directly re-run every request, and *their* dependents re-run only
    when the handled value actually differs.

  The probe contract extends to failures for the same reason it covers values: a
  resource whose `load` can raise *different* exceptions for one probe value must
  fold that distinction into the probe. Invalidation compares probes only, never
  exception messages, which are frequently nondeterministic.
  (See: `test_failed_resource_loads_are_recorded_only_when_the_probe_is_total`,
  `test_file_replaced_by_a_directory_matches_a_fresh_database`,
  `test_directory_replaced_by_a_file_matches_a_fresh_database`,
  `test_missing_file_replaced_by_a_directory_matches_a_fresh_database`,
  `test_module_replaced_by_a_package_matches_a_fresh_database`,
  `test_directory_restored_after_a_kind_swap_matches_a_fresh_database`,
  `test_handled_unrecordable_failure_invalidates_a_transitive_reader`,
  `test_handled_unrecordable_failure_propagates_more_than_one_hop`,
  `test_permanently_unrecordable_failure_settles_the_revision`)
- **Failures are not checkpointed.** A failure record holds no value, and a
  reader that handled a failure is only reproducible while the load keeps
  failing. The failure record and every record that transitively depends on it
  are omitted from a checkpoint, so they re-execute against live state after
  `load_checkpoint`. A failure the kernel could not record is excluded the same
  way, and it has to be: the resource record an unprobeable raise contradicted
  still carries the probe and digest from before that raise, which verify against
  a world that healed back into exactly that state, and the reader that consumed
  the raise carries a handled-failure value no record explains. Both are dropped,
  and with them every record above them.
  (See: `test_checkpoints_omit_failed_resource_records_and_their_readers`,
  `test_checkpoints_omit_readers_of_an_unrecordable_failure`)

`inspect()` and `explain()` show the failure node with decision `failed` and a
reason naming the exception; it counts in `DatabaseStatistics.resource_count`
like any other resource node. A load that raised is not counted as a
`resource_load`, and re-running a load on an unchanged failing probe is not
counted as a `resource_probe_hit`. Unless the resource overrides
`probe_and_load` to observe both from one read, the probe stored with a failure
is taken just after it, so a `failed` node can display a probe describing an
already-healed world; the next request re-runs the load and clears it.

A failure the kernel could **not** record writes no record at all, so there is
nothing for `inspect()` to relabel: the node keeps the decision, probe, digest,
and `changed_at` of its last real observation, which is now older than the
database `revision`. Read a resource node that way — as the last observation the
kernel could describe, not as a claim about the world right now. Invalidation
does not consult that stale `changed_at`: the unconfirmed mark reports the node
changed on every refresh and retires its probe until a real observation replaces
it.

## Caught Child Query Failures

A query result and its dependency edges publish together only after the query
returns. When a nested query raises before that point, there is no failed query
record to attach as an edge. The kernel therefore takes the conservative path:
each caller frame the exception unwinds through is marked untracked and
checkpoint-ineligible. If one of those callers catches the exception and
returns a fallback, that successful catcher re-executes on every request rather
than retaining a dependency-free answer after the child heals. Repeated failures
from the same child add one inspection reason, not one per attempt. A later
successful execution replaces the conservative mark with the dependencies it
actually read.

The same rule covers failures while constructing a nested query key, attempting
a checkpoint warm, or executing the child. If a previously successful child
fails while the kernel is verifying a parent's dependency, that verification is
reported as changed and the parent body runs; the exception then appears at the
ordinary child call site, where the parent's `try`/`except` can handle it.
Cold child state created by the failed attempt is rolled back, and a handled
fallback is omitted from checkpoints so a loading database re-derives it from
live state in `strict`, `checked`, and `fast` modes.

A direct recursive call to the exact same query key remains the narrow
exception. Its `CycleError` is a deterministic property of the already-active
frame, so a query may catch that error and cache its fallback. Indirect cycles
cross distinct query keys and use the conservative rule above.
(See: `test_caught_cold_child_failure_heals_without_stale_fallback`,
`test_child_success_failure_and_heal_are_handled_in_parent_body`,
`test_failure_propagates_across_query_frames_and_deduplicates`,
`test_caught_failure_record_is_omitted_from_same_mode_checkpoint`,
`test_query_catching_its_own_cycle_keeps_committed_registries`)

## Request Spans

`db.request_span()` is a context manager that holds one request open across
several top-level `get` / `inspect` / `inspect_fresh` / `read_resource`
calls, so once-per-request work — resource validation above all — happens
once for the whole batch.

Entering a span declares that the world the database reads from does not
change until the span closes; a caller that changes it mid-span must declare
the change with `db.request_inputs_changed()`, which rolls the span onto a
fresh request so the next read of each node re-validates instead of answering
from the span's earlier observation. The declaration is instance-wide: a
change committed by any thread rolls the span. `db.set(...)` and
`db.set_many(...)` declare their own changes — inside a span an input update
that actually changed something rolls the request exactly as
`request_inputs_changed()` does, so later gets re-derive from the new inputs,
while an update the equality decision ignores rolls nothing.

Outside a span `db.request_inputs_changed()` is a no-op — every top-level
call already opens its own request. Spans are reentrant: an inner span, or
one opened inside a `get`, joins the enclosing request, and only the
outermost close ends it.

```python docs-check
from pathlib import Path
from tempfile import TemporaryDirectory

from pyinc import Database, FileResource, Input, query

NOTES = FileResource()
SUFFIX = Input[str]("span.suffix")


@query
def line_count(db: Database, path: str) -> int:
    return len(NOTES.read(db, path).splitlines())


@query
def summary(db: Database, path: str) -> str:
    return f"{line_count(db, path)}{SUFFIX.read(db)}"


with TemporaryDirectory() as directory:
    path = str(Path(directory, "notes.txt"))
    Path(path).write_text("first\nsecond\n", encoding="utf-8")

    db = Database()
    db.set(SUFFIX, " lines")
    assert db.get(summary, path) == "2 lines"

    before = db.statistics()
    with db.request_span():
        assert db.get(line_count, path) == 2       # validates the file once
        assert db.get(summary, path) == "2 lines"  # answers from that pass
        assert db.inspect(summary, path).last_decision == "reused"
        db.set(SUFFIX, " rows")                    # a real change rolls the span
        assert db.get(summary, path) == "2 rows"   # re-validates against it
    after = db.statistics()

    # Four calls, two requests: one per declared world, not one per call.
    assert after.total_requests - before.total_requests == 2
    assert after.resource_probe_hits - before.resource_probe_hits == 2
```

Without the span those same four calls open four requests and re-probe the
file on three of them.

## Explicit Limitations

These fall **outside** the soundness envelope. The kernel does not guarantee
from-scratch consistency when any of these apply.

**1. Unintercepted ambient reads.**
The condition 2 guard covers an enumerated set of entry points, not a category
of behaviour. Everything else that observes external state bypasses it and
silently violates condition 2 unless declared via
`db.report_untracked_read(reason)`: `os.open()` (the low-level syscall),
C-extension I/O, network calls, `ctypes` memory access, and similar. Direct
external-command launch through the enumerated standard-library APIs instead
raises `QueryConcurrencyError`; observe command output in a Resource hook.
(See: `test_os_open_bypasses_untracked_read_guard`,
`test_condition_two_entry_points_stay_guarded`)

The concurrent-launch guard is likewise enumerated. It covers the documented
`threading`, `_thread`, `concurrent.futures`, `multiprocessing`, `subprocess`,
and `os` launch families, including the ordinary cached and prewarmed executor
paths. It is not a capability sandbox: a native extension or private runtime
mechanism that creates a thread or process without traversing a wrapped or
audited CPython entry point can escape the current `ContextVar` and lies outside
the soundness envelope.

Three of those gaps sit close enough to the guarded set to be named individually.

- **File metadata.** The guard sees file *contents* and directory *listings*; it
  does not see `stat`. `os.stat`, `os.lstat`, `os.access`, `Path.stat`,
  `Path.exists`, `Path.is_file`, `Path.is_dir`, `Path.resolve`, and the
  `os.path` helpers built on them (`exists`, `isfile`, `getsize`, `getmtime`)
  each reach the live filesystem from inside a query and return normally. A
  query that asks whether a file exists, or how large or how recently modified
  it is, rather than opening it, is therefore silently untracked: no dependency
  edge is recorded, so the query is reused unchanged after that file appears,
  disappears, or is rewritten, while a fresh `Database` reports the new state.
  Route the observation through `FileStatResource`, whose probe covers
  existence, size, and mtime, through `ResolvedPathResource` when the
  observation is where a path canonicalizes to, or declare it with
  `db.report_untracked_read(reason)`, accepting that the declaration prevents
  memo reuse but does not make the metadata observation tracked.
  (See: `test_file_metadata_reads_bypass_untracked_read_guard`,
  `test_stat_only_query_is_never_invalidated_by_the_file_it_stats`,
  `test_report_untracked_read_prevents_stat_query_memo_reuse`,
  `test_file_stat_resource_tracks_metadata_changes`,
  `test_resolved_path_resource_tracks_symlink_retargeting`)
- **The byte-oriented environment.** `os.getenv` and `os.environ` are
  intercepted; `os.getenvb` and `os.environb` — the same process environment
  under a second name where `os.supports_bytes_environ` holds — are not.
  (See: `test_byte_environment_views_bypass_untracked_read_guard`)
- **The working directory.** `os.getcwd` and `Path.cwd` are not intercepted, so
  a query whose result varies with the process working directory is untracked.
  Pass absolute paths as query arguments instead.
  (See: `test_working_directory_reads_bypass_untracked_read_guard`)

**2. Custom `eq=`/`cutoff=` with side effects.**
If `eq=` or `cutoff=` callbacks perform ambient reads or external mutations,
the equivalence check itself becomes a hidden dependency. Detached operands
protect stored snapshots from comparator mutation, but the kernel cannot make
other side effects deterministic. These callbacks must be deterministic and
side-effect-free; the kernel may make incorrect backdating decisions if they
are not. This limitation does not relax identity validation: statically
discovered policy captures and callable instance state still have to be
snapshot-safe.
(See: `test_custom_eq_with_side_effect_does_not_corrupt_graph`)

**3. Mutation in `fast` mode.**
`fast` mode does not detect mutation of boundary values inside queries. The frozen
snapshot stored by the kernel is safe (it was deep-copied at set time), but a
mutating query may observe corrupted intermediate state. Use `checked` or `strict`
for mutation safety.
(See: `test_fast_mode_uses_owned_values_without_mutation_detection`)

**4. Durable cross-run cache (trusted, under stated conditions).**
The kernel is in-memory, but a durable `ArtifactStore` checkpoint (see
[Checkpoint Save and Load](#checkpoint-save-and-load) for the API) is trusted
for from-scratch consistency across processes and runs when **all** of the
following hold:

- **(i)** every `Input` the checkpoint depends on is set before
  `load_checkpoint`, uses the same explicit non-empty key across runs, and has
  the same equality or cutoff policy; compatible aliases resolve to one logical
  input, and a database rejects aliases with divergent policies;
- **(ii)** resources satisfy the probe contract across runs — a resource's
  probe changes whenever its `load` result changes, and probe values are
  snapshot-safe and process-independent;
- **(iii)** adapters for any adapted snapshot type are registered in the
  loading process with unchanged `freeze`/`thaw` implementations and instance
  configuration;
- **(iv)** the checkpoint key and the store it loads from come from a trusted
  channel. Content addressing proves that bytes match the key they were asked
  for by — it does not authenticate where the key came from. A coherent
  attacker-selected key names a coherent attacker-selected manifest, so keys
  and store contents must be produced by a prior trusted `save_checkpoint`,
  not accepted from an untrusted input (see `SECURITY.md`).
- **(v)** the loading database uses the same `strict`, `checked`, or `fast`
  execution mode recorded by the saving database. Cross-mode loads raise
  `CheckpointModeError` before any checkpoint state is staged.
- **(vi)** query behavior stays within the static capture-analysis envelope;
  dynamic namespace/reflection reads either use an `Input`/`Resource` or call
  `report_untracked_read()` and are therefore absent from the checkpoint.

Under these conditions `load_checkpoint(key)` followed by `db.get(query)`
returns the value a fresh recomputation on the same declared state would, in all
three modes. The mechanisms that earn this:

- **Query identities are recomputed live in the loading process.** A query's
  identity pins the interpreter/build identity (see
  [Interpreter and Build Identity](#interpreter-and-build-identity)) and the
  complete supported static function-definition payload — a canonical typed
  code-object encoding, defaults, keyword defaults, comparator policies, and
  the definitions of transitively and statically captured queries, functions,
  and modules — so a body or policy edit anywhere in that discovered graph, or
  a build-configuration change, produces a different identity and the stale
  record simply misses. This encoding never depends on object reference counts
  and supports nested code and slice constants. Direct capture sites also carry
  a process incarnation: capture-free definitions can reproduce their identity
  across processes, while definitions that expose captured-object identity
  intentionally miss and execute.
- **Inputs and dependency edges verify exactly.** Warmed records carry their
  real dependency edges; each input and sub-query dependency is re-checked
  against the live graph by digest before the record is trusted. Input policy
  digests independently include the interpreter/build identity.
- **Resources are re-probed or re-executed live.** A checkpoint dependency that
  is a resource is re-probed against the real world; a sub-query dependency that
  cannot be warmed is re-executed from its pinned code (the execute-to-verify
  frontier) and its result digest compared to the manifest. Resource identity
  is pinned as described under
  [Additional Kernel Properties](#additional-kernel-properties).
- **Bytes verify against their content addresses.** Every snapshot loaded from
  the store is rejected unless `sha256` of its raw bytes equals the digest it was
  keyed by, and the manifest itself is re-hashed against the checkpoint key
  before anything is parsed out of it.

Anything that cannot be verified is skipped and re-executed rather than trusted:
query subgraphs reached only through a runtime import or dynamic dispatch (their
code is not pinned into any identity), records marked untracked via
`report_untracked_read()`, corrupted or missing store bytes, and adapter
mismatches. A tampered, truncated, wrong-version, or wrong-kernel-fingerprint
manifest is rejected loudly with a typed `CheckpointError` subclass.

Dynamic namespace/reflection reads are not automatically recognized as
unverifiable. Unless declared untracked, a query containing one can be restored
and reused with the same static identity while the dynamically reached object
has changed; condition (vi) excludes that program from checkpoint trust.

Residual limitations that stay outside the envelope: the dynamic
namespace/reflection and module-monkey-patch gaps of limitation 5 apply across
runs exactly as they do in-process; and a checkpoint does not survive an
interpreter or build-configuration change — such records miss safely (they
re-execute) rather than being trusted.

**5. Dynamic namespace/reflection and ambient monkey-patching.**
Captured modules contribute their `__version__`, a SHA-256 digest of their
source or compiled file bytes, declared `__all__`, stable scalar constants, and
the behavior reached through statically resolvable attribute chains. Re-exported
functions and submodules found by the static walk pin their defining modules
transitively; statically found dynamic access to a custom module may be rejected
when its behavior cannot be proven. A third-party version bump or source-file
edit invalidates cached results that statically capture that module. The
complete supported static definition is fingerprinted once per request before
a stored query identity can be selected or reused. Repeated uses in that
request share only the final digest, and no live-definition fingerprint cache
entry crosses the request boundary. A `request_span` is a declared-stable
request; `request_inputs_changed()` rolls the span forward and clears its
request-local digest cache.

The static walk does not discover a value named only by a string passed through
`globals()`, `vars()`, dynamic `getattr`, `eval`/`exec`, import machinery, or a
similar reflection path. An in-process monkey-patch of an existing module or
module-owned class attribute (for example, `sys.modules["foo"].X = 42` or
`foo.Model.flag = True` without reloading or touching the file) is likewise not
detected. Route such mutable state through an `Input` or custom `Resource`, or
declare the read untracked before performing it.

**6. LRU eviction under active dependencies.**
If `max_query_nodes` is set low enough that an intermediate query is evicted while
a dependent is still active, the dependent will re-execute the intermediate from
scratch on its next request. This is correct but may degrade performance.
(See: `test_rewiring_with_lru_eviction`)

## Escape Hatches

- **`db.report_untracked_read(reason)`** — marks the current query as impure;
  forces re-execution on every request and disables backdating for that node.
  Downstream consumers re-verify but can still backdate if their own results are
  unchanged. This guarantees only that the node's prior memo is not reused. It
  does not track time, randomness, network state, or another nondeterministic
  observation, and it does not promise equality with a fresh evaluation made
  at a different time.
  (See: `test_report_untracked_read_forces_reexecution_on_every_request`,
  `test_impure_child_prevents_parent_backdating_unless_result_unchanged`)

- **`ValueAdapter`** — allows custom types to participate in freeze/thaw by
  implementing `freeze` and `thaw`. Adapters extend the condition 1 value
  boundary, so the boundary's obligations extend to them as laws:

  - **Deterministic, side-effect-free hooks.** `freeze` and `thaw` are pure
    functions of their arguments; neither reads ambient state. Adapter work
    at query boundaries runs under the condition 2 guard, so an intercepted
    read raises `UntrackedReadError` there.
  - **Owned results.** `freeze` returns a payload sharing no mutable state
    with the live value; `thaw` returns a value the caller owns outright.
  - **Semantic round-trip.** For any accepted value, `thaw(freeze(x))` is
    semantically equal to `x` wherever the adapted type is consumed.
  - **Pinned adapter state.** Adapter instance configuration is immutable for
    the `Database` lifetime. Construction captures a complete implementation
    and instance-state digest; every adapter boundary verifies it before and
    after use and rejects a mismatch with `UnsupportedValueError`. Adapter
    configuration is therefore a database invariant, not part of ordinary
    query identity. Checkpoint manifests independently record the same digest
    to prevent reuse across databases configured with different adapters.

  (See: `test_adapter_freeze_of_a_query_result_runs_under_the_guard`,
  `test_adapter_thaw_of_query_arguments_runs_under_the_guard`)

- **`eq=` / `cutoff=`** on `Input` and `@query` — allows custom equivalence.
  `Input.eq` compares thawed values directly; query `eq=` compares mode-exposed
  values. Every operand is detached from stored and candidate snapshots before
  policy code runs. `cutoff=` compares snapshot-safe tokens with the kernel's
  deep typed relation. These are mutually exclusive. Cutoff tokens must be
  snapshot-safe, and the declared equivalence must be substitutive for
  dependents (condition 3) for the guarantee to hold on exact values.
  (See: `test_input_cutoff_suppresses_equal_updates`,
  `test_query_cutoff_backdates_and_skips_downstream`)

## Output Reconciliation (Actions)

Queries are pure and never write. The separate **action layer** (`@action`,
`Output`, `ReconcileResult`; see [action-contract.md](action-contract.md))
reconciles a query-derived desired-output set against the filesystem: atomic
writes, content-hash change/tamper detection, ownership-ledger orphan deletion,
and dry-run planning. Reconciliation runs at top level only — never inside a
query — so it does not change query semantics, the value membrane,
untracked-read enforcement, or the `strict`/`checked`/`fast` modes. The
kernel's from-scratch guarantee lifts to the filesystem: an incremental
sequence of reconciles yields the same output files as a single reconcile from
a fresh `Database` into an empty directory.

## Additional Kernel Properties

- Query identity includes the supported static function-definition payload —
  statically discovered values and the definition payloads of transitively
  captured queries, so a body edit to a discovered dependency query moves the
  parent's identity — plus the interpreter/build identity (see
  [Interpreter and Build Identity](#interpreter-and-build-identity)).
  Direct mutable closure/global captures and local/dynamically unbound type
  objects found by this static analysis are rejected. Dynamic namespace and
  reflection paths are outside that analysis, as documented under
  [Static capture analysis](#static-capture-analysis). Non-function callable
  objects that expose `__wrapped__` are also rejected when used as captures,
  equality/cutoff policies, or the
  `identity`, `probe`, `load`, and `probe_and_load` resource hooks: the wrapped
  function is not a substitutive identity for the object's `__call__`
  implementation and state. Ordinary decorated Python functions, decorated
  bound methods, query handles, and fully identified resources remain supported.
  Module-level types are pinned through their defining module identity. Use
  `pyinc.explain_query_captures(fn)` to preview how each statically discoverable
  capture will be classified before the first `db.get()`.
- `Query` is public, and `@query(key=...)` accepts an explicit stable exact
  `str` key (subclasses are rejected); the
  default is `module:qualname`. Coroutine and generator queries are rejected at
  decoration time. A query handle is slotted and immutable. Its observable data
  surface is the read-only `fn`, `eq`, `cutoff`, and `key` properties plus the
  copied `__module__`, `__name__`, `__qualname__`, `__doc__`, and `__wrapped__`
  callable metadata; arbitrary attributes cannot be attached. A statically
  captured query fingerprints that complete surface and the referenced
  function and policy definitions.
- `Resource[KeyT, ValueT, ProbeT]` and `Database.read_resource(...)` are public.
  Resource identity includes the resource's configuration, the implementations
  of every state-observation hook (`probe`, `load`, `probe_and_load`, and
  `identity`), and the interpreter/build identity. The built-in resources
  (condition 2) cover text, binary, environment, stat, directory, and
  path-resolution observation. Resource hooks may observe external state but
  cannot read database-managed state; compose Inputs, queries, and resources in
  the calling query instead.
- `Database.inspect(...)` exposes the last recorded provenance tree as structured
  data. `Database.explain(...)` formats it for humans. Inspection is
  observational and does not force an extra verification pass;
  `Database.inspect_fresh(...)` runs verification first and then returns the
  provenance tree.
- `Database(max_query_nodes=...)` enables bounded memoization. Eviction happens
  at top-level request boundaries and affects query nodes only. Per-node timing
  uses fixed-size count/total/min/max/last aggregates; eviction also removes the
  node's call snapshot, timing profile, and unused query registry entry.
- Query labels consist of the query key, a short argument digest, and the query
  function's name — nothing else; formatting a graph or profile never calls
  argument `repr` or embeds argument values.
- Query records and dependency rewiring publish only after successful execution.
  A failed or cyclic evaluation keeps an earlier record usable and cannot leave a
  dangling dependency edge.
- The distributed package is PEP 561 typed via `py.typed`.

## Interpreter and Build Identity

Query identities, input policy digests, resource identities, and adapter
digests each embed a common interpreter/build identity. Its components include
the Python implementation, the full version tuple (including the prerelease
level), the `-O` optimize flag, the platform, `os.name`, UTF-8 mode, the
API/ABI tag, the multiarch/platform tag, the extension suffix, the build
string, and the pointer width.

## Thread Safety

Within a process, `Database` is thread-safe for concurrent use both across
independent instances and on a single shared instance. Each `Database` holds
a `threading.RLock` that serialises every public state read and mutation,
including queries, resources, inputs, statistics, profiles, dependency graphs,
resets, checkpoint operations, and subscriptions.

The ambient-read and concurrent-launch guards are installed globally exactly
once and dispatch per context via `ContextVar` stacks of active databases. Two
top-level threads using different `Database` instances do not stomp each
other's enforcement, and raw I/O from a thread that is *not* inside any query
continues to work unaffected. Starting a worker from a query through an
enumerated API raises `QueryConcurrencyError`: the worker would escape the
ambient-read context, and joining a child database read while holding the
parent lock could deadlock. If many top-level threads share one `Database`,
work serialises on its lock; separate instances may execute concurrently. On
ordinary GIL-enabled CPython, CPU-bound Python bytecode does not thereby run in
parallel, though I/O and code that releases the GIL may overlap.

## Snapshot Serialization and Store Keys

The kernel derives deterministic content keys from the `Snapshot` union
(scalars, `FrozenList`, `FrozenDict`, `FrozenSet`, `FrozenRecord`,
`FrozenAdapterValue`, `FrozenGraph`, `FrozenRef`, and tuples of the same). The
length-prefixed, type-tagged byte grammar is stable across supported CPython
minor versions. Its digest helper is internal and is intentionally not exported
from `pyinc`; consumers use the `ArtifactStore` and checkpoint APIs rather than
constructing store keys themselves.

The public `serialize_snapshot` and `deserialize_snapshot` functions round-trip
the full snapshot grammar, including `FrozenGraph` / `FrozenRef`. Serialized
snapshots contain data only; adapted values still require the matching adapter
registry when they are thawed. A byte-grammar change is a cache-key break, so
older persisted records are rejected rather than silently reused.

## Checkpoint Save and Load

An outbound `ArtifactStore` (`InMemoryArtifactStore` /
`FileSystemArtifactStore`) accepts persisted query/call snapshots and the
complete snapshot frontier needed by an explicit checkpoint, keyed by internal
content digests, via `Database(store=...)`. `set` and `set_many` keep input
mutation in memory; checkpoint save persists their reachable snapshots before
publishing its manifest. This keeps input transactions independent of stores
that cannot atomically write several objects.
Snapshot bytes use the encoding described in
[Snapshot Serialization and Store Keys](#snapshot-serialization-and-store-keys).

The protocol's `contains(digest)` method has a concrete
`get(digest) is not None` default. It is a presence convenience, not an
integrity check. `save_checkpoint` therefore calls the store's idempotent
`put` for every result snapshot, query call snapshot, and resource parameter,
even when that address is already present. A conforming store accepts identical
bytes and raises `ValueError` for conflicting bytes; such a conflict aborts the
save before a new checkpoint manifest is published. `InMemoryArtifactStore.keys()`
returns a detached read-only snapshot, so callers cannot mutate its backing
object table or observe later writes through an earlier result.

On top of this, `Database.save_checkpoint(store=None) -> str` serialises the
current query and resource records — their snapshot bytes, call snapshots,
resource parameters, dependency edges, execution mode, and per-adapter implementation digests —
into a content-addressed manifest (schema v7), returning a key prefixed with
`"ck"`. Adapter digests include `freeze`/`thaw` code, snapshot-safe instance
configuration, and the interpreter/build identity. `Database` construction
rejects an adapter whose statically discovered captures or state cannot be
pinned; checkpoint loading under a different, pinnable adapter safely misses
and re-executes instead of thawing bytes across a changed implementation
boundary. Records whose cached
value no longer
matches the live graph (a "dirty" save with no intervening `get`) are omitted
rather than persisted stale, so a reload never warms a value a fresh run would
not produce.

`Database.load_checkpoint(key, store=None)` validates every record, dependency,
input policy, probe, and referenced content address before atomically staging
any records; the manifest and byte-level checks that gate this are part of the
trust envelope described under limitation 4. The next `db.get(query)` verifies
dependencies as described there and reuses the stored result without
re-executing when everything checks out, or re-executes the affected query
otherwise. Both methods accept an optional `store=` kwarg for call-site store
injection; the store passed to `load_checkpoint` is also used for subsequent
snapshot loading if the `Database` was not constructed with a `store=`
argument.

## FileSystemArtifactStore

`FileSystemArtifactStore` accepts only digest-shaped keys, serializes each
digest with an OS-native process lock, and publishes flushed same-directory
temporary files atomically.

On POSIX it uses no-follow directory-relative operations, reopens the expected
parent and verifies its filesystem identity immediately before publication, and
rejects a directory rename it observes. POSIX cannot portably exclude a hostile
rename in the final interval between that check and the mutation, so store
roots must not be concurrently renamed by non-cooperating processes.

On Windows it pins every non-reparse directory component with a handle that
denies delete sharing, publishes from the temporary-file handle, and deletes
only through a validated file handle. Lock files retain the same protected
directory handle chain for the lock's lifetime.

Unsafe object, directory, or lock paths surface a typed `ArtifactStoreError`;
this includes an unresolvable root or internal directory and a symbolic-link
loop regardless of the exception spelling used by the running Python version.
Lock timeouts surface `ArtifactStoreLockError`.

## Push Observers

`Database.observe(callback, query, *args, **kwargs)` registers a callback that
fires when the identified query node's stored value changes. It returns a
`Subscription` whose `unsubscribe()` method detaches that exact registration
from future changes; callback equality is never used to choose a registration,
and repeated unsubscribes are no-ops.

Fires exactly when:

- the node already had a stored value before the request, **and**
- it re-executed during a top-level `get` / `inspect` / `inspect_fresh` call,
  **and**
- the new value did not match the previous value under `eq=` / `cutoff=` /
  semantic equality.

Does **not** fire on:

- cold execution, including recreation after LRU eviction or a checkpoint miss;
- `"backdated"` — the recomputation was backdated (see
  [The Guarantee](#the-guarantee));
- `"reused"` — dependencies were unchanged so no recomputation happened;
- an untracked re-execution that produced the same structural value, even
  though its inspection decision remains `"executed"`;
- `db.set(...)` / `db.set_many(...)` — input mutation alone does not
  execute any query. Observers fire on the next `get` that triggers
  dependent re-execution.

Dispatch model:

- Events are buffered on the outermost request scope and delivered **after**
  the kernel lock is released. A callback may therefore re-enter the
  database (e.g. call `db.get(...)`) without risk of deadlock. Inside a
  `request_span` the outermost request scope is the span itself: events
  buffered by gets inside the span are delivered when the outermost span
  closes — on a clean close and when the span body raises — and an inner
  span's close delivers nothing.
- Each event snapshots its callback recipients under the state lock at the
  instant the changed value is published, then carries that immutable recipient
  tuple until lock-free dispatch. A subscription created later in the same
  `request_span` cannot receive an earlier event. Unsubscribing after an event
  occurred does not retract that delivery; consequently, removing oneself or a
  sibling during dispatch does not alter the rest of the already-captured
  event or batch, but it does prevent capture by future events.
- Exceptions raised by a callback are routed to the
  `observer_error_hook` passed to `Database(...)` (default: a one-line
  stderr log) and do not suppress sibling callbacks for the same event.
- Subscriptions survive LRU eviction of their node. Recreating the evicted node
  is cold and emits nothing; a later policy-distinct recomputation fires
  normally.

`QueryChangeEvent` is a frozen dataclass carrying the node's `query_id`,
`args_digest`, decision (`"executed"`), and the `changed_at` / `verified_at`
revisions at the time of execution.

The observer regression suite, including `tests/test_observer_semantics.py`,
exercises these rules in all three modes: equal callback objects, late
subscription, unsubscription before and during dispatch, multi-event batches,
LRU/cold behavior, untracked stable execution, and checkpoint-warm versus fresh
event histories.

## Public Surface

Everything `pyinc` exports, and nothing else, carries the semver contract.
The offline documentation check compares these tables against
`pyinc.__all__` in both directions.

Core:

| Name | What it is |
|---|---|
| `Database` | The incremental query database: `get`, `set`, `set_many`, `inspect`, `inspect_fresh`, `explain`, `observe`, `request_span`, `request_inputs_changed`, checkpoint save/load. |
| `Input` | A declared, keyed input whose values enter through `db.set`. |
| `query` | Decorator declaring a pure incremental query. |
| `Query` | The declared-query object `@query` returns; readable from other queries and from `db.get`. |
| `Resource` | Base class for tracked external values; see the Resource hooks above. |
| `FileResource` | Regular text-file resource: nonblocking descriptor validation, content-hash probe, decoded string value. |
| `BinaryFileResource` | Regular byte-file resource: nonblocking descriptor validation, content-hash probe, raw bytes value. |
| `FileStatResource` | Stat-signature resource for existence/shape checks without content reads; `read()` returns `FileStatSnapshot` in every mode. |
| `FileStatSnapshot` | The frozen typed stat observation `FileStatResource` produces, with `.exists`, `.size`, and `.mtime_ns`. |
| `EnvResource` | Environment-variable resource. |
| `DirectoryResource` | Directory-listing resource. |
| `ResolvedPathResource` | Symlink-aware path canonicalization as a tracked value. |

Values and snapshots:

| Name | What it is |
|---|---|
| `freeze` | Deep-convert a value into its canonical owned snapshot. |
| `thaw` | Rebuild the mutable form of a snapshot. |
| `semantic_equal` | The kernel's semantic-equality decision over two values. |
| `serialize_snapshot` | Encode a canonical snapshot into the stable `K2` byte grammar. |
| `deserialize_snapshot` | Decode and validate `K2` bytes back into a snapshot. |
| `FrozenList` | Read-only list view crossing cached boundaries. |
| `FrozenDict` | Read-only mapping view crossing cached boundaries. |
| `FrozenSet` | Read-only set view crossing cached boundaries. |
| `FrozenRecord` | Read-only dataclass snapshot retaining a qualified-name tag and fields, not reconstructible Python type identity. |
| `FrozenGraph` | Canonical encoding of a cyclic or shared object graph. |
| `FrozenRef` | Back-edge marker inside a `FrozenGraph` node table. |
| `FrozenAdapterValue` | Snapshot produced by a registered `ValueAdapter`. |
| `ValueAdapter` | Adapter making a foreign type snapshot-safe. |

Actions and stores:

| Name | What it is |
|---|---|
| `action` | Decorator declaring a filesystem-reconciling action over declared outputs. |
| `Action` | The declared-action object: `reconcile` and `plan`. |
| `Output` | One declared file output: relative path plus content. |
| `ReconcileResult` | What a reconcile did: created, updated, repaired, actually deleted, unchanged, dry-run `would_delete`, plus `dry_run`. |
| `ArtifactStore` | Interface for durable content-addressed artifact storage. |
| `InMemoryArtifactStore` | Process-local store for tests and ephemeral use. |
| `FileSystemArtifactStore` | Durable on-disk store with advisory locking. |

Inspection and observation:

| Name | What it is |
|---|---|
| `DatabaseStatistics` | Current node counts plus cumulative request/work counters for one database. |
| `InspectionNode` | One node in an `inspect`/`explain` report: revisions, latest verification decision, latest recomputation outcome, reason, impurity, and dependencies. |
| `DependencyGraphNode` | One labeled node in the exported dependency graph. |
| `QueryProfile` | Per-query-node body-execution timing aggregates; reuse has no execution sample. |
| `CaptureInfo` | One statically discoverable captured name in an `explain_query_captures` report. |
| `explain_query_captures` | Preview how a query's statically discoverable captures will be classified before first `get`; dynamic namespace/reflection reads are absent. |
| `Subscription` | Handle returned by `Database.observe`; closes the subscription. |
| `QueryChangeEvent` | Delivered to observers when a subscribed query's result changes. |
| `ObserverCallback` | Callback type receiving `QueryChangeEvent`s. |
| `ObserverErrorHook` | Callback type receiving exceptions raised by observers. |

`InspectionNode.last_decision` is the most recent verification decision for the
node (`executed`, `backdated`, `reused`, or `failed`).
`InspectionNode.last_recompute` is the most recent outcome that actually ran
the node body or resource load; a later reuse changes `last_decision` but leaves
`last_recompute` intact. `changed_at` is the revision at which the stored value
last changed, while `verified_at` is the revision through which its dependencies
were last verified. The remaining fields are `label`, `kind`, `reason`,
`untracked_reasons`, recursive `dependencies`, and derived `is_untracked`.

`DatabaseStatistics` reports current `node_count`, `input_count`,
`query_count`, and `resource_count`; cumulative `query_executions`,
`query_reuses`, `query_backdates`, `resource_loads`, `resource_probe_hits`,
`input_sets`, `input_equal_ignores`, and `evictions`; and the database-lifetime
`total_requests`. `reset_statistics()` clears the cumulative work counters and
query timings, not the graph or lifetime request ordinal.

Each `QueryProfile` contains `query_label`, `execution_count`, and nanosecond
`total_ns`, integer `mean_ns`, `min_ns`, `max_ns`, and `last_ns`. A body that
executes and backdates contributes a timing sample because it ran; a reused
query does not. Profiles are per argument-specific query node and include only
samples since construction or the last `reset_statistics()`.

Errors:

| Name | What it is |
|---|---|
| `PyIncError` | Base error for pyinc; every error below is catchable as this. |
| `MutationError` | A query mutated one of its boundary inputs. |
| `UntrackedReadError` | Code performed an undeclared external read. |
| `ResourceDependencyError` | A Resource hook attempted to read database-managed state. |
| `QueryContextError` | A query attempted a forbidden database operation, cross-database read, or Layer-3 integration call. |
| `QueryConcurrencyError` | Query or Resource execution attempted to launch concurrent work. |
| `UnsupportedValueError` | A value cannot cross a cached boundary safely. |
| `CycleError` | Query evaluation encountered a dependency cycle. |
| `InputKeyError` | An input key is invalid or conflicts within a database. |
| `CheckpointError` | Base error for durable-checkpoint failures. |
| `CheckpointVersionError` | A checkpoint uses an unsupported manifest or kernel version. |
| `CheckpointModeError` | A checkpoint was saved under a different execution mode. |
| `CheckpointManifestError` | A checkpoint manifest is malformed or internally inconsistent. |
| `CheckpointIntegrityError` | Checkpoint bytes do not match their content address. |
| `ActionError` | Base error for output reconciliation failures. |
| `ActionPathError` | An action output path is unsafe or ambiguous. |
| `ActionManifestError` | An action ownership manifest is malformed or untrusted. |
| `ActionLockTimeoutError` | An action cannot acquire its filesystem lock in time. |
| `ArtifactStoreError` | Base error for artifact-store failures. |
| `ArtifactStoreKeyError` | An artifact key is malformed or unsafe. |
| `ArtifactStoreLockError` | An artifact-store lock cannot be acquired. |

## Verification

The from-scratch consistency guarantee is exercised by property-based
differential tests that compare incremental results against fresh-database
recomputation for the same declared state, across all three modes and
with/without LRU eviction. Finite tests are evidence, not a formal proof —
which is one reason the guarantee is stated with explicit conditions and
limitations:

- `test_incremental_results_match_fresh_recomputation` (basic query graph)
- `test_resource_backed_queries_match_fresh_recomputation` (file resources)
- `test_workspace_queries_match_fresh_recomputation` (integration-level)
- `test_multi_level_rewiring_matches_fresh_recomputation` (diamond + multi-level switching)

The rewiring torture suite (`test_diamond_dependency_with_rewiring`,
`test_multi_level_switching`, `test_sharing_pattern_backdates_when_rewired_result_is_equal`,
`test_swapping_pattern_two_queries_exchange_deps`, `test_rewiring_with_lru_eviction`)
verifies that dynamic dependency changes correctly drop stale edges and maintain
correctness across sharing, switching, and swapping patterns.

The mutation adversarial suite (`test_external_alias_mutation_after_boundary_crossing`,
`test_deeply_nested_mutation_detection`, `test_mutation_of_query_return_value_does_not_corrupt_memo`,
`test_two_queries_reading_same_input_get_independent_copies`) verifies that the
value membrane protects cached state from external mutation, deep mutation, and
cross-query aliasing.

`tests/test_query_concurrency.py` exercises the synchronous-query boundary in
all three modes: low-level thread aliases, cached and prewarmed executors,
multiprocessing Process/Pool paths, command launch, fork, sticky caught
violations, same-mode checkpoint reloads, same/cross-database deadlock shapes,
and positive top-level and Resource-command cases.

The durable cross-run guarantee (limitation 4) is checked by the same
fresh-recomputation equivalence, extended to the checkpoint path:

- `test_checkpoint_reload_matches_fresh_recomputation` (property test in
  `tests/test_properties.py`) — reloads a checkpoint across all three modes and
  with/without LRU eviction, comparing `load_checkpoint` + `get()` against a
  fresh, cache-free run over the same edit sequence, and exercises the
  dirty-graph save path directly.
- `tests/test_checkpoint_cross_process.py` — a subprocess matrix that saves in
  one interpreter and reloads in another, checking that identities and digests
  line up across processes.
- `tests/test_checkpoint_trust.py` — the adversarial store and trust suite:
  bit-flipped and truncated snapshot bytes, tampered and wrong-version
  manifests, changed query/adapter/resource implementations, and
  runtime-import-reached dependencies each fall back to safe re-execution or a
  loud `ValueError` rather than serving a stale or tampered value.
- `tests/test_checkpoint_store_integrity.py` — preseeded conflicting bytes are
  rejected for query results and calls plus resource results and parameters;
  equal preseeded bytes remain idempotent, every mode warms like a fresh run,
  and warmed checkpoint resaves cannot endorse corrupt destination state.
