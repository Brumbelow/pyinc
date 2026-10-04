# Kernel Contract

`pyinc` is a correctness-first, in-memory incremental query kernel. This
document defines the guarantee it makes and the conditions under which the
guarantee holds. It is the stable semver contract for `src/pyinc`.

## The Guarantee

pyinc guarantees **from-scratch consistency**: the result of incremental
evaluation matches a fresh evaluation on the same declared inputs and
resources, provided the three conditions below hold. The guarantee applies
only under those conditions.

When a recomputed value is canonically equal to the stored value, the record is
**backdated** (early cutoff). It keeps its `changed_at` revision, so dependents
stay green and skip recomputation.

The default equality decision is **canonical-encoding equality** over stored
snapshots: two values are equal if and only if their canonical snapshot
encodings are byte-identical. `1`, `1.0`, and `True` are three different
values, `0.0` differs from `-0.0`, and a canonical NaN equals a canonical NaN.
The same relation decides default input updates, resource probe comparisons,
checkpoint probe hints, and the token comparison behind a `cutoff=` policy, in
every mode. An `eq=` policy decides under its own relation instead.

## Conditions for From-Scratch Consistency

### 1. Value boundary ownership

Every value crossing a cached boundary (query arguments, query results,
`Input` values) must be snapshot-safe. A snapshot-safe value is an immutable
scalar, a container `freeze` can deep-convert, or a value handled by a
registered `ValueAdapter`. `freeze` converts `list` → `FrozenList`, `dict` →
`FrozenDict`, `set` → `FrozenSet` (kind `"set"`), `frozenset` → `FrozenSet`
(kind `"frozenset"`), and dataclasses → `FrozenRecord`. Tuples are native
members of the `Snapshot` union and are frozen element-wise.

#### Canonical order

A frozen mapping holds its entries in a canonical order derived from each
key's snapshot digest. The order is deterministic across processes and
platforms, and it is neither insertion order nor sorted order. `thaw` and every
mode's boundary exposure preserve that order, so a mapping iterates in the
stored order in all three modes. `FrozenSet` members are ordered by the same
rule. That order belongs to the snapshot and to `strict`'s view of it. A thawed
`set` or `frozenset` is an ordinary unordered Python container. Sequences keep
their own order: a `FrozenList` keeps its element order and a `FrozenRecord`
its field declaration order.

The canonical order is part of this contract. Every digest, store key, and
checkpoint is derived from an encoding that reads entries in stored order.

#### Dataclasses and adapters

A dataclass thaws to a dictionary, because the kernel does not import and
reconstruct arbitrary user classes. Its snapshot is tagged with the class's
`__qualname__` alone, so same-named dataclasses in different modules share a
tag.

The kernel's own resource snapshot types are the exception. Every `Database`
registers `BUILTIN_ADAPTERS`, so a `FileStatSnapshot` is rebuilt as itself at
every boundary in every mode. Registering an adapter for one of those types
replaces the built-in entry. `freeze()` and `thaw()` use only the registry
they are handed. To thaw a database-produced `FileStatSnapshot` outside a
database, pass `adapters=dict(BUILTIN_ADAPTERS)`. Without it, `thaw` raises
`UnsupportedValueError` naming the adapter key.

#### Hash positions

A mapping key or set member must stay a hashable value with a stable hash
after thaw. `freeze` raises `UnsupportedValueError` for three shapes in such a
position:

- A dataclass, a wrapper that thaws to a mutable container, or a composite
  holding one.
- An adapted value whose payload contains a `FrozenGraph`, registered adapter
  or not, since the state it rebuilds stays mutable after insertion.
- A pair of positions that are distinct under the snapshot encoding but
  collapse under Python `==`/`hash` after thaw: `1` beside `1.0`, `True`
  beside `1`, `0.0` beside `-0.0`, or two adapter positions with the same key
  and collapsing payloads. The error names the pair. `thaw`,
  `serialize_snapshot`, and `deserialize_snapshot` refuse that case alike.
  Every canonical NaN is one class for this purpose.

The gate reads payload encodings and never runs the adapter. So an adapter
that thaws two distinct payloads into equal values passes this gate. Such an
adapter breaks the round-trip law under [Escape Hatches](#escape-hatches).

#### Exposure by mode

The kernel stores frozen snapshots. `strict` exposes `Frozen*` views of them.
`checked` and `fast` expose owned thawed values. The views reject ordinary
writes, but `object.__setattr__` still rebinds a field on a view. So the
kernel rebuilds a view for every exposure: rebinding changes that view alone,
and the next request answers from the stored record. A value with a
registered adapter is reconstructed through that adapter in every mode.

No external alias to a value that crossed the boundary can influence the
stored snapshot, in either direction. `freeze` returns a snapshot the kernel
owns outright, and it clones even an already-frozen wrapper. Leaf scalars and
all-leaf tuples are shared with the clone, since nothing reflective can rebind
a leaf.

#### Shared and cyclic graphs

`freeze` memoizes mutable containers (`list`, `dict`, `set`, dataclass) by id.
When it detects shared identity or a back-edge, it emits a
`FrozenGraph(nodes, root)` wrapper. `thaw` rebuilds identity with a two-pass
allocate-then-fill, so a list containing itself round-trips. A pure tree keeps
the bare snapshot shape and still pays the deep freeze at every level.

Memoization covers those four container types only. A value crossing through a
`ValueAdapter`, a `tuple`, or a `frozenset` cannot be the target of a
back-edge, and `freeze` rejects such a graph with `UnsupportedValueError`.
`FrozenAdapterValue` is not a legal `FrozenGraph` node. A cyclic adapted
object must route its cycle through one of the four container types, or be
decomposed by its adapter into one, within the mode-shaped payloads law under
[Escape Hatches](#escape-hatches).

### 2. Tracked ambient reads

Every read of external state inside a query goes through the Resource API: a
built-in resource (`FileResource`, `BinaryFileResource`, `FileStatResource`,
`EnvResource`, `DirectoryResource`, `ResolvedPathResource`) or a user-defined
`Resource`. The public hooks are `read`, `probe`, `load`, `probe_and_load`,
`identity`, and `label`. On a warm request the kernel may check for an
unchanged world with `probe` alone, and call `probe_and_load` only when the
probe misses. So `probe` and the probe component of `probe_and_load` must agree
on an unchanged world. A stored probe/value pair always comes from one
`probe_and_load` observation.

Resource identity includes the resource's configuration, the implementations of
`probe`, `load`, `probe_and_load`, and `identity`, and the interpreter/build
identity. A resource that keeps observation state of its own must define
`identity()` to return the configuration that distinguishes it. A
configuration that changes between two reads leaves the resource undefined. A
change written into a list, dict, or set the resource holds is refused at the
read that observes it. A value rebound anywhere else makes the capturing query
re-fingerprint on every request and execute cold each time.

A resource hook observes the outside world, and only the outside world. The
interception below is lifted while `probe`, `load`, and `probe_and_load` run,
but a hook may not read back into the `Database` it is observing for
([Reentrancy](#reentrancy)). Database-derived values reach a resource through
its **key**: the reading query reads them, which declares its edges, and
passes them in.

While a query runs, the kernel intercepts these calls and raises
`UntrackedReadError` when they happen outside a resource hook:

- `builtins.open` and `io.open`
- `os.getenv` and `os.environ` access, and on POSIX `os.getenvb` and
  `os.environb` access
- `os.listdir` and `os.scandir`
- `Path.iterdir`
- `os.getcwd`, `os.getcwdb`, and `Path.cwd`

Writes to the environment stay allowed: assignment, `del`, `update` and
`clear`. `pop`, `popitem` and `setdefault` return what they read, so they are
refused like other reads.

Resolving a path that is not fully qualified reads the working directory too.
`os.path.realpath` and `os.path.abspath` are wrapped, so they refuse such a
path on every platform and version, however the interpreter reaches the
directory. Windows' `realpath` read the directory through `os.getcwd` for
every path until 3.13.16 and 3.14.8, and in C since. Windows' `abspath` reads
it in C through `nt._getfullpathname`. Before 3.13, POSIX's `realpath` skips
`os.getcwd` for a relative path that reaches an absolute link. What is built
on the two wrapped functions refuses such a path too:

- `Path.resolve`
- `os.path.relpath`, which anchors both its arguments and whose default start
  is the working directory
- `Path.absolute`, which reaches `abspath`, `os.getcwd` or, on 3.11,
  `Path.cwd`

On Windows, a rooted path with no drive (`\data`) and a drive-relative one
(`C:data`) count as anchored. They resolve on the working directory's drive,
or on that drive's own working directory. A fully qualified path resolves
everywhere. Pass absolute paths as query arguments.

Other code that reaches the working directory through these entry points is
refused the same way, wherever it runs:

- the first import inside a query body of a module that reads it at import
  time (`multiprocessing`, and so `concurrent.futures`' process pool)
- `contextlib.chdir`
- `inspect`'s frame helpers, when a frame they look at has a file name that is
  not fully qualified (a program started with `python -m`, or `exec`'d or
  generated code)

The guard sees only reads that go through these names (limitation 1).

Declare every read this mechanism misses (limitation 1) with
`db.report_untracked_read(reason)` ([Escape Hatches](#escape-hatches)). A
module imported for the first time inside a query body runs its module-scope
code inside that query's boundary. A read performed while it initializes is
treated the same as one written in the query body. Import at module scope.

### 3. Deterministic queries

Given the same tracked inputs, resources, and sub-query results, a query
returns a semantically equal value. Nondeterminism (timestamps, random
numbers, process state) is routed through a Resource or declared with
`report_untracked_read()`. Query bodies and equality/cutoff policies must have
fingerprintable implementations and snapshot-safe captures. Immutable
constants and explicit `Input`, resource, and query handles are accepted.
Mutable closure or global data, dynamically scoped local classes, and
reflective namespace reads are rejected before the first execution.

The reflective rule is a conservative static read of the bytecode of the query
function and of every callable folded into its identity. `globals()`,
`locals()`, `vars()`, `eval`, `exec`, and a `__globals__` load are always
refused. `getattr`, `setattr`, `delattr`, and a `__dict__` load are refused
beside a handle that can reach a module namespace. Such a handle is a
`modules` attribute load, the string `"modules"` beside one of those builtins,
an `import_module` attribute load, or a global load of `importlib`. Reaching a
module namespace is allowed by itself: `sys.modules[name]` and
`import_module(name)` with no reflective builtin beside them are accepted, and
neither reaches identity (limitation 5). `pyinc.explain_query_captures(fn)`
previews how each capture is classified before the first `db.get()`. It judges
each capture with the fold the kernel gives it, so it accepts and refuses the
same captures the fingerprint does.

A query may capture a callable the condition 2 guard replaced. A name bound
once a `Database` exists (`from os import getcwd`, `from os.path import
realpath`, `Path.cwd`) holds the guard's wrapper. Wherever the kernel folds a
captured callable, it pins the wrapper as the standard-library callable it
guards, the same way it pins a standard-library type. The pin uses that
callable's module and qualified name, its module's identity, and the
interpreter build, and never the wrapper's own code.

The guard wraps whatever holds a guarded name when it is installed. It pins
only a wrapper around the standard-library callable that its own module and
qualified name lead back to. A capture of a wrapper around a mock, a
`functools.partial`, or a function of the caller's own put there earlier is
refused. A standard-library function that calls a guarded callable through its
module's namespace, such as `os.path.relpath` or `os.path.ismount`, is folded
as the function it is, with the wrapper among its globals.

Calling a captured wrapper inside a query behaves as the call through its
module does. A guarded read is refused, a fully qualified `realpath` or
`abspath` answers, and `Thread.start` binds the thread to the query.
`explain_query_captures` reports such a capture with kind `guarded`.
The environment mappings the guard installs are state, and the pin covers
callables only. A capture of `os.environ` or `os.environb` itself is refused,
as it always was.

Nothing folds pyinc's version, or the kernel as a whole, into every identity.
The kernel marks a change to its own encoding and rules by hand: with the `K2`
prefix every digest carries, and with the kernel fingerprint version a
checkpoint records. pyinc's own code reaches an identity only where a query
captures a pyinc object. There it is folded as any captured module is, by the
bytes of the file that defines it. An annotation evaluated to a pyinc type
(`db: Database` without `from __future__ import annotations`) folds
`pyinc/runtime.py`. A captured resource folds the file its type is defined in
and the files its code reaches. Such an identity moves with any edit to those
files.

The guard's pin folds no file of pyinc's, by design. A captured wrapper moves
with pyinc's code only as much as the same call spelled through its module
(`os.getcwd()`) does. A change to the guard moves it only when the pin's
versioned tag is bumped.

`Input` keys and `@query`/`Query` keys are non-empty plain `str` values. The
default query key is `module:qualname`. A `str` subclass, a `StrEnum` member
included, is rejected at construction, with a message naming the plain string
to pass instead. A key must be a plain `str` because it is stored as node
identity and formatted into labels and the checkpoint manifest.
`Resource.label()` must likewise return a plain `str`. Coroutine and generator
queries are rejected at decoration time.

Custom `eq=`/`cutoff=` policies must be **substitutive** for every dependent:
when a policy reports two values unchanged, each dependent must produce a
semantically equal result from either value. A coarser policy is permitted,
and the guarantee it buys is correspondingly coarser. Backdating keeps
dependents at results computed from the earlier representative. So
from-scratch consistency then holds *modulo the declared equivalence*, and
exact values can differ.

## Mode-Specific Enforcement

| Mechanism | `strict` | `checked` | `fast` |
|---|---|---|---|
| Values exposed as frozen | Yes | No (owned copies) | No (owned copies) |
| Mutation detection at boundary | `TypeError` on write | Fingerprint before/after | None |
| Untracked read interception | Yes | Yes | Yes |
| Mutable closure/global rejection | Yes | Yes | Yes |
| Semantic equality for cutoffs | Yes | Yes | Yes |
| Backdating on equal recomputation | Yes | Yes | Yes |

## Failing Resource Loads

When a resource's `load` (or `probe_and_load`) raises, the failure is itself an
observation. The kernel stores a **failure record** for that node, with the
probe observed alongside the failure. Ordinary probe comparison drives
invalidation, the same way in every mode:

- The reading query records its edge on the failing resource before the
  exception propagates, so a later `get()` re-checks that node.
- The exception surfaces inside the query body, where the query's own
  `try`/`except` can see it. An unhandled one propagates out of `db.get()`
  unchanged, including one raised while a dependent is being verified. Either
  way the node reports `failed` for `last_decision` and `last_recompute`.
- An unchanged failing probe leaves the revision where it is, so a query that
  handled the failure stays green. A changed probe, or a transition between
  success and failure in either direction, invalidates the readers.
- A failure record holds no value. The first read in a request (or in the
  enclosing `request_span`) re-runs the load. Later reads re-raise the same
  exception. The exception is dropped when the request ends, so a node that
  keeps failing pins neither frames nor allocations.

Optional external state is therefore from-scratch consistent. A query that
returns a default when a file is missing returns the file's contents once it
appears, and the default again once it is removed. Two boundaries apply:

- The probe must be total. `probe()` models failure as a return value:
  `FileResource.probe` returns `("missing",)` for an absent file, and a pipe,
  socket, or device answers as an absent path does. A resource whose `probe`
  also raises is outside the contract. When such a probe raises:
  - With no record yet, the exception propagates unchanged. A query that
    catches it is cached as if it had no dependency at all. Nothing in that
    process re-checks it, though it is still refused a checkpoint.
  - With a record present, the node is reported as *changed* and marked
    *unconfirmed*. Its stored probe is retired until a real observation
    rewrites the record. Its direct readers re-execute and see the exception
    again. Their dependents re-run only when the handled value differs.
    Entering that state moves the revision once per transition, and repeated
    requests leave it in place, so `revision` settles while a resource stays
    unprobeable. `inspect()` shows the node as its last real observation.

  A `load` that can raise different exceptions for one probe value must fold
  that distinction into the probe, because invalidation compares probes and
  ignores exception messages.
- Failures stay out of checkpoints. A failure record, a failure the kernel
  could not record, and every record that transitively depends on either are
  omitted from a checkpoint. They re-execute against live state after
  `load_checkpoint`.

`inspect()` and `explain()` show a failure node with decision `failed` and a
reason naming the exception. The node counts in `resource_count`. A load that
raised does not count as a `resource_load`, and re-running a load on an
unchanged failing probe does not count as a `resource_probe_hit`. Unless the
resource overrides `probe_and_load`, the probe stored with a failure is taken
right after the failure. So a `failed` node can display a probe describing an
already-healed world until the next request.

A `ReentrantDatabaseError` from a hook that read back into the database is a
refusal, and it observes nothing. The kernel writes no failure record and no
probe for it. A query that catches the refusal on a resource this
`Database` has never loaded is marked untracked, with a reason naming the
resource. A node that already held a record keeps it and is marked
unconfirmed, the same as for a raising probe.

## Reentrancy

`Database` splits in two at the query boundary. Inside a query body only the
reading surface is open: `get`, `read_input`, `read_resource`,
`report_untracked_read`, and a `request_span` that joins the request the
execution already opened. Every other call raises `ReentrantDatabaseError`
before it does anything:

- the administrative calls `set`, `set_many`, `save_checkpoint`,
  `load_checkpoint`, `reset_statistics`, `request_inputs_changed`, and
  `observe`
- the observational calls `statistics`, `query_profile`, `dependency_graph`,
  `explain`, `inspect`, `inspect_fresh`, and the `revision` property
- `Subscription.unsubscribe`

An administrative call would move state the running execution derives from.
An observational call answers with a function of the database's own history,
and a query result must stay independent of that history.

Inside a resource hook, every call back into the `Database`, the reading
surface included, raises `ReentrantDatabaseError` naming the hook, whether or
not a query is running. A thread started inside a query body or a resource
hook stands where its parent does. Its calls back into the `Database` are
refused at once, before they can wait on the lock
([Thread Safety](#thread-safety)).

## Explicit Limitations

These fall **outside** the soundness guarantee, except where an entry says
otherwise. The durable-cache entry states the conditions under which the
guarantee survives into a later process. The eviction and caught-failure
entries keep it in-process at the cost of incrementality.

### 1. Unintercepted ambient reads

The condition 2 guard covers an enumerated set of entry points. Everything
else that observes external state bypasses it silently unless declared with
`db.report_untracked_read(reason)`. That includes `os.open()`, C-extension
I/O, subprocess output, network calls, `ctypes` memory access, and similar
reads. Four gaps sit close enough to the guarded set to be named:

- *File metadata.* `os.stat`, `os.lstat`, `os.access`, `Path.stat`,
  `Path.exists`, `Path.is_file`, `Path.is_dir`, `Path.resolve` of a fully
  qualified path, and the `os.path` helpers built on them return normally
  inside a query. A query that asks whether a file exists, or how large or how
  recent it is, records no edge. It is reused unchanged after the file
  changes. Route the observation through `FileStatResource` or
  `ResolvedPathResource`. Declaring it removes stale reuse for the declaring
  node alone.
- *The working directory outside the guarded names.* These reads of the
  directory all pass the guard:
  - The import system resolves an empty or relative `sys.path` entry with the
    interpreter's own `getcwd`.
  - A name bound before the first `Database` is created
    (`from os import getcwd`) keeps the unguarded function. It reads the
    directory unrefused and is fingerprinted as that function. A name bound
    afterwards holds the guard's wrapper (condition 3).
  - A system call given a relative path (`os.stat("data")`) reads the
    directory itself.
- *Threads the query did not start.* The guard covers threads a query body
  starts, at any depth, and only those. A pre-warmed pool, an executor built
  at module scope, or a reused `ThreadPoolExecutor` worker is untracked. A
  query that waits on such a worker while the worker waits on the state lock
  deadlocks, and nothing refuses it first. Hand the work to a thread the query
  starts, or declare it.
- *A guarded name put back.* The guard replaces each name once, when the
  first `Database` is created, and wraps whatever holds the name then. A name
  assigned afterwards holds what it was given. A `mock.patch` or
  `monkeypatch.setattr` of a guarded name that is active when the first
  `Database` is created puts the unguarded original back when it ends. That
  name then stays unguarded for the rest of the process. Create the first
  `Database` outside such a patch.

### 2. Custom `eq=`/`cutoff=` with side effects

If a policy callback performs ambient reads or mutations, the equivalence
check itself becomes a hidden dependency the kernel cannot detect. The kernel
may then make incorrect backdating decisions. Policy captures and callable
instance state must still be snapshot-safe.

### 3. Mutation in `fast` mode

`fast` skips the check for mutation of boundary values inside queries. The
stored snapshot is safe, but a mutating query may observe corrupted
intermediate state. Use `checked` or `strict`.

### 4. Durable cross-run cache (trusted, under stated conditions)

A durable `ArtifactStore` checkpoint
([Checkpoint Save and Load](#checkpoint-save-and-load)) is trusted for
from-scratch consistency across processes and runs when **all** of the
following hold:

- (i) Every `Input` the checkpoint depends on is set before
  `load_checkpoint`, uses the same explicit non-empty key across runs, and has
  the same equality or cutoff policy. Compatible aliases resolve to one
  logical input, and a database rejects aliases with divergent policies.
- (ii) Resources satisfy the probe contract across runs. A resource's probe
  changes whenever its `load` result changes, and probe values are
  snapshot-safe and process-independent.
- (iii) Adapters for any adapted snapshot type are registered in the loading
  process, with unchanged `freeze`/`thaw` implementations.
- (iv) The checkpoint key and the store it loads from come from a trusted
  channel. Content addressing proves that bytes match the key they were asked
  for by. It does not authenticate where the key came from, and an
  attacker-selected key names an attacker-selected manifest (see
  [SECURITY.md](../SECURITY.md)).
- (v) The loading database runs in the same mode as the one that saved the
  checkpoint. The manifest records the saving mode, and a load into another
  mode refuses with `CheckpointModeError` before staging any record.

Under these conditions, `load_checkpoint(key)` followed by `db.get(query)`
returns the value a fresh recomputation on the same declared state would
return in that mode.

A checkpoint record warms when every pinned object and every statically
captured module chain the record depends on is unchanged. A sub-query or
resource reached through a statically captured module attribute is pinned the
same way as a directly captured one, and warms on the same terms. A record the
kernel cannot verify is re-executed.

Identities are recomputed live in the loading process. Edges are re-checked by
digest. Resources are re-probed against the real world. A snapshot loaded from
the store is accepted only when the `sha256` of its bytes equals the digest it
was keyed by. Query subgraphs reached only through a runtime import or dynamic
dispatch, records marked untracked, corrupted or missing store bytes, and
adapter mismatches are skipped and re-executed. A tampered, truncated,
wrong-version, wrong-kernel-fingerprint, or wrong-mode manifest is a property
of the manifest as a whole. So the load is refused outright with a typed
`CheckpointError` subclass and stages nothing.

Identities are independent of the hash seed and the install path. A
checkpoint written under one `PYTHONHASHSEED` warms a process running under
another. A checkpoint written from one installation warms a byte-identical
installation unpacked at another prefix, because identity pins where a
definition sits in its package and leaves out the absolute path. A body that
reads `__file__` or takes a path argument still folds that path. Code compiled
without a source file of its own (`<string>`, a sourceless `.pyc`) folds its
filename verbatim. After an interpreter or build-configuration change, a
checkpoint's records miss safely.

### 5. Ambient module or class monkey-patching

Captured modules contribute their `__version__`, a SHA-256 digest of their
source or compiled file bytes, and their declared `__all__`. Modules outside
the standard library also contribute their module-level stable constants,
read live, and the behavior reached through statically resolvable attribute
chains. A standard-library, built-in, or frozen module contributes only the
constants on the attribute paths the capturing query's own code reads off it.
The interpreter/build identity already pins the build. Re-exported functions
and submodules pin their defining modules transitively. Dynamic access to a
custom module is rejected when the behavior cannot be proven. Rebinding a
statically captured module attribute, or an entry in a directly captured class
body, moves query identity at the next request, warm or fresh alike.

Where the interpreter exposes no Python evaluator to observe (a `type` alias
before 3.14, a runtime-constructed `TypeVar`'s bound), the payload anchors
each class it reaches to its live module binding. Rebinding such a binding, a
base, a metaclass, or a directly bound body class then refuses loudly instead
of moving identity.

Three shapes escape this tracking. Route such state through an `Input` or a
`Resource`:

- A chain that lands on a class or a frozen dataclass instance, named directly
  or held inside an immutable container, is compared by the landing's
  identity. What its members hold or read moves a fresh fold and leaves a
  memoized one in place. So a fresh `Database` sees `foo.Model.flag = True`,
  and a warm one misses it. When such a class stops being its module's live
  binding, a warm database serves the stored answer while a fresh computation
  refuses.
- A captured standard-library module folds only the names of the paths read
  off it. The behavior behind them stays out of the fold, so patching a stdlib
  function or class (`json.dumps = other`) goes undetected, warm or fresh. A
  path read through `getattr` with a computed name contributes no path. A
  captured guard wrapper (condition 3) is pinned the same way, by the name it
  guards.
- Outside the standard library, a module that stores a process id, an import
  timestamp, or anything derived from them at module scope makes every
  identity that captures it process-varying.

### 6. LRU eviction under active dependencies

`Database(max_query_nodes=...)` bounds query memo nodes only, and evicts at
top-level request boundaries. Inputs and resources stay resident. If an
intermediate query is evicted while a dependent is still active, the dependent
re-executes it on its next request. The result stays correct, at the cost of
speed. Eviction also removes the node's call snapshot, timing profile, and
unused registry entry.

### 7. Catching an exception raised by a child query

A query's record and edges publish only after it returns. So a failed or
cyclic evaluation keeps an earlier record usable and never leaves a dangling
edge. It also means a query that catches an exception raised by a sub-query
holds a value with no edge to what produced it. The kernel marks the catching
query untracked, with a reason naming the sub-query. It re-executes on every
request, never backdates, and is kept out of checkpoints with everything above
it. A later change that makes the sub-query succeed therefore reaches the
caller, matching a fresh `Database`. The cost is incrementality. Model a
failure the caller means to handle as a returned value, or route it through a
`Resource`.

One shape is exempt. A query refused for asking for *itself* raises
`CycleError` before any work starts, so catching that in the query that asked
for itself leaves an ordinary reusable record. A parent catching a child's
self-cycle, or a cycle that reaches back through another query, is marked like
any other caught failure.

## Escape Hatches

- `db.report_untracked_read(reason)` marks the current query as untracked.
  The query re-executes on every request and never backdates. Its dependents
  re-verify, and can still backdate when their own results are unchanged. The
  kernel applies the same mark itself wherever a value rests on something no
  record describes: a caught sub-query exception (limitation 7) and a caught
  hook refusal on a never-loaded resource
  ([Failing Resource Loads](#failing-resource-loads)).

- `ValueAdapter` lets a custom type participate in freeze/thaw. Adapters
  extend the condition 1 boundary, so its obligations apply to them as laws:
  - *Deterministic, side-effect-free hooks.* `freeze` and `thaw` are pure
    functions of their arguments. Adapter work at query boundaries runs under
    the condition 2 guard, so an intercepted read raises `UntrackedReadError`.
  - *Owned results.* `freeze` returns a payload sharing no mutable state with
    the live value. `thaw` returns a value the caller owns outright.
  - *Semantic round-trip.* `thaw(freeze(x))` is semantically equal to `x`
    wherever the adapted type is consumed.
  - *Mode-shaped payloads.* `thaw` runs at every boundary in every mode. Its
    first argument is the payload as the snapshot holds it. Its second, the
    recursive callable, yields values the way that mode exposes them, so one
    implementation serves all three modes. A payload that freezes to a
    `list`, `dict`, `set`, or dataclass inside a shared or cyclic value, or a
    tuple holding such a container, cannot be handed back whole. So `freeze`
    refuses the value with `UnsupportedValueError`. An adapter with a
    container to carry decomposes it into tuples and scalars.
  - *Pinned adapter state.* Adapter instance configuration is immutable for
    the registered lifetime. It is digested at construction and re-derived on
    every top-level request. `AdapterContractError` names the adapter key
    whose digest moved or could not be re-derived. Implementations and
    configuration participate in checkpoint identity, and stay out of a
    query's definition fingerprint. An adapter whose configuration cannot be
    digested contributes no digest, is skipped by the in-process check, and is
    refused trust by checkpoints.

  The built-in adapters (`BUILTIN_ADAPTERS`: a stateless `FileStatAdapter` for
  `FileStatSnapshot`) hold no instance configuration and ship with the kernel.
  So each one's digest is derived and published once per process, and every
  database reads that one digest back at each boundary.

- `eq=` and `cutoff=` on `Input` and `@query` declare a custom equivalence.
  They are mutually exclusive. `eq=` compares detached operands, so the stored
  snapshot is safe from anything a comparator does to them. A recomputed
  result arrives as thawed values in `checked` and `fast`, and as detached
  `Frozen*` views in `strict`. An input update arrives as thawed values in
  every mode. `cutoff=` derives snapshot-safe tokens from those same operands,
  and the kernel compares the tokens under the canonical relation. A cyclic
  operand is handed over as the cycle it is, so a structural `left == right`
  raises `RecursionError` in every mode. A policy on a query that can return a
  cyclic result must be cycle-aware. The declared equivalence must be
  substitutive (condition 3) for the guarantee to hold on exact values.

## Output Reconciliation (Actions)

Queries are pure and never write. The separate action layer (`@action`,
`Output`, `ReconcileResult`; see [action-contract.md](action-contract.md))
reconciles a query-derived desired-output set against the filesystem. It
provides atomic writes, content-hash change and tamper detection,
ownership-ledger orphan deletion, and dry-run planning. Reconciliation runs at
top level only, outside every query. So query semantics, the value membrane,
untracked-read enforcement, and the modes stay as this document describes
them. The from-scratch guarantee lifts to owned output files under the action
contract's soundness boundary.

## Interpreter and Build Identity

Query identities, input policy digests, resource identities, and adapter
digests embed one interpreter/build identity. It covers the implementation,
the full version tuple, the platform, `os.name`, the byte order, the API/ABI
tag, the multiarch tag, the extension suffix, the build string, the pointer
width, and every `sys.flags` field except `hash_randomization`, folded by
name.

Hash randomization is excluded because two processes that leave
`PYTHONHASHSEED` unset carry the same flag and different hash orders. Folding
it would separate nothing a query's answer can depend on. Route a dependence
on hash order through an `Input` or a `Resource`. A free-threaded build,
another minor version, or another platform derives different identities and
misses safely. On a free-threaded build `sys.flags.gil` is `None`, `0`, or `1`
as `PYTHON_GIL` is unset, `0`, or `1`, so a checkpoint written under one
setting misses under another.

## Thread Safety

Within a process, `Database` is thread-safe both across independent instances
and on one shared instance. Each `Database` holds a `threading.RLock` that
serialises every public read and mutation. Threads sharing one instance
serialise on its lock. Threads holding separate instances each use only their
own lock. On a default build this brings no parallelism: CPU-bound Python work
runs on one thread at a time, and parallel speedup needs separate processes.
On a free-threaded build, threads holding separate instances can run at the
same time, and one instance still serialises on its lock.

The ambient-read guard is installed globally, once, and dispatches per context
through a `ContextVar` stack of active databases. So threads inside queries on
different instances leave each other's enforcement undisturbed, and raw I/O
from a thread outside every query is unaffected.

A thread a **query body** starts inherits the boundary of that query. Its
undeclared ambient reads raise `UntrackedReadError`. Its calls back into the
same `Database` raise `ReentrantDatabaseError` at once, because the query body
holds the lock for its whole execution. A child that waited for the lock while
the body waited for the child would deadlock. The boundary ends when the query
does.

A thread a **resource hook** starts inherits the hook's standing, with or
without a query running. Its raw reads are allowed, and only its calls back
into the `Database` refuse. A hook's boundary is a depth, where a query's is
a frame. So a thread that outlives its hook stays refused, while a thread that
outlives its query returns to normal. Threads that already existed when the
query began are outside every boundary (limitation 1).

A request, and a `request_span` holding one open, belongs to the thread that
opened it. A call from any other thread opens a request of its own. That holds
even when the other thread carries the opener's context, as every thread
started on a free-threaded 3.14 build does, and as `asyncio.to_thread` does
everywhere. A context copied while a request was open joins nothing once that
request has ended. The opener is the thread object itself. Its ident plays no
part, because a later thread can be given that ident once the opener has
exited. So a request left open by an exited thread is joined by no one.

As a result, validation done for one thread's request is never reused by
another thread's request. The answer is the same whether or not the
interpreter copies contexts into new threads. An ended request holds none of
its observer events or failures, so a copied context keeps none of them alive.

Databases on several threads may share one artifact store. Each shipped store
checks and stores a digest in one step: `InMemoryArtifactStore` under a lock,
`FileSystemArtifactStore` under a per-digest file lock. So a digest rebound to
different bytes raises `ValueError` however two puts interleave.

A process forked while another thread holds an `InMemoryArtifactStore`'s lock
gives the child a new lock for that store, so the child can go on using it. A
`Database` that another thread was using at the fork can stay locked in the
child, so create the child's databases after the fork.

## Snapshot Serialization and Store Keys

The kernel derives deterministic content keys from the `Snapshot` union:
scalars, `FrozenList`, `FrozenDict`, `FrozenSet`, `FrozenRecord`,
`FrozenAdapterValue`, `FrozenGraph`, `FrozenRef`, and tuples of the same. It
uses a length-prefixed, type-tagged byte grammar that is stable across
supported CPython minor versions. The digest helper is internal. Consumers use
the `ArtifactStore` and checkpoint APIs and leave store keys to the kernel.

`serialize_snapshot` and `deserialize_snapshot` round-trip the full grammar.
Serialized snapshots contain data only, so adapted values still need the
matching adapter registry when thawed. A byte-grammar change is a cache-key
break: older persisted records are rejected. The grammar requires the
canonical order. A hand-assembled `FrozenDict` or `FrozenSet` in any other
order is rejected with `UnsupportedValueError` by `freeze`, `thaw`,
`serialize_snapshot`, and `deserialize_snapshot` alike. So each value has a
single store key.

## Checkpoint Save and Load

An `ArtifactStore` (`InMemoryArtifactStore`, `FileSystemArtifactStore`) passed
as `Database(store=...)` receives every snapshot the kernel freezes, keyed by
its content digest. An implementation owes three things:

- `get` returns `None` for a digest it does not hold, and never raises for
  one.
- `put` is idempotent for equal bytes under the same digest, and raises
  `ValueError` when a digest would be rebound to different bytes.
- `contains` reports presence, defaulting to `get(...) is not None`.

Every store handed to `Database(store=...)`, `save_checkpoint(store=...)`, or
`load_checkpoint(..., store=...)` is validated against the protocol at that
call. A missing method, or an explicit protocol subclass implementing neither
`get` nor `put`, raises `TypeError` at injection.
`InMemoryArtifactStore.keys()` returns a read-only snapshot of the stored
payloads by digest. Call it again to see later puts.

`Database.save_checkpoint(store=None) -> str` serialises the current query and
resource records into a content-addressed manifest (schema v8) and returns a
key prefixed with `"ck"`. The records carry snapshot bytes, call snapshots,
resource parameters, dependency edges, per-adapter implementation digests, and
the saving mode. Saving rejects an adapter whose captures or state cannot be
pinned. Records whose cached value is stale against the live graph (a dirty
save with no intervening `get`) are omitted. So a reload warms only values a
fresh run would produce.

`Database.load_checkpoint(key, store=None)` re-hashes the manifest against the
key. It validates every record, dependency, input policy, probe, and content
address. Only then does it stage records atomically, under the trust rules of
limitation 4. The store passed to `load_checkpoint` is also used for later
snapshot loads if the `Database` was constructed without one. Manifest schema
v8 rejects older manifests with `CheckpointVersionError`. Stale checkpoints
are re-saved, never migrated.

A store found holding different bytes under a digest the kernel is publishing
is an integrity fault. `save_checkpoint` raises. A value re-executed because
the load skipped those bytes meets the same refusal when it is persisted
again. To recover, remove the corrupt object or supply a clean store.

### FileSystemArtifactStore

`FileSystemArtifactStore` accepts only digest-shaped keys, serialises each
digest with an OS-native process lock, and publishes flushed same-directory
temporary files atomically. On POSIX it uses no-follow directory-relative
operations, and verifies the parent's filesystem identity immediately before
publication. POSIX cannot exclude a hostile rename in the final interval, so a
store root must be safe from concurrent renames by non-cooperating processes.
On Windows it pins every directory component with a handle that denies delete
sharing, and publishes from the temporary-file handle. Unsafe paths surface
`ArtifactStoreError`. Lock timeouts surface `ArtifactStoreLockError`.

## Push Observers

`Database.observe(callback, query, *args, **kwargs)` registers a callback that
fires when the identified query node's stored value moves. It returns a
`Subscription` whose `unsubscribe()` detaches that one registration. Repeated
unsubscribes are no-ops. A change committed after `unsubscribe()` returns never
reaches the callback. Each `observe` call is its own registration. Both calls
are outside-only ([Reentrancy](#reentrancy)).

A callback fires when, and only when, the node's stored value moved during a
top-level `get` / `inspect` / `inspect_fresh` / `explain` call or inside a
`request_span`. That means a cold execution, or a re-execution that advanced
`changed_at`. It stays silent for a backdate, a reuse, a re-execution that
landed a byte-identical value on an untracked node, and `db.set` /
`db.set_many` alone. Input mutation executes nothing, and observers fire on
the next `get`. An untracked node forfeits its `eq=`/`cutoff=` policy for
events, as it does for backdating: a byte-different value its policy would
call equal still fires.

Events are buffered on the outermost request scope. Inside a `request_span`
that scope is the span itself, and events are delivered when the outermost
span closes, cleanly or by raising. Delivery happens after the kernel lock is
released, so a callback may re-enter the database.

The callback list is snapshotted when the change commits. A subscription added
after the change does not receive it. One removed before delivery begins
receives nothing. One removed during dispatch still receives events already
snapshotted. Callback exceptions go to the `observer_error_hook` passed to
`Database(...)` (default: a one-line stderr log), and sibling callbacks still
run. Subscriptions survive LRU eviction of their node. A re-execution after
eviction fires as a cold execution, and the stream does not promise strictly
climbing `changed_at` values. `QueryChangeEvent` carries the node's
`query_id`, `args_digest`, the decision that produced the move (always
`"executed"`), and the `changed_at` / `verified_at` revisions at execution
time.

## Node Record Fields

`Database.inspect(...)` returns the last recorded provenance tree as
structured data, and `Database.explain(...)` formats it. Neither runs a
verification pass, and neither advances a node's decision fields.
`Database.inspect_fresh(...)` verifies first. Inspecting a node that has no
record executes it. Every inspection opens a request or joins the enclosing
span. Query labels consist of the query key, a short argument digest, and the
function name. Formatting a graph or profile never calls argument `repr`. Each
node in a report carries four decision fields:

- `last_decision`: what the most recent request that *touched* this node
  concluded. It is `executed`, `reused`, `backdated`, or `failed`, and
  `pending` before any request recorded one. A second reach within one request
  is recorded as `reused` without re-checking anything.
- `last_recompute`: the outcome of the last time the node's body ran. It is
  `executed`, `backdated`, or `failed`, and `never` before the body has run.
  An input node records the set that gave it its value, and a resource node
  records its load. A reuse leaves it unchanged, so a node may read
  `last_decision` `reused` and `last_recompute` `backdated`. A checkpoint
  restore stamps both fields `reused` on a node this database never ran.
- `reason`: a short phrase for a reader. The definitions here are stated in
  terms of the decisions alone.
- `untracked_reasons`: the reasons recorded during the node's most recent run,
  in order and with duplicates kept. The list is rebuilt from that run alone.
  It holds the reasons passed to `db.report_untracked_read(...)` plus the ones
  the kernel recorded itself, each naming the sub-query or resource it caught.

## Public Surface

The semver contract this document defines covers everything `pyinc` exports,
and only that. The package is PEP 561 typed. `pyinc.integrations` states its
own stable surface in [integration-contract.md](integration-contract.md).
`pyinc_tools` and `pyinc_codegen` are unstable (see
[SECURITY.md](../SECURITY.md)). `scripts/check_docs.py` compares these tables
against `pyinc.__all__` in both directions.

Core:

| Name | What it is |
|---|---|
| `Database` | The incremental query database: `get`, `set`, `set_many`, `inspect`, `inspect_fresh`, `explain`, `observe`, `request_span`, `request_inputs_changed`, checkpoint save/load. Its administrative and observational entry points are outside-only: called from a query body, they raise `ReentrantDatabaseError` (see [Reentrancy](#reentrancy)). |
| `Input` | A declared, keyed input whose values enter through `db.set`. |
| `query` | Decorator declaring a pure incremental query. |
| `Query` | The declared-query object `@query` returns; readable from other queries and from `db.get`. Handle attributes are part of query identity: writing one moves the query's identity, so records stored under the old identity stop answering. |
| `Resource` | Base class for tracked external values; the hooks are listed under condition 2. |
| `FileResource` | Text-file resource: content-hash probe, decoded string value. |
| `BinaryFileResource` | Byte-file resource: content-hash probe, raw bytes value. |
| `FileStatResource` | Stat-signature resource for existence/shape checks without content reads. |
| `FileStatSnapshot` | The frozen stat observation `FileStatResource` produces. |
| `FileStatAdapter` | The stateless built-in `ValueAdapter` rebuilding `FileStatSnapshot` at every cached boundary. |
| `BUILTIN_ADAPTERS` | Read-only map of the adapters every `Database` registers for the kernel's own value types; also the registry to hand `freeze`/`thaw` outside a database. |
| `EnvResource` | Environment-variable resource. |
| `DirectoryResource` | Directory-listing resource. |
| `ResolvedPathResource` | Symlink-aware path canonicalization as a tracked value. |

Values and snapshots:

| Name | What it is |
|---|---|
| `freeze` | Deep-convert a value into its canonical immutable snapshot. |
| `thaw` | Rebuild the mutable form of a snapshot. |
| `semantic_equal` | The kernel's canonical equality decision: two values are equal when their frozen snapshots' canonical encodings match. |
| `serialize_snapshot` | Encode a canonical snapshot into the stable `K2` byte grammar. |
| `deserialize_snapshot` | Decode and validate `K2` bytes back into a snapshot. |
| `FrozenList` | Immutable list view crossing cached boundaries. |
| `FrozenDict` | Immutable mapping view crossing cached boundaries. |
| `FrozenSet` | Immutable set view crossing cached boundaries. |
| `FrozenRecord` | Dataclass snapshot: the class's `__qualname__` (no module component, so same-named classes in different modules share a tag) plus ordered fields. Thaws to a dict; reconstructing the original class requires a `ValueAdapter`. |
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
| `ReconcileResult` | What a reconcile did: created, updated, repaired, deleted, unchanged, plus `dry_run`. |
| `ArtifactStore` | Interface for durable content-addressed artifact storage. |
| `InMemoryArtifactStore` | Process-local store for tests and ephemeral use. |
| `FileSystemArtifactStore` | Durable on-disk store with advisory locking. |

Inspection and observation:

| Name | What it is |
|---|---|
| `DatabaseStatistics` | Node, input, query, and resource counts plus work counters for one database. Fields: `node_count`, `input_count`, `query_count`, `resource_count`, `query_executions`, `query_reuses`, `query_backdates`, `resource_loads`, `resource_probe_hits`, `input_sets`, `input_equal_ignores`, `evictions`, `total_requests`. |
| `InspectionNode` | One node in an `inspect`/`explain` report; its decision fields are defined in [Node Record Fields](#node-record-fields). Fields: `label`, `kind`, `changed_at`, `verified_at`, `last_decision`, `last_recompute`, `reason`, `untracked_reasons`, `dependencies`. |
| `DependencyGraphNode` | One labeled node in the exported dependency graph. Fields: `label`, `kind`, `changed_at`, `verified_at`, `last_decision`, `is_untracked`, `dependency_labels`. |
| `QueryProfile` | Per-query timing aggregate from `query_profile()`: one execution count with the total, mean, minimum, maximum, and last nanosecond figures. Reuse and backdate counts live on `DatabaseStatistics`. Fields: `query_label`, `execution_count`, `total_ns`, `mean_ns`, `min_ns`, `max_ns`, `last_ns`. |
| `CaptureInfo` | One entry in an `explain_query_captures` report: a captured name, a reflective namespace read, or one piece of handle state. |
| `explain_query_captures` | Preview how a query's captures will be classified before first `get`; also reports reflective namespace reads and, given a `Query`, its handle state. |
| `Subscription` | Handle returned by `Database.observe`; closes the subscription. |
| `QueryChangeEvent` | Delivered to observers when a subscribed query's result changes. |
| `ObserverCallback` | Callback type receiving `QueryChangeEvent`s. |
| `ObserverErrorHook` | Callback type receiving exceptions raised by observers. |

Errors:

| Name | What it is |
|---|---|
| `PyIncError` | Base error for pyinc; every error below is catchable as this. |
| `MutationError` | A query mutated one of its boundary inputs. |
| `UntrackedReadError` | Code performed an undeclared external read. |
| `UnsupportedValueError` | A value cannot cross a cached boundary safely. |
| `AdapterContractError` | A registered adapter's instance configuration changed after `Database` construction. |
| `CycleError` | Query evaluation encountered a dependency cycle. |
| `ReentrantDatabaseError` | A call re-entered the database from inside its own execution: from a query body, from a resource hook, or from a thread spawned inside a query execution. |
| `InputKeyError` | An input key is invalid or conflicts within a database. |
| `CheckpointError` | Base error for durable-checkpoint failures. |
| `CheckpointVersionError` | A checkpoint uses an unsupported manifest or kernel version. |
| `CheckpointManifestError` | A checkpoint manifest is malformed or internally inconsistent. |
| `CheckpointIntegrityError` | Checkpoint bytes do not match their content address. |
| `CheckpointModeError` | A checkpoint saved in one database mode was loaded into a database running another; refused before anything is staged. |
| `ActionError` | Base error for output reconciliation failures. |
| `ActionPathError` | An action output path is unsafe or ambiguous. |
| `ActionManifestError` | An action ownership manifest is malformed or untrusted. |
| `ActionLockTimeoutError` | An action cannot acquire its filesystem lock in time. |
| `ArtifactStoreError` | Base error for artifact-store failures. |
| `ArtifactStoreKeyError` | An artifact key is malformed or unsafe. |
| `ArtifactStoreLockError` | An artifact-store lock cannot be acquired. |
| `CompositionError` | A high-level integration entrypoint was called from inside a query body. |

## Verification

Property-based differential tests exercise the guarantee
(`tests/test_properties.py`). They compare incremental results against
fresh-database recomputation over the same edit sequences, across all three
modes, with and without LRU eviction. Dedicated suites cover dependency
rewiring, boundary mutation, and the checkpoint path:

- `tests/test_checkpoint_trust.py`: tampered bytes and manifests, all six
  cross-mode load pairings, and changed implementations.
- `tests/test_checkpoint_cross_process.py`: save in one process and reload in
  another, under differing hash seeds and install prefixes.
- `tests/test_fingerprint_process_stability.py`: every shipped identity is the
  same in three processes under different hash seeds.

Finite tests are evidence and fall short of proof. That is one reason the
guarantee is stated with explicit conditions and limitations.
