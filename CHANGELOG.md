# Changelog

All notable changes to this project will be documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking

- `os.getcwd`, `os.getcwdb` and `Path.cwd` inside a query body raise
  `UntrackedReadError`, and on POSIX so do reads of `os.getenvb` and
  `os.environb`. Both were named gaps in the kernel contract. A query that read
  the working directory or the byte view of the environment recorded no edge, so
  it was reused after either moved. Resolving a path that is not fully qualified
  reads the working directory, so it is refused too, on every platform and
  version. That covers `os.path.realpath` and `os.path.abspath`, which are
  wrapped to make this hold, and `Path.resolve`, `os.path.relpath` and
  `Path.absolute`, which are built on them. Windows' `abspath` reads the
  directory in C, through `nt._getfullpathname`. Without its wrapper, `abspath`,
  `relpath` and, from 3.12, `Path.absolute` of a drive-relative path (`C:data`)
  would answer there. On Windows, a rooted path with no drive (`\data`) and a
  drive-relative path count as not fully qualified. Fully qualified paths
  resolve on every platform as before, and environment writes stay allowed.
  Inside a query body, other code that reaches the working directory through
  these names is refused as well. That includes the first import of a module
  that reads it at import time (`multiprocessing`), `contextlib.chdir`, and
  `inspect`'s frame helpers on a frame whose file name is not fully qualified
  (`python -m`, `exec`'d or generated code).
- `deep_module_resolution_analysis` and `resolve_module_path` skip a relative
  `sys.path` entry, as they already skipped the empty one. On Windows they also
  skip a rooted entry with no drive. They used to resolve such an entry against
  the working directory.
- `InMemoryArtifactStore.keys()` returns a read-only snapshot taken at the call,
  so a mapping held from an earlier call leaves out later puts. It used to
  return a live read-only view, which raised `RuntimeError` when iterated while
  another thread stored.

| Symbol or behaviour | Before | After | What to do |
|---|---|---|---|
| `os.getcwd()`, `Path.cwd()` in a query body; there, `os.path.abspath`, `os.path.relpath`, `os.path.realpath`, `Path.absolute` or `Path.resolve` of a path that is not fully qualified (on Windows `\data` and `C:data` too) | answered | `UntrackedReadError` | pass an absolute path as a query argument, or read it through a `Resource` |
| `os.environb`, `os.getenvb()` in a query body | answered | `UntrackedReadError` | `EnvResource.read()`, then `os.fsencode` |
| First import of `multiprocessing` or a process pool, `contextlib.chdir` in a query body; `inspect.stack()` there when a frame's file name is not fully qualified | answered | `UntrackedReadError` | import at module scope; keep directory changes and frame inspection outside queries |
| Relative (or, on Windows, rooted) `sys.path` entry in deep module resolution | resolved against the working directory | ignored | put a fully qualified path on `sys.path` |
| `InMemoryArtifactStore.keys()` | live read-only view | read-only snapshot at call time | call `keys()` again after puts |

### Fixed

- Saving a module file while a database runs re-fingerprints the queries that
  fold it. The memo guard used to refuse one of them with "Resource ... changed
  its own state between two reads". A resource that keeps the default
  `identity()` folds its class into its configuration digest, and with it the
  bytes of the module that defines the class. One fingerprint read such a file
  more than once, and a request kept a digest across its fingerprints. So a
  save could land between two reads, and the guard blamed the difference on
  the resource. Each query fingerprint now reads each module file once, and
  the guard reads a kept digest again before it judges the resource.
- A thread that carries a `request_span`'s context stays out of the span's
  request. Every thread started on a free-threaded 3.14 build copies its
  starter's context, as `asyncio.to_thread` and `Thread(context=...)` do on any
  build. Such a thread used to answer from the validation done for the span,
  even after the span had closed and the world had moved, so it disagreed with a
  fresh `Database`. It also queued its observer events on a list already
  delivered. A request now belongs to the thread that opened it and ends when
  its scope does. `pyinc.integrations.request_scope` follows the same rule for
  its `once_per_request` memo. The thread is recognised by a token that dies
  with it, because a later thread can be given its ident once it exits. On every
  build, a thread that a query body starts and that outlives the query also
  stays out of the query's ended request.
- `WorkspaceSession.close()` waits for a watcher thread that is still finishing
  before it removes the mirror. A `PollingWorkspaceWatcher` thread used to drop
  its own reference as it wound down. A `stop()` in that window found nothing to
  join and returned early. `close()` then removed the mirror under a live
  thread, in about one close in ten on a free-threaded build. The thread now
  keeps its reference until it exits, and `start()` refuses while the previous
  thread is alive. This also fixes a restart that raced the old thread's exit
  and left the watcher out of the session, so `close()` left it running. A
  stopped watcher can be started again. A callback that runs past `stop()`'s
  five-second join still outlives `close()`, which prints a warning.
- `pyinc.integrations.request_scope` releases its `Database` and every value it
  memoized when it ends. A context copied while the scope was open holds its
  request for as long as the context lives. Every thread started on a
  free-threaded 3.14 build copies one, including the workers of a
  `ThreadPoolExecutor` and of asyncio's default executor. The database and the
  memo used to stay alive as long as such a thread or its pool, even after
  `shutdown()`. The kernel's own request releases its observer events and
  failure keys when it ends, and still delivers the events.
- A query that captures `os.getcwd` or `os.getcwdb` by name once a `Database`
  exists (`from os import getcwd`) is fingerprinted again, as in 4.0. The
  working-directory guard had put a wrapper in their place. The wrapper was a
  closure over pyinc's own state, and fingerprinting refused it with
  `UnsupportedValueError`.
- Threads that call the integrations directly on one `strict` `Database`, with
  no `WorkspaceSession` to serialize them, keep each other's decoded results.
  Two such threads could each find no decode memo for a new database and install
  their own. The second install dropped what the first had stored, so the next
  call decoded again. On a free-threaded build, about one such race in three
  lost an entry. The memo is now read and written under a lock, and decodes run
  outside it. Threads that decode the same payload at once all get back the one
  result stored first.
- Databases constructed at the same moment on several threads use the one digest
  of the kernel's own adapters that the process memoizes. Each database that
  found none memoized used to derive its own and keep it, while the memo kept
  whichever was written last. The derivation is deterministic, so the copies
  agreed. An out-of-contract rewrite of the adapter between two derivations
  would still have left databases in one process disagreeing. Every database now
  uses the first digest published.
- `InMemoryArtifactStore` refuses a digest rebound to different bytes, even when
  two threads put it at once. `put` used to look for the digest and store it in
  two steps. Two puts could both find it absent, and the second overwrote the
  first without the `ValueError` the store protocol requires. On a free-threaded
  build, eight threads putting one digest let a conflicting put through in 186
  rounds of 2000. `put` now looks and stores under the store's lock. `keys()`
  copies under the same lock, so it can be iterated while another thread stores.
  The snapshot it returns is listed under Breaking.
- A process forked while another thread holds the integrations' decode memo
  lock, or an `InMemoryArtifactStore`'s lock, can go on using them. The child
  used to inherit the lock in its held state, with no thread left to release it.
  Its next entrypoint call on a `strict` `Database` (even a new one), or its
  next `put` or `keys` on that store, waited forever. About half of the children
  forked while another thread decoded hung, on 3.11 and on 3.14t. The child now
  gets a new lock for each.
- `InMemoryArtifactStore` can be pickled and deep copied again, as in 4.0. Its
  lock made both raise `TypeError`. A copy, shallow or deep, now holds its own
  items under its own lock, so each store sees only its own puts.
- On Windows, threads whose first store, action or lock-file operation lands at
  the same moment share one Win32 boundary. Each thread that found none used to
  build its own, loading `kernel32` and declaring its function prototypes again,
  and the threads could go on using different ones. The boundary is now built
  once, under a lock.
- The language server's diagnostics publishing is safe against its own shutdown.
  `publish_workspace_diagnostics` is public, and the watcher thread calls it. It
  used to check the session and then read it again, while teardown closed the
  session before letting go of it. A publish in that window raised
  `AttributeError` on `None` or "WorkspaceSession is closed." (the watcher
  printed either to stderr), or sent diagnostics for a session already closed.
  Teardown now detaches the session under the write lock before closing it. A
  publish reads the session once, and sends only while that session is still the
  server's.
- A `PollingWorkspaceWatcher` stop that lands while `start()` runs stops the
  thread `start()` launches. `start()` replaces its stop event under the
  watcher's lifecycle lock. `stop()`, and `WorkspaceSession.close()` when it
  stops each watcher, used to set the event without that lock. A stop landing in
  between set the event being replaced. The new thread ran on, `stop()` waited
  out its five-second join, and `close()` removed the mirror under the running
  thread. `poll()` used to check that the watcher was stopped and only then
  poll. A `start()` landing between the two polled alongside it over the same
  pending paths, and one of them raised `RuntimeError`. Setting the stop event
  and `poll()` now both hold the lifecycle lock.
- A query can capture a recursive function that the kernel pins by its source.
  The kernel pins a function by its source when its definition reads a mutable
  module global, or reaches one that does. Such a function that calls itself, or
  one of a pair that call each other, used to fail at fingerprinting with
  `RecursionError`. It now fingerprints. A function with a global that nothing
  can fold, such as `glob.glob` (whose helper reads a compiled pattern), is
  refused with `UnsupportedValueError`.
- `explain_query_captures` accepts the same captures as the kernel's
  fingerprint, and judges each with the fold the kernel gives it. It used to
  report three kinds of capture as refused. The first was a tuple, a frozenset
  or a frozen dataclass holding any callable. The second was a function the
  kernel pins by its source because it reads a mutable module global. The third
  was an annotation the kernel folds as a capture, in a query that reads its own
  annotations back.
- Every name the ambient-read guard replaces pickles by reference again once a
  `Database` exists. The wrappers for `open`, `io.open`, `os.getenv`,
  `os.getenvb`, `os.listdir`, `os.scandir`, `os.getcwd`, `os.getcwdb`,
  `Path.iterdir`, `Path.cwd` and `Thread.start` were local functions that pickle
  could not find, so handing one to a process pool failed. Each wrapper now
  carries the module and name of the callable it replaces.
- On Windows 3.11 and 3.12, `os.path.realpath` and `os.path.abspath` inside a
  query refuse a path such as `/:/data`. Those versions normalise it to
  `/:\data`, which Windows resolves on the working directory's drive. The
  full-qualification check used to take it for a drive with a root.
- The language server sends diagnostics in the order its analyses ran. A publish
  on the watcher thread used to analyze outside the server's locks. It could
  then send after the request loop had published a newer edit, and bring stale
  diagnostics back. A publish now holds one lock from before its analysis until
  its last send. A publish also used to record each document as sent before
  sending it. When an edit left a stale range past the end of its line, the send
  raised, and the same diagnostics were later skipped as already sent. The
  server now records a document once its notification is written. A document
  whose range falls outside the edited text is skipped, and the publish that
  follows the edit sends it.

- A guarded name that a patch puts back is guarded again. A `mock.patch` or
  `monkeypatch.setattr` of a guarded name that was active when the first
  `Database` was created put the unguarded standard-library callable back when
  it ended. That name then answered inside every query for the rest of the
  process. Each query execution now wraps such a name again as it starts, so a
  read through it raises `UntrackedReadError`, as it would have without the
  patch. The new wrapper pickles by reference and fingerprints like the first.
  A fake or a mock set in a guarded name's place keeps it.

### Changed

- `os.environ.clear()` and `os.environb.clear()` are allowed inside a query,
  like the other environment writes. The inherited `clear()` reads every key
  first, so the guard used to refuse it. `pop`, `popitem` and `setdefault`
  return what they read and stay refused.
- Once a `Database` exists, a query may capture by name any callable the
  ambient-read guard replaces. These are `open`, `io.open`, `os.getenv`,
  `os.getenvb`, `os.listdir`, `os.scandir`, `os.getcwd`, `os.getcwdb`,
  `os.path.realpath`, `os.path.abspath`, `Path.iterdir`, `Path.cwd` and
  `Thread.start`. The capture may be direct, a default, or through a closure, a
  container, a helper function, a module attribute, a query handle, or, for
  every name but `Path.cwd`, a static method in a class body. Such a capture
  used to be refused with `UnsupportedValueError`, apart from `os.getcwd` and
  `os.getcwdb`, which 4.0 fingerprinted as builtins (see Fixed). It is now
  fingerprinted as the standard-library callable it guards. The fingerprint
  covers that callable's module and qualified name, its module's identity, and
  the interpreter build. A standard-library function that calls one through its
  module's namespace is folded with it. So `os.path.relpath` and
  `os.path.ismount` captured by name fingerprint too, `ismount` on POSIX for the
  first time. Calling a captured wrapper inside a query behaves as the call
  through its module does: a guarded read is refused, a fully qualified
  `realpath` or `abspath` answers, and `Thread.start` binds the thread to the
  query. `explain_query_captures` reports such a capture with kind `guarded`. A
  name bound before the first `Database` still holds the unguarded original and
  fingerprints as before. A capture of `os.environ` or `os.environb` itself is
  still refused. So is a capture of the wrapper around a mock, a
  `functools.partial` or a function of your own that held a guarded name when
  the first `Database` was created. No standard-library name describes what such
  a wrapper calls.
- CI runs the test suite on the free-threaded CPython 3.14t build with the GIL
  disabled, on Linux, macOS, and Windows, and runs the property suite there
  nightly. The release workflow gates on it with the rest of the matrix. The FAQ
  drops its statement that free-threaded builds are unverified.
- The package declares `Programming Language :: Python :: Free Threading ::
  2 - Beta`. The post-release check also installs the published wheel on
  3.14t with the GIL disabled, which makes fifteen operating-system and
  Python combinations.
- CONTRIBUTING drops the rule that branch coverage must stay at or above 90%.
  The floor was dropped earlier, and CI reports coverage without gating on it.

## [4.0.0] - 2026-09-04

v3.1.1 was tagged but never published. Its release run was cancelled after an
internal review found the consistency issues fixed below. Do not use the tag.

### Highlights

- A checkpoint written by one process, container or installation warms another.
- A checkpoint warms a query that reaches its child as a module attribute.
- Reentrant database calls raise an error where they used to deadlock or go
  stale.
- Every file-reading entry point is bounded, and pipes and devices answer as
  absent.
- Every integration text read compares the text it returns, warm as fresh.
- `strict` mode thaws adapters at every boundary and rebuilds every view.

### Breaking

- `Input`, `Query` and `NodeKey` keys and `Resource.label()` must be plain `str`
  values. A `str` subclass is refused.
- `FileStatResource` delivers `FileStatSnapshot` in every mode. It used to
  deliver a view or a `dict`.
- `Query.compare` is removed. The kernel compares snapshots under the query's
  policy.
- `Subscription.unsubscribe()`, every administrative or observational `Database`
  entry point, and every `Database` read from a resource hook or a thread
  spawned inside a query raise `ReentrantDatabaseError`.
- Every high-level integration entrypoint runs only outside a query. From a
  query body it raises `CompositionError`.
- A resource that mutates a list, dict or set it holds between reads is refused
  with `UnsupportedValueError`.
- An adapter payload holding a container inside a shared or cyclic value is
  refused at the freeze.
- `semantic_equal(1, 1.0)` and `semantic_equal(True, 1)` are `False`.
  `semantic_equal(nan, nan)` is `True`.
- The checkpoint manifest is v8 and the action ledger v3. A v7 or earlier
  manifest raises `CheckpointVersionError`, and a load into another mode raises
  `CheckpointModeError`.
- Reflective namespace reads and directly captured `functools.cache` callables
  are refused as captures. Every query identity moved, so a checkpoint written
  by an earlier build misses and re-executes.
- The semantic-versioning promise covers `pyinc` and `pyinc.integrations`.
  `pyinc_tools` and `pyinc_codegen` are unstable.

| Symbol or behaviour | Before | After | What to do |
|---|---|---|---|
| `Input(key)`, `@query(key=...)`, `Resource.label()` | any `str` subclass | `str` itself | pass `member.value` or a plain string |
| `FileStatResource` value | record view or `dict` | `FileStatSnapshot` | `stat["exists"]` becomes `stat.exists` |
| `Query.compare(a, b)` | public helper | removed | `semantic_equal(a, b)` for the default relation |
| `db.set`, `observe`, `unsubscribe`, `inspect`, ... in a query body | answered | `ReentrantDatabaseError` | move the call outside the body |
| Integration entrypoint in a query body | ran or was refused | `CompositionError` | `db.get()` the payload query it decodes |
| `semantic_equal(1, 1.0)` | `True` | `False` | declare `eq=` where numeric coercion is wanted |
| Resource mutating a held container | ran, never reused | `UnsupportedValueError` | keep the state outside, or define `identity()` |
| Adapter payload holding a container inside shared structure | mode-dependent value | `UnsupportedValueError` | return tuples and scalars |
| Checkpoint saved by 3.1 | loaded | `CheckpointVersionError` | re-save from a warm 4.0 run |

### Fixed

- A checkpoint warms a query that reaches a child query or a resource as a
  module attribute (`import pkg.queries as q`, then `q.thing(db, x)`). The warm
  gate used to count only direct captures as pinned, so the parent always
  re-executed. The pinned walk stops where the fingerprint fold stops, so a
  query reached only around a module-capture cycle counts as unpinned. A
  dependency record is restored only when its live identity equals the saved
  one.
- Identities leave out a captured module's whole namespace and a code object's
  absolute source path. A checkpoint written under one hash seed or at one
  install prefix now warms a process running under another. Captured-module
  identity hashes the file's bytes on every observation, on every platform.
- A failed `db.set` or `db.read_input` leaves no half-registered input behind,
  and the input registry holds one entry per distinct key.
- The resources, the actions and the stores all refuse a self-referential
  symbolic link and an embedded null character as unsafe paths. The error is
  both a `PyIncError` and an `OSError`.
- The action layer validates a malformed ledger unconditionally, persists a
  voided ledger, pins orphan deletion to the file it verified, and reports it.
- Observers fire only when a stored value moves, detach per registration, and
  receive only events committed after they subscribed. Persisting a snapshot
  verifies the stored bytes. `ArtifactStore.contains` has its documented
  default, and `InMemoryArtifactStore.keys()` is read-only.
- Collapsing `FrozenDict` keys and `FrozenSet` members are refused, and policies
  receive detached operands. `explain_query_captures` agrees with the kernel.
- `csv_analysis()` answers an unusable sniffed dialect, and the requirements
  walk declares the existence of every file it reaches.

### Changed

- `ResolvedPathResource`, `FileStatAdapter`, `BUILTIN_ADAPTERS`,
  `AdapterContractError`, `CheckpointModeError` and `CompositionError` are
  added.
- `Database`, `save_checkpoint` and `load_checkpoint` validate a `store=`
  against the `ArtifactStore` protocol and raise `TypeError` at the call.
- Query identity is wider. Wraps-decorated captures, a `Query` handle's own
  state and a rebound captured module attribute move it. A mutated adapter
  configuration raises `AdapterContractError`.
- The ambient-read guard follows threads started inside a query. A query that
  catches a sub-query's exception is marked untracked, where it used to be
  cached. `freeze` always returns an owned snapshot. TOML nesting is accepted to
  200 levels, and the development-status classifier is `4 - Beta`.
- The documentation is restructured. The README keeps the entry path. The kernel
  contract is cut to its rules and states its guarantee as conditional. The
  architecture page folds into the index and the FAQ, and the 2.x migration
  guide is removed.

## [3.1.1] - 2026-08-03

### Highlights

- Marker comparisons evaluate the way `packaging` does, clause for clause. They
  make a version comparison only where packaging makes one, and use its fallback
  table everywhere else. The old Python string ordering for non-version
  variables is gone.
- Lock acquisition retries a transient lock-file open within the caller's
  deadline. A concurrent holder on Windows used to fail the acquire outright.
- A cycle of aliases spanning definitions is an `alias-cycle` error that blocks
  generation. It used to generate modules whose aliases resolve to no type.
- The integrations decode memo is keyed per `Database`, so a dropped database
  releases the payloads it pinned.
- Contributor, security, and release-verification documentation, a comparison
  FAQ, and issue templates.

### Changed

- An `enum` beside a declared `type` the generator cannot use reports
  `unsupported-enum-type` once. It used to add one `enum-type-mismatch` per
  member, for a type no member could ever match. Members are still checked
  against a usable declared type.
- The test suite runs in parallel on CI (`pytest -n auto --dist load`, for the
  suite and the coverage run), and `pytest-xdist>=3.6` is added to the `dev`
  extra. A serial `pytest` is unchanged and stays the local release gate.
- The `dev` extra requires `packaging>=26.2`, the release the specifier and
  marker parity tests compare against.
- Project metadata declares `Operating System :: OS Independent`,
  `Typing :: Typed`, and a `Changelog` URL.

### Fixed

- Marker comparisons match `packaging`'s evaluation. A version comparison is
  made only for `python_version`, `python_full_version`,
  `implementation_version`, and `platform_release`. The specifier is always
  built from the operator and the right-hand side, and the left-hand side is the
  version tested against it. A literal on the left used to be compensated for by
  inverting the operator. When the clause is an invalid specifier, packaging's
  fallback table decides. In that table `<` and `>` are false, `<=`, `>=`, and
  `==` are string equality, and `!=` is string inequality. `~=` has no entry, so
  it evaluates false under the new `undefined-marker-comparison` diagnostic.
  Every other variable therefore falls to that table, where it used to use
  Python string ordering. So `platform_machine > "arm"` is false, where it was
  an ordering test, and `platform_release == "6.5.0-28-generic"` is string
  equality, where it was a failed version parse. An environment value that fails
  to parse under an otherwise valid specifier evaluates false and reports
  `unparseable-version`.
- A wildcard specifier whose base carries a pre-release, post-release, dev, or
  local segment (`==1.0rc1.*`, `==1.0.post1.*`, `==1.0.dev1.*`, `==1.0+abc.*`)
  is rejected as invalid, matching packaging's specifier grammar. So is a
  wildcard under an ordered operator (`>=1.0.*`). Such clauses used to parse and
  prefix-match, so a requirement carrying one counted as satisfied. It is now
  reported as unevaluatable.
- Lock acquisition retries a lock-file open that fails transiently, until the
  same deadline the contention loop uses. A transient failure is a sharing
  violation, or access-denied while the lock path is still a regular file or
  missing. These are the shapes a concurrent holder or a file scanner produces
  on Windows. The open used to happen once, before the deadline was computed, so
  that contention raised out of the acquire. Every other open failure still
  raises unchanged, including access-denied at a path that is a directory or
  other special file. An exhausted deadline raises the same typed lock error
  that contention already raised: `ArtifactStoreLockError` from the store and
  `ActionLockTimeoutError` from an action.
- A cycle of pure aliases spanning definitions (`{"A": {"$ref": "#/$defs/B"},
  "B": {"$ref": "#/$defs/A"}}`) reports the blocking `alias-cycle` error on
  every member, naming the cycle in definition order (`A -> B -> A`). It used to
  generate one module per member, each aliasing the next, which closed an import
  loop that resolved to no type. A container or object field anywhere in the
  loop still breaks it and keeps compiling. The single-definition case remains
  `self-referential-alias`.
- The integrations decode memo is keyed per `Database` through a weak reference,
  so a dropped database releases every payload and decoded value it pinned. They
  used to stay in one process-wide cache until the entry bound cleared the whole
  thing. The bound now applies per database.

### Documentation

- `CONTRIBUTING.md` covers development setup, what CI checks, the architectural
  boundaries, adding an integration, benchmarks, and the commit and release
  rules. These moved out of `AGENTS.md`, which used to be the only place they
  were written down.
- `SECURITY.md` states the supported versions, how to report a vulnerability or
  a soundness violation, what is in scope, and how release integrity is
  established.
- `docs/releases.md` documents what the release workflow verifies, what happens
  after publication, and how to verify a downloaded artifact yourself.
- `docs/faq.md` compares pyinc with Salsa and Adapton and with
  `functools.lru_cache`, states the overhead, covers the GIL and free-threaded
  builds, and says when not to use it. The README gains a section on why pyinc
  exists that points at it.
- The README and demo page state the demo's numbers in prose: 109.08 s to
  analyze all 270 files of a pinned pytest checkout from cold, then 632 ms to
  catch up after one edit.
- Issue templates for bug reports, feature requests, and soundness reports.
- The integration contract states the notebook surrogate-scanning boundary. Cell
  sources, cell types, and kernel metadata are scanned because they reach the
  cached payload, and outputs and per-execution metadata never reach it. So a
  notebook whose outputs hold a lone surrogate stays fully analyzable, and one
  whose sources hold one is a decode error with no partial analysis.
- The architecture, kernel-contract, and migration documents say checkpoint
  manifest v5, where three of them still said v4. `scripts/check_docs.py` now
  pins every manifest-version statement in the contracts to the value the
  runtime writes.

## [3.1.0] - 2026-08-03

### Highlights

- `Database.request_span()` holds one kernel request open across several reads,
  with `Database.request_inputs_changed()` to declare mid-span input changes.
  `WorkspaceSession` holds a span per public method, so a warm
  `analyze_workspace` validates each resource once per call.
- A resource whose `load` raises gets a failure record. Readers keep their
  dependency edges, and `inspect()` and `explain()` show the node as `failed`.
  An unchanged failing probe stays green, and a failing load costs one load per
  request, where it cost one per reader.
- The warm path got cheaper. Default backdating compares stored snapshots with
  no thaw or re-freeze, and unchanged file reads answer from the probe alone,
  with no decoding of contents.
- `pyinc-tools analyze` can gate a CI job: `--format text`,
  `--diagnostics-only`, and `--fail-on` with exit status `3`.
- For PEP 440 conformance, `===` evaluates, `~=` uses a true prefix-match upper
  bound, wildcard matching compares epochs, and `pip-compile --generate-hashes`
  output parses correctly.
- Code generation compiles `const`, inline `enum`, schema-valued
  `additionalProperties`, and the single-branch combinator spellings.
  Annotation-only keywords warn, where they used to fail the document.
- Notebook cells with IPython syntax are neutralized width-for-width and
  analyzed. They used to be reported as syntax errors.
- Structured-configuration nesting is capped (XML at 256 element levels, JSON at
  200, TOML at 100). Past a cap the integration reports a diagnostic, where it
  used to exhaust the stack.
- Checkpoints from 3.0.0 are refused with an error (manifest v5). They used to
  warm records the current kernel would not produce.
- The workspace demo is back, with recordings of `pyinc-tools` watching a pinned
  checkout of pytest.

### Added

- `Database.request_span()` is a public context manager that holds one kernel
  request open across several `get`/`inspect`/`inspect_fresh`/`read_resource`
  calls. `Database.request_inputs_changed()` declares mid-span input changes.
  Entering a span declares that the world the database reads from is stable
  until the span closes. `set`/`set_many` declare their own changes, and a
  change committed by any thread rolls the span. Buffered observer events are
  delivered when the outermost span closes, even if the span body raises. A
  failing load's exception lives to span end.
  `pyinc.integrations.request_inputs_changed()` rolls a held span, and
  `WorkspaceSession` holds a span for each public method.

- `pyinc-tools analyze` can report diagnostics and gate a CI job.
  `--format text` prints one `path:line:col: severity code message` line per
  diagnostic. `--diagnostics-only` emits only the diagnostics array in place of
  the full result. `--fail-on` exits with status `3` when a diagnostic reaches
  the given severity. Diagnostics are sorted by location so output is stable,
  and the report is always printed before the exit status is decided.
- PEP 440 arbitrary equality (`===`) is evaluated in version specifiers,
  requirement evaluation, and dependency checking. It compares the version as
  written, with no normalization, padding, or case folding. So it is decided
  without parsing, and works against versions that do not conform to PEP 440.
- Semantic tokens classify a `from ... import ...` use by the workspace
  declaration it resolves to. Such a use used to be left unstyled. Imports that
  resolve outside the workspace, or ambiguously, remain unclassified.
- A resource whose `load` or `probe_and_load` raises now gets a *failure
  record*, carrying the probe observed alongside the failure. The reading query
  records its dependency edge on that node before the exception propagates. A
  later `get()` therefore re-checks the resource, where it used to treat the
  reader as dependency-free. `inspect()` and `explain()` show the node with
  decision `failed` and a reason naming the exception, and it counts in
  `DatabaseStatistics.resource_count`. An unchanged failing probe leaves the
  revision alone, so a query that handled the failure stays green across
  requests. A changed probe, or a transition between success and failure in
  either direction, invalidates its readers. Behaviour is identical in `strict`,
  `checked`, and `fast`.
- `ClassModel.truncated_bases` names every base that resolved to a workspace
  class but sat past the `MAX_BASE_DEPTH` cap, so the walk stopped before it.
  Each is reported as written at the stopped edge, deduplicated in
  first-encounter order. Members inherited eight or more levels above a class
  are still omitted, and the omission is now reported.
- Code generation compiles four constructs that used to be
  `unsupported-construct` errors. `const` and inline `enum` render as
  `typing.Literal[...]`. Schema-valued `additionalProperties` in property
  position renders as `dict[str, T]`, recursively, and a referenced definition
  joins the model's reference graph. The two combinator spellings that name a
  single type render as `S` and as `S` made optional: single-branch
  `{"allOf": [S]}`, and `{"anyOf": [S, {"type": "null"}]}` in either branch
  order. `Literal` is imported only when a rendered type uses it. Multi-branch
  composition remains an error.
- Code generation accepts annotation- and validation-only keywords wherever a
  schema node is accepted, with a non-blocking `ignored-constraint` warning
  naming the keyword. The keywords are `format`, `pattern`, `minimum`,
  `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minLength`,
  `maxLength`, `minItems`, `maxItems`, `uniqueItems`, `deprecated`, `readOnly`,
  `writeOnly`, boolean `additionalProperties`, `examples`, and `default`. A
  single `"format": "email"` used to make an entire document fatal. A JSON
  `default` still does not become a dataclass default.
- New code-generation diagnostic codes: `ignored-constraint` (warning),
  `invalid-constraint` (error), `unconstrained-object-model` (warning),
  `unsupported-const-value` (error), `const-type-mismatch` (error),
  `unsupported-tuple-items` (error), and `self-referential-alias` (error).
- Notebook code cells that fail to parse as Python are re-parsed after
  neutralizing IPython syntax. Line magics, shell escapes, help forms, and
  capture assignments are replaced by equal-width placeholders. The rest of the
  cell is still analyzed, and every range still names its real notebook line and
  column. A first-line cell magic claims the whole cell, and its body is dropped
  unless the magic runs that body as Python. Recognition is lexical and only at
  the start of a logical line, so magic-shaped lines inside string literals or
  bracketed continuations are left alone.
- New notebook diagnostic code `notebook-non-python-cell`, for a cell that still
  fails to parse after neutralization. It carries a source range like
  `syntax-error` does.
- Nesting caps for the three structured-configuration integrations: XML at 256
  element levels, JSON at 200 object/array levels, and TOML at 100 container
  levels. Each is reported as an ordinary diagnostic, and none raises an
  exception.
- A workspace demo page, `docs/demo.md`, with recordings of `pyinc-tools`
  watching a pinned checkout of pytest, linked from the README. The clips ship
  as 1.8 MB of media in the sdist. The page and the README carry no measured
  timings, and the recordings stand on their own.

### Changed

- The default backdate decision in `checked` and `fast` now matches `strict` in
  three corners where it used to diverge. A recomputed dataclass whose type name
  (its qualified name) changed while its fields stayed equal counts as a change
  in every mode. So does a dataclass replaced by a dict of the same shape. Both
  used to backdate in `checked`/`fast`, because thawing dropped the type
  identity. Default comparisons now invoke no `ValueAdapter` `thaw`/`freeze`
  hooks in any mode. Queries with an `eq=` or `cutoff=` policy are unaffected.
- `pyinc-tools analyze --fail-on` combined with `--watch` is rejected as a usage
  error (exit status `2`), because watch mode never terminates normally. The
  default remains `--fail-on none`, so existing invocations keep their previous
  output and exit status.
- `===` clauses evaluate, where they used to report `ambiguous`.
- Three previously unstated limits are documented. Cyclic-graph support covers
  mutable containers only, so values crossing through a `ValueAdapter`, `tuple`,
  or `frozenset` cannot be the target of a back-edge. CSV dialect and header
  sniffing inspect only the first 8192 characters. Re-export and base-class
  following both stop at depth 8. Re-export following reports `ambiguous`
  through `follow_depth`/`trail`, and base-class following now names the bases
  it stopped at in `ClassModel.truncated_bases`.
- Inheritance flattening resolves a member name to the definition at the
  shortest inheritance distance from the starting class. It used to take
  whichever definition the depth-first walk reached first. Ties at equal
  distance still go to the earlier depth-first left-to-right arrival. A class
  reached again at a strictly shallower distance is re-walked with the larger
  remaining budget. Every flattened `ClassMember` field (`defining_path`,
  `defining_class`, `range`, `annotation`, `signature`) is now fixed by the
  inheritance graph, base declaration order, and the depth cap, and traversal
  order has no effect. This changes which definition wins where a base and a
  derived class both declare a name. For `class A3: m`, `class A2(A3)`,
  `class A1(A2)`, `class B1: m`, `class D(A1, B1)`, `class_model(D)` used to
  resolve `m` to `A3` and now resolves it to `B1`. The rule differs from C3, so
  it can differ from CPython's MRO, which resolves that example to `A3`.
  `docs/integration-contract.md` states this as a limit.
- A failing resource costs one load per request, where it cost one per reader.
  The first read in a request re-runs the load, and later reads within that
  request re-raise the exception that load produced. The retained exception and
  its traceback are dropped when the request ends, so a permanently failing node
  releases the load frame and anything it allocated.
- A failure record, and every record that transitively depends on it, is omitted
  from a checkpoint and re-executes against live state after `load_checkpoint`.
  The same exclusion covers a failure the kernel could not record. It covers the
  resource record an unprobeable raise contradicted, and the reader that
  consumed such a raise, plus everything above it.
- `resource_load` leaves out a load that raised, and `resource_probe_hit` leaves
  out a load re-run on an unchanged failing probe.
- The XML element walk, the JSON section walk, and the TOML section walk are
  iterative. Payloads and their order are unchanged.
- Configuration nesting past a cap is rejected without analysis. XML at 257
  element levels, JSON at 201 container levels, and TOML at 101 container levels
  now yield an empty payload and one diagnostic naming the limit. For TOML, the
  implicit root table is the first level and each array-of-tables header costs
  two, so 50 nested `[[…]]` headers cross the cap. Below each cap, payloads and
  cutoff tokens are byte-identical to the previous release. The caps keep every
  accepted document within the snapshot depth the value layer supports. TOML's
  cap is half of JSON's because its cutoff encoding spends two snapshot levels
  per table.
- The JSON integration's pre-parse depth scan moves its query fingerprints.
- Code generation selects a schema node's shape by one precedence everywhere:
  `$ref`, then `allOf`/`anyOf`, then `enum`, then `const`, then `type`.
- Root-schema violations are reported as one `unsupported-root-schema` error at
  the document root, naming every keyword collected. They used to raise one
  error per keyword. The message states the rule that models must be declared
  under `$defs` or `definitions`. Generation now proceeds past an ignored
  keyword at the root.
- A definition that declares `type: object` with no `properties` records a
  non-blocking `unconstrained-object-model` warning. The emitted `.py` is
  unchanged, byte for byte. The warning is keyed on the absence of the keyword,
  so `{"type": "object", "properties": {}}` stays silent.
- Malformed ignored-keyword values are `invalid-constraint` errors naming the
  expected shape. The draft-07 tuple form of `items` is
  `unsupported-tuple-items`, where it was `invalid-schema-node`. `prefixItems`
  now suppresses `unconstrained-array-items`. Rejection messages for unsupported
  combinators name the specific rule, where they used to accompany a generic
  `unconstrained-schema` warning.
- Cells whose only problem was IPython syntax report their imports and
  definitions with no diagnostic. They used to report one `syntax-error` and
  nothing else. A cell mixing notebook syntax with broken Python is reported as
  `notebook-non-python-cell`.
- `docs/kernel-contract.md` gained a "Failing Resource Loads" section. Its
  "Explicit Limitations" now names three ambient-read gaps one by one: file
  metadata (`os.stat`, `Path.exists`, `Path.is_file`, `os.path.getsize` and
  `getmtime`), the byte-oriented environment (`os.getenvb`, `os.environb`), and
  the working directory (`os.getcwd`, `Path.cwd`). The guard lets these through,
  so route them through `FileStatResource` or `db.report_untracked_read`.
- The durable checkpoint manifest version is `5`. A manifest written by 3.0.0
  records version `4`, and `load_checkpoint` now refuses it with
  `CheckpointVersionError`. The record layout is identical either way, so only
  the version tells them apart. 3.0.0 recorded no dependencies for a query whose
  resource read raised a caught exception. Such a record warms under this
  release reporting "dependencies unchanged", while a fresh database re-derives
  it from the resource. Re-save affected checkpoints.
- `db.set` and `db.set_many` decide default input equality (no `eq=`, no
  `cutoff=`) on the stored canonical snapshots, the same operands and the same
  decision recomputation uses. They used to compare thawed values, which drops
  `FrozenRecord` type identity. Setting `GridPoint(1, 2)` and then
  `{"x": 1, "y": 2}`, or a same-shaped different dataclass, counted as an equal
  update and was ignored. The stored snapshot was replaced anyway, so a warm
  `strict`-mode dependent kept a dataclass-derived result no fresh database
  produces. This completes the change above that stops default comparisons from
  invoking `ValueAdapter` `thaw`/`freeze` hooks in any mode. The default input
  path now runs no adapter hook beyond freezing the incoming value. Inputs
  declared with `eq=` or `cutoff=` are unaffected and keep comparing the values
  as written.
- A recomputation producing an equal NaN-bearing value backdates like every
  other unchanged value. The default decision compares stored snapshots, and a
  NaN never equals itself. So a query returning `float("nan")` re-ran its
  dependents on every request, even though its canonical digest was unchanged.
  The decision now falls back to the record digests, which normalize NaN to one
  bit pattern. The fallback only adds equality, so the shapes where the two
  disagree the other way (`True` against `1`, `1` against `1.0`) decide as
  before.
- A JSON object key containing a lone surrogate is a `json-decode-error`
  diagnostic. Such a key reaches the cached payload verbatim, as its own section
  name and again in every descendant's dot path. It used to raise an
  exception out of `json_analysis`. Values are unaffected, because they reach
  the payload through `repr`, which escapes a surrogate. Payloads and cutoff
  tokens for every document without a lone surrogate are byte-identical.
- A path that is a directory, or that has a file somewhere in its parent chain,
  reads as a missing file in every shipped file resource. The probes caught only
  `FileNotFoundError`, so a directory raised `IsADirectoryError` and a path
  reached through a file raised `NotADirectoryError`. A probe that raises
  retires the record it was checking. So replacing a tracked `mod.py` with a
  same-named directory raised out of a warm `workspace_analysis`, while a fresh
  database returned the analysis without that module. The workspace walk only
  collects regular files. `FileResource.load` still raises
  `FileNotFoundError`. A permission denial, and every other `OSError`, is a real
  failure and still propagates into the failure records that landed this cycle.
- `DirectoryResource.probe` answers for a path that holds no listing, where it
  used to raise. A listing whose kind changed is now a recorded failure, where
  it used to be unrecordable, and its reader keeps the dependency edge. The read
  still raises `NotADirectoryError` for a path that is a file, which is how a
  workspace walk tells a module from a package. The probe distinguishes "absent"
  from "not a directory", which reads differently and so may not share a probe
  with it.
- A notebook carrying a lone-surrogate escape is reported as one
  `notebook-decode-error` naming the field that holds it, with no cells and no
  kernel metadata. Cell sources, cell types and the kernel metadata reach the
  cached payload and the cutoff token verbatim, where `freeze` cannot snapshot a
  lone surrogate. Outputs and per-execution metadata reach neither, so a
  notebook that stores a surrogate only there keeps its analysis. Payloads and
  cutoff tokens for every surrogate-free notebook are unchanged.

### Performance

- The default backdate comparison runs on the canonical stored snapshots
  themselves. It used to expose both values and re-freeze them. The warm
  recompute path saves a deep thaw, a validation walk, and a re-freeze per
  comparison. Queries with an `eq=` or `cutoff=` policy keep the previous path
  and still receive mode-exposed values.
- Warm resource validation answers an unchanged-probe check from `probe()`
  alone, and runs `probe_and_load` only on a probe miss. Unchanged file reads
  skip decoding their contents and allocating the decoded value they would have
  discarded. Stored probe/value pairs still come from a single atomic
  observation, and a content change under a stable stat signature is still
  detected.
- `WorkspaceSession` holds one kernel request span per public method, so a warm
  `analyze_workspace` validates each resource once per call, where it did so
  once per internal request.

### Fixed

- `~=` implements its PEP 440 definition, where the upper bound is a prefix
  match. It used to be an ordered comparison, so prereleases and dev releases of
  the excluded next release (`3.0a1` against `~=2.2`) satisfied it. An installed
  prerelease of the next major could then report a compatible-release
  requirement as satisfied. Wildcard matching compares the epoch, so `1!1.1` now
  fails `==1.1.*`. Both are verified against `packaging` across epoch, post,
  dev, and prerelease shapes.
- `applicable_requirements` checks an installed version with pre-releases
  allowed, through the same helper dependency checking uses. The two entrypoints
  now agree that an installed `2.0.0rc1` satisfies `>=1.20`. They used to report
  `version_mismatch` and `satisfied` for the same requirement in the same
  environment. `evaluate_version_specifier` keeps resolver-style exclusion
  unless the specifier opts in.
- Marker comparisons route wildcard literals through PEP 440 prefix matching. On
  any 3.x interpreter, `python_version == "3.*"` is true, and
  `python_version != "2.7.*"` is true where it used to evaluate false.
  Requirements guarded by such markers are applicable again.
- `document_diagnostics` orders module-name diagnostics canonically. A `$defs`
  key reorder backdates the canonicalized schema text, and it used to leave an
  incremental database returning a diagnostics tuple ordered differently from a
  fresh one's.
- A definition named after a binding the generated module itself uses (`str`,
  `Literal`, `dataclass`, and the rest of the emitter's closed set) is rejected
  with the blocking `reserved-definition-name` diagnostic. It used to emit code
  whose imports shadow `typing` and builtins under type checking, with nothing
  reported.
- `{"type": null}` produces the blocking `invalid-type` error at the `/type`
  pointer, like any other invalid type value. It used to be conflated with an
  absent key and generate with only an `unconstrained-schema` warning.
- The polling watcher keeps every update. Its baseline reflects the content the
  mirror was synced from, so a file edited between session construction and the
  first poll is detected and refreshed. The whole initial analysis runs in that
  window. A failed refresh returns its paths to pending, so the next tick
  retries the change, where it used to drop it forever.
- An LSP notification handler that fails with an unexpected error (a mirror
  write hitting a full disk, a client opening a directory URI) is logged, and
  the server keeps serving, matching the request path's guard. It used to
  terminate the process with a traceback.
- A value change two levels above an untracked query invalidates its transitive
  dependents. A recomputation that lands a changed value now moves the revision,
  the way input and resource changes always did. Before, the parent of a
  `report_untracked_read` query re-executed and stored its new value at the same
  revision its own dependents had already verified. A grandparent kept reporting
  `dependencies unchanged`, and `db.get()` diverged from a fresh `Database`. An
  untracked query that re-executes to a byte-identical value leaves the revision
  alone, so warm requests over a stable graph still settle.
- Strict mode exposes cyclic and shared query results, and `read_input` values,
  through the same immutable container views it already used for query
  arguments. It used to leak the raw `FrozenGraph` snapshot, which crashed
  `len()` and iteration with `TypeError`. Those views also freeze back. Passing
  one into `db.set` or as a query argument re-encodes it to the identical
  canonical snapshot (same fingerprint, same cache node), where it used to hit
  `RecursionError`.
- The environment guard installed by `Database` matches `os._Environ` again.
  Both `|` union directions and `|=` work. They used to raise `TypeError` for
  any code in the process once a `Database` had ever been constructed. The
  `encodekey`/`decodekey`/`encodevalue`/`decodevalue` helpers are reachable.
  Every other attribute, including the raw backing mapping, raises
  `AttributeError`, and union reads inside a query still require a `Resource`
  scope.
- An `@action` whose output layout migrates between a file and a directory
  (`pkg` ↔ `pkg/model.py`) reconciles, where it used to wedge permanently on its
  own ledger. Manifest entries that conflict with the new layout are deleted as
  orphans of the previous layout. The deletion runs before publication, under
  the usual tamper policy, and prunes only directories it left empty. `plan()`
  reports those deletions without mutating. Every reconcile and every `plan()`
  used to raise `ActionPathError` until the manifest was edited by hand.
- Requirement lines carrying pip per-requirement options parse correctly.
  `pip-compile --generate-hashes` output used to fold `--hash=...` continuation
  lines into the version text, which corrupted the specifier and misreported
  every requirement in a hashed lockfile. `applicable_requirements` reports an
  undecidable specifier as `ambiguous`, matching dependency checking. It used to
  report `version_mismatch`.
- The LSP server serializes writes to its output stream. A watcher-thread
  diagnostics notification used to be able to interleave with a main-loop
  response and corrupt `Content-Length` framing. The same lock guards the
  published-diagnostics bookkeeping.
- The session lookups behind `textDocument/declaration` and the rename preflight
  take the session lock like every other entry point. They used to read the
  workspace mirror mid-refresh. Like the rest of the session surface, they raise
  `RuntimeError` after `close()`.
- `WorkspaceSession` diagnostics name the real workspace path in their message
  text. They used to leak the temporary workspace-mirror path. A kernel
  `Diagnostic` has no path field, so an integration that needs to name a file
  interpolates it into the message. Under a session, that file was the mirror
  copy, in a randomly named temporary directory. `source-decode-error`, `cycle`,
  `missing-requirements-file`, and the `-r path outside project` error now name
  the real workspace path, matching the `path` field, which was already correct.
  Affected messages are therefore identical across runs, which
  `pyinc-tools analyze --format text` and LSP `publishDiagnostics` both depend
  on.
- A query that catches an error from a resource read is now from-scratch
  consistent as the file appears and disappears. Reading an optional
  configuration file with the ordinary `try: ... except FileNotFoundError:`
  pattern recorded no dependency at all. After the file was created, `db.get()`
  kept returning the default while a fresh `Database` returned the file's
  contents, and `inspect()` reported `dependencies=()` with reason
  `dependencies unchanged`. Deleting a file the query had already read failed
  the mirror-image way. `FileNotFoundError` propagated out of `db.get()` from
  inside the invalidation machinery, before the query body ran, so the query's
  own `except` clause never saw it. Both now match a fresh `Database`.
- An observation that raises without leaving a record retires the record's
  stored probe. A world that returns to the state that probe describes (an undo,
  a branch switch back) re-loads, and the queries reading it are invalidated.
  Before, the probe was reused, and those queries answered at a revision their
  own dependents had already verified past. Replacing a tracked file with a
  directory and then restoring it left every transitive dependent holding a
  stale value permanently.
- The cutoff functions of `json_analysis` and `config_analysis` handle an
  over-deep document without raising `UnsupportedValueError`. That raise was
  reachable on a post-edit recomputation with enough stack. `xml_analysis`
  handles a deeply nested document without raising `RecursionError`.
- Stack-exhaustion diagnostics carry fixed text. They used to carry
  `RecursionError`'s message, which varies with where the stack blew and was
  flowing into a cached payload.
- Base-class flattening keeps every member, whatever the traversal order. A
  class first reached at the depth cap was recorded as visited without
  contributing anything, so a later, shallower reach of that same class was
  skipped. That could report a subclass as having strictly fewer members than a
  base it inherits from.
- `{"type": "object", "const": V}` in a definition reports
  `const-type-mismatch`. It used to drop the `const` and emit an empty
  dataclass. Annotations on an `anyOf` null branch are validated like
  annotations anywhere else. An `anyOf` whose branches are both
  `{"type": "null"}` is rejected, where it used to compile to a bare `None`
  type.
- Mirror sync recovers when a workspace path swaps kind: a tracked file replaced
  on disk by a same-named directory, or the reverse. The conflicting mirror
  entry, and any conflicting entry between it and the mirror root, is cleared
  before the new one is materialized. The refresh succeeds, where it used to
  raise and leave its paths pending for every later watcher tick to retry. A
  mirrored child whose parent is a file again is dropped, because the traversal
  reports that case as absence. A symlinked parent reports the same errno under
  `O_NOFOLLOW` and is still rejected as an unsafe path component. An overlay
  write lands over a mirror directory the swap left behind.
- The polling watcher stops only for a closed session. A `RuntimeError` raised
  while collecting the snapshot or refreshing, including a `RecursionError` (a
  `RuntimeError` subclass), now reaches the watcher's error handler, and the
  loop keeps polling. It used to retire the watcher thread silently, as though
  the session had closed.
- `WorkspaceSession`'s request lock is released even when tearing down its
  integrations request scope or its kernel request span raises. The span closes
  even when the scope exit raises. Before, a failure below the session could
  leave the lock held, which would have deadlocked every later call on that session,
  `close()` included. It could also leave a kernel request open past the
  stability it declares.
- A property named after a binding the generated module itself uses (`str`,
  `dict`, `Literal`, and the rest of the emitter's closed set) is rejected with
  the blocking `reserved-field-name` diagnostic at the property. The field bound
  that name for the rest of its own class body. The annotations after it stopped
  naming the builtin or the `TYPE_CHECKING` import they spell, so `zone: str`
  read the model's own `str` field. The emitted module failed type checking with
  nothing reported by analysis.
- `enum` and `const` members are checked against the nullable union declared
  beside them. They used to be reported as disagreeing with it. Only a string
  `type` was understood, so every member of `{"type": ["string", "null"],
  "enum": ["red", null]}`, including the ones that matched, produced an
  `enum-type-mismatch` or `const-type-mismatch` error. A definition-level enum
  also rejected the union as an `unsupported-enum-type`. A member matching
  neither the type the union names nor the null it adds is still an error.
- A definition whose alias resolves straight back to its own name (through a
  bare `$ref`, a single-branch `allOf`, or a nullable `anyOf`) is reported as
  `self-referential-alias`. It used to emit `Loop: TypeAlias = 'Loop | None'`,
  which no type checker can resolve. Recursion through a model or a container
  (`Tree` → `list[Tree | None]`) names a type and still generates.
- An `@action` run that stops between publishing its outputs and publishing its
  ledger leaves later runs able to proceed. A recorded output whose parent path
  is now a file cannot exist. A recorded output whose path is now a directory
  holding only files of the desired layout was already released by the stopped
  run. Preflight recognizes both, so the next locked `reconcile()` and `plan()`
  converge the set. They used to raise `ActionPathError` for every desired set
  until the manifest was edited by hand. Recovery never deletes to repair, so a
  directory holding any other entry still refuses under the tamper policy. Files
  a stopped run published but never recorded stay unowned. A rollback or
  teardown that would have to remove them is still refused until a reconcile of
  the published layout records them.
- `plan()` reports the prune refusal `reconcile()` enforces. During preflight, a
  directory that the previous layout must leave empty is checked for unowned
  entries, and the refusal names the blocking entry. A dry run used to report a
  clean migration that the next reconcile abandoned after deleting its orphans.
  That reconcile now refuses before deleting anything.
- `freeze` detects sharing across the whole boundary value, where it used to
  look at one wrapper at a time. A `strict`-mode result like `(items, items)`
  (one list reached twice through a raw tuple) stores a `FrozenGraph`.
  Re-freezing the exposed view returned the tree `(FrozenList, FrozenList)`, so
  the view failed to round-trip to its own snapshot or fingerprint, through
  `db.set` or as a query argument. Only the `[items, items]` spelling worked,
  because a list spine keeps the aliasing inside one wrapper. Fingerprints for
  every shape that already round-tripped are unchanged, and a tuple carrying no
  `Frozen*` wrapper pays nothing.
- A JSON document carrying a lone-surrogate escape (`"\ud800"`) completes an
  incremental recomputation, as a fresh database does. `json.loads` accepts the
  escape and the snapshot grammar refuses it. The cutoff's defensive clause
  named only `ValueError`, and `freeze` raises `UnsupportedValueError`, a
  `PyIncError`. The clause now names it and degrades to the raw text as
  intended. The TOML and XML cutoffs gained the same name defensively, though
  neither parser can produce a lone surrogate.
- `fingerprint_snapshot` and `serialize_snapshot` reject an integer wider than
  CPython's int-to-str conversion limit with `UnsupportedValueError` naming the
  digit limit. They used to let a raw `ValueError` out of the encoder. The `K2`
  grammar is unchanged.
- A notebook with a lone-surrogate escape gets one consistent analysis. It used
  to fail two different ways. A fresh read raised `UnsupportedValueError` out of
  the payload, and an incremental one raised it out of the cutoff with a
  different message. A first read, a post-edit incremental read, and a database
  that never saw the file now agree, and none of them raises.

## [3.0.0] - 2026-07-12

### Release validation

- RC candidate: `v3.0.0rc1` at `6296106725e372a428dfeca5e45390f8cd2821fa`
- [x] Clean installations from the published RC artifacts passed.
- [x] The benchmark/correctness report was reviewed, and every pyinc result
  matched a fresh run.
- [x] Final promotion approved.

## [3.0.0rc1] - 2026-07-12

### Added

- Stable keyed `Input` and `Query` identities, optional `@query(key=...)`, a
  public generic `Resource` contract, `Database.read_resource`, and
  `BinaryFileResource`.
- Zero-based `SourcePosition` / `SourceRange` geometry, public `DocumentMap`
  encoding conversion, and lexical `SymbolId`, `Scope`, `Binding`, and
  `ScopeTree` resolution shared by Python navigation and refactoring features.
- Code-generation diagnostic severities and JSON Pointers, with
  `SchemaGenerationError` preventing reconciliation when an error diagnostic
  exists.
- `pyinc-tools --version`, LSP 3.18 position-encoding negotiation, Python 3.14
  support, and installed-wheel validation in CI.
- `python -m pyinc_tools` and `python -m pyinc_tools.cli` module execution.
- Task-oriented getting-started and LSP references, and an offline documentation
  checker for links, anchors, executable examples, CLI output, and the
  documented stable integration surface.
- A correctness-first benchmark workflow that uploads five isolated-run
  `samples.csv`, summarized `benchmark.csv`, `benchmark.md`, and provenance-rich
  `metadata.json` artifacts.
- Automated GitHub Releases after PyPI publication, and a manual 12-environment
  workflow that validates exact published artifacts and compares PyPI and GitHub
  Release hashes.

### Changed

- Replaced marshal-based code identity with canonical typed code-object
  encoding. It covers slice and nested-code constants, definition defaults,
  immutable and transitive captures, comparator policies, resource/adapter
  implementations, and relevant interpreter/build flags.
- Unpinnable equality/cutoff policy captures and local or dynamically unbound
  class captures are rejected. They used to collapse to name- or type-only
  identities. Input policies, resources, and adapters now each include
  interpreter/build identity at their checkpoint trust boundaries.
- Checkpoints use fully prevalidated manifest schema v4, and v1-v3 checkpoint
  manifests are rejected by design. The `K2` user-value encoding is unchanged.
- `set_many` is all-or-nothing. Query execution commits records and dependency
  rewiring only after success, and all public database state operations are
  locked. Profiles use bounded timing aggregates, and evicted nodes leave no
  profile or registry state behind.
- `ReconcileResult.written` is replaced by `created`, `updated`, and `repaired`.
  Action manifests use root-bound schema v2 and a SHA-256 name derived from the
  full tool identity.
- Python source is decoded with PEP 263/BOM rules, and AST byte columns are
  converted to Unicode-code-point ranges at the parser boundary. Unsupported
  attribute chains return no result, where they used to return speculative
  locations or edits.
- `pyinc_tools` diagnostics, locations, highlights, edits, links, lenses,
  semantic tokens, and hierarchy results expose direct `SourceRange` (or
  `SourcePosition`) fields. `WorkspaceSession.find_references` and rename use
  resolved `SymbolId` values. Name-only access and v2 coordinate aliases are
  removed. The workspace mirror rejects file symlinks.
- `pyinc_tools` separates shared models, document geometry, pure analysis, edit
  generation, workspace mirroring/watching, and JSON-RPC framing behind the
  lock-owning `WorkspaceSession` façade. Tools moved from private resolver
  internals to the stable public integration surface.
- `pyinc_tools` carries its own identifier-lexing helper and stops importing the
  kernel-private `pyinc._python_lexing` module. Both consumer packages stay on
  the public `pyinc` / `pyinc.integrations` contracts.
- Generated model packages use deferred annotations and type-checking-only
  imports for cyclic local references. Definition/module collisions are checked
  after Unicode normalization, snake conversion, and case folding.
- Each guide or contract now has one purpose. The documentation uses PyPI-safe
  navigation, describes the exact from-scratch-consistency guarantee and frozen
  container types, and keeps protocol operation details in a compact LSP
  reference.
- Benchmark correctness, fixed row coverage, deterministic work counts, and node
  ceilings are release gates. Wall timings are informational medians with
  min/max ranges and no `tracemalloc` instrumentation. Generated reports stay
  out of the repository.

### Fixed

- Filesystem artifact publication and action reconciliation are serialized
  across processes. Writes are flushed and atomically published from the same
  directory, through no-follow filesystem handles where available, and
  conflicting artifact bytes are refused.
- Action preflight rejects malformed manifests, unsafe or ambiguous paths,
  symlink escapes, non-regular owned targets, malformed digests, and conflicting
  file/directory declarations before mutation.
- Unsafe or non-regular action and artifact lock paths surface typed
  `ActionPathError` / `ArtifactStoreError` failures. They used to surface raw OS
  errors.
- Workspace mirrors use content hashes, filter source/configuration inputs,
  honor exclusion globs, retain recursively referenced requirements files
  whatever their suffix, surface requirements-chain diagnostics, and reject
  escaping symlinks.
- XML analysis rejects every `DOCTYPE` and entity declaration before parsing,
  including external-entity and entity-expansion payloads.

### Security

- Release builds and validation run without OIDC publishing privileges. A
  separate minimal trusted-publishing job receives only the verified sdist and
  wheel artifacts.
- Checkpoint records are validated completely before any cache warming, and
  resource implementation changes invalidate reuse even when probes happen to
  match.
- Artifact-store keys are restricted to lowercase SHA-256 digests (optionally
  checkpoint-prefixed with `ck`), which prevents path traversal and platform
  path injection.
- Tag publication waits for reusable CI, CodeQL, and benchmark gates. The GitHub
  Release job receives only `contents: write`, reuses the exact verified
  distributions, and publishes their `SHA256SUMS` file.

### Migration

- This is a clean API and persistence break. Read `docs/migration-v3.md` before
  upgrading, and discard v2 checkpoint/action ledger state as described there.

## [2.6.0] - 2026-07-05

### Added

- New integration entrypoint `symbol_resolution.class_model(db, root, path,
  qualified_name)` returns a `ClassModel(path, qualified_name, members,
  unresolved_bases)`, the declaration-only member set of a workspace class.
  `ClassMember` (`method` / `class_variable` / `instance_variable`, each
  carrying `defining_path` / `defining_class`) covers class-body variables,
  methods, and `self.NAME` instance attributes collected from methods whose
  first parameter is named `self`. The model is flattened over workspace base
  classes depth-first, left-to-right, first definition wins (a derived member
  shadows a base member of the same name). Flattening is bounded by
  `MAX_BASE_DEPTH = 8` with a cycle guard. Base files are queried one at a time
  (`class_models_for_file`), so an edit to one base invalidates per file. The
  order differs from C3 MRO by design. Bases that resolve to no workspace class
  (stdlib / installed / missing / ambiguous / starred) contribute no members and
  are listed in `unresolved_bases`. `ClassMember` and `ClassModel` join the
  stable `pyinc.integrations` surface. No kernel contract change.
- `pyinc-tools` LSP completion serves instance-member lists that used to need
  type inference, all from the new `symbol_resolution.class_model` surface.
  Completion stays declaration-driven, with no runtime types. `self.` / `cls.`
  inside a method complete the enclosing class's instance / class view. A bare
  name whose declared annotation (bare `Name`, one-hop `mod.Foo`, or
  whole-string forward reference) names a workspace class completes that class's
  instance view. A bare `Foo.` class owner serves the flattened class view, so
  `Derived.` and `self.` alike show members inherited from workspace base
  classes. Subscripted / union / deep-dotted / callable annotations, chained
  owners (`obj.attr.`), closures over the receiver, and non-workspace bases
  contribute nothing. No kernel or `pyinc.integrations` contract change beyond
  the `class_model` surface above.
- The `pyinc-tools` LSP refines its shipped completion and signature-help
  features in three ways:

  - Completion handles dotted attribute owners. `pkg.sub.<caret>` completes when
    the dotted owner is itself a workspace module (its exports), and
    `pkg.sub.C.<caret>` / `M.C.<caret>` complete a class's members when the
    owner is `<workspace-module>.<class>`. Owner resolution is longest-match
    first. It routes module lookup through an exact `workspace_symbol_index`
    match, so ambiguous resolutions produce no results. Single-component owners
    keep the existing `resolve_symbol` path. Instance chains
    (`obj.attr.<caret>`) and stdlib/installed owners still yield nothing.
  - signatureHelp covers attribute calls. `M.foo(` and `M.C(` surface a
    signature. A single-dot owner that is a bare `Name` is resolved through the
    file's imports to a workspace module, and then to the attribute inside it.
    This is the same bare-`Name`-LHS idiom that `callHierarchy/outgoingCalls`
    and `inlayHint` use, now a shared `_resolve_attr_on_module` helper. Deep
    chains (`pkg.sub.foo(`) and subscripted calls stay `null`.
  - Signature labels show default values. Signature-help labels render parameter
    defaults (`name: ann = default` / `name=default`), read from the defining
    file's source. `symbol_resolution.Parameter` is unchanged because the
    contract type carries no default, so this is a consumer-side read.
    Completion `detail` and hover are untouched.

  `pyinc_tools` also exports the existing `CompletionItem` /
  `CompletionItemKind` types. No kernel or `pyinc.integrations` contract change.
- The `pyinc-tools` LSP handles `textDocument/linkedEditingRange`. The server
  advertises `linkedEditingRangeProvider: true`. For the symbol under the
  cursor, it returns the ranges in the current file that an editor should mirror
  as the user types, so editing one updates them all live. It also returns a
  `wordPattern` of `[A-Za-z_][A-Za-z0-9_]*`, which tells the client to stop
  mirroring once the typed text stops being a Python identifier.

  The mirrored ranges are the same file-scoped occurrences that
  `textDocument/documentHighlight` already reports. They are the declaration
  name span (repaired off the synthetic `def` / `class` placeholder that
  `find_references` emits) plus every verified bare-name and rightmost-attribute
  reference. All ranges cover the same bare identifier, so they are safe to edit
  together. The feature is in-file only and lighter than `textDocument/rename`
  by design. It never touches other files, so workspace-wide renames still go
  through `rename`. Unknown identifiers, whitespace cursor positions,
  non-workspace targets (stdlib / installed / ambiguous / missing), and files
  outside the workspace return `null`.

  New consumer-layer dataclass `LinkedEditingRange(lineno, col_offset,
  end_col_offset)` (1-based `lineno`, 0-based `col_offset` / `end_col_offset`,
  like the other session dataclasses). New entrypoint
  `WorkspaceSession.linked_editing_ranges_at(path, qualified_name) ->
  tuple[LinkedEditingRange, ...]`, thread-safe through the same `_state_lock` as
  every other public mutator, since it delegates to `find_document_highlights`.
  It uses only the stable `pyinc.integrations` public surface
  (`find_references`). The kernel contract and the integration-layer surface are
  unchanged. `docs/pyinc-tools-guide.md` documents the limitations.
- The `pyinc-tools` LSP reports an `unused-import` diagnostic. Analysis flags a
  workspace `from M import name [as alias]` binding that nothing in the file
  uses. It is conservative by design. Only `from` imports that resolve to a
  workspace module are considered, so `find_references` can verify usage.
  `import M`, stdlib / installed targets, and `from M import *` are left alone.
  `__init__.py` files, self-alias re-exports (`from y import z as z`), and
  bindings another workspace module re-imports from this file (a cross-module
  re-export) are never flagged. The diagnostic has severity Hint and carries the
  LSP `Unnecessary` tag (`tags: [1]`), so editors fade the binding. It rides
  both the push and pull diagnostic channels. The new additive field
  `AnalysisDiagnostic.tags: tuple[str, ...]` is folded into the pull-model
  `resultId` signature, so a tag change re-issues the report.
- The `pyinc-tools` LSP offers `textDocument/codeAction` quick fixes. The server
  advertises `codeActionProvider: {codeActionKinds: ["quickfix"]}` and answers
  with diagnostic-anchored quick fixes and no refactorings. For diagnostics
  intersecting the request range it offers *Remove unused import*
  (`unused-import`) and *Remove unresolvable import* (`missing-import`). For
  `unresolved-symbol` it offers a *Remove import of 'name'* action, plus an
  *Import 'name' from '<module>'* retarget when only one workspace module
  exposes a top-level symbol of that name (single-name statements only). Each
  action echoes its anchor diagnostic and carries a `WorkspaceEdit`
  (`{"changes": {uri: [TextEdit]}}`), and `context.only` is honored. New
  consumer-layer dataclasses `CodeAction(title, kind, diagnostic, edits)` and
  `CodeActionEdit(path, start_line, start_character, end_line, end_character,
  new_text)` (0-based, LSP-style), and entrypoint
  `WorkspaceSession.code_actions_for_range(path, start_line, start_character,
  end_line, end_character)`. It reuses the existing import-deletion geometry
  (`_statement_line_span` / `_alias_list_deletion_edits`) and
  `workspace_symbol_index`. The kernel and integration-layer contracts are
  unchanged.
- The durable cross-run cache is a trusted guarantee. The `save_checkpoint` /
  `load_checkpoint` flow shipped in v2.0.0 gave only a best-effort warm. The
  checkpoint path now earns from-scratch consistency across processes and runs,
  under the conditions restated in `docs/kernel-contract.md` limitation 4:
  single-process store access, the checkpoint's inputs set before load,
  resources honouring the probe contract, and adapters registered with unchanged
  implementations. The supporting machinery:

  - Query identities are deterministic across processes. `Input` carries a
    per-name `seq` ordinal, so same-named inputs resolve to the correct node on
    reload. Captured queries fold their full definition payload into the
    parent's identity transitively, so a body edit to any dependency query moves
    the parent. The code fingerprint includes the build configuration (`-O`
    optimize flag, platform, `os.name`, UTF-8 mode) alongside the interpreter
    and version tuple.
  - Frontier reuse is execute-to-verify. A checkpoint dependency that cannot be
    warmed directly is re-executed from its pinned code, with resources probed
    against the real world, and its result digest is compared to the manifest. A
    warmed subtree is trusted only when its frontier reproduces.
  - Adapter implementations are digested. Each registered adapter's
    `freeze`/`thaw` body is fingerprinted and recorded in the manifest. Every
    thaw-into-live path refuses a record whose adapter has changed or vanished
    since the save, even for a change to `thaw` alone.
  - The checkpoint manifest uses schema v3. It is canonically sorted and
    content-addressed, with the kernel fingerprint version cross-checked at
    load.

  On upgrade, checkpoint keys written before this branch fail to load, because
  `load_checkpoint` rejects their older manifest schema with `ValueError`. Drop
  the old key and call `save_checkpoint` again. Within v3 checkpoints, records
  whose identities shift (interpreter, build configuration, or code changes)
  miss safely. The affected queries re-execute on the first `get` (a one-time
  re-execution wave) and their stale records go unused. Stored snapshot
  artifacts stay valid either way, because the `fingerprint_snapshot` encoder
  (`K2;`) is unchanged, so an existing object store needs no rewrite. No
  `pyproject.toml` version bump accompanies this (release hygiene is tracked
  separately).
- The `pyinc-tools` LSP serves completion (`textDocument/completion`). The
  server advertises a `completionProvider` (`{"triggerCharacters": ["."],
  "resolveProvider": false}`) and serves declaration-driven completion.
  Candidates come from real `symbol_resolution` bindings and import resolution,
  never from inferred runtime types. Three contexts are recognised. A bare-name
  prefix completes current-file module-level symbols, workspace module names,
  and Python keywords. Attribute access `M.<prefix>`, for a bare-name `M` that
  resolves to a workspace module or class, completes the module's exports or the
  class's methods and class variables. In import position,
  `from pkg import <prefix>` completes `pkg`'s names, and `import <prefix>`
  completes workspace module names. A mid-edit buffer is usually unparseable at
  the caret (a trailing `owner.`), so the server repairs the caret line to
  `pass` before analysis, which keeps every top-level import and definition for
  resolution. Items carry `label` / `kind` / `detail` (signature label for
  callables, declared annotation for variables). Strings, comments,
  non-bare-`Name` owners, and stdlib / installed targets yield nothing. The
  consumer entrypoint is `WorkspaceSession.completions_at(path, line,
  character)`. It uses only the stable `pyinc.integrations` surface, with no
  kernel change. `docs/pyinc-tools-guide.md` documents it.
- `@action` adds a declared-output reconciliation layer. It is a new,
  domain-agnostic kernel surface that turns query-derived desired artifacts into
  files on disk and keeps side effects out of queries. `Output(path, content)`
  is snapshot-safe, so a `tuple[Output, ...]` can be a `@query` return and take
  part in caching and backdating. `@action(tool=...)` wraps a pure
  `(db, *args) -> Iterable[Output]` function, and `Action.reconcile(...)` /
  `Action.plan(...)` apply it to the filesystem. Reconciliation:

  - writes only outputs whose on-disk bytes differ from the desired bytes (the
    same content-hash rule repairs out-of-band edits to generated files);
  - deletes outputs the action owned before but has stopped declaring, using a
    per-`tool` JSON ownership ledger, so files the action did not write are
    never touched;
  - writes atomically (temp file + `os.replace`) and skips the manifest write
    when nothing changed, so a no-op reconcile performs zero filesystem writes;
  - supports a dry-run `plan` that reports `written` / `deleted` / `unchanged`
    and leaves the disk untouched.

  Reconciliation runs at top level only. Query semantics, the value membrane,
  untracked-read enforcement, and the modes are unchanged. The kernel's
  from-scratch guarantee lifts to the filesystem: incremental reconciles equal a
  fresh run into an empty directory. `pyinc` exports `Output`,
  `ReconcileResult`, `Action`, and `action`, and `docs/action-contract.md`
  documents them. The runnable examples are `examples/action_reconcile_demo.py`
  and the end-to-end include-aware `calc` fixture (`examples/calc/`,
  `examples/calc_demo.py`), the canonical worked example for a query graph that
  reconciles outputs to disk.
- `pyinc_codegen` is a JSON-Schema → typed-Python compiler. It is a new consumer
  package (`src/pyinc_codegen/`) and the first useful file→file compiler built
  on pyinc. It reads a JSON Schema and generates one typed model and one doc
  file per definition, plus an aggregate `__init__.py`. Output goes through the
  `@action` layer, so only changed artifacts are written.

  - The supported subset is local documents, `$defs` and legacy `definitions`,
    local `$ref`, object `properties`, `required` vs optional, arrays,
    primitives, `enum`, nullable unions, `description` (docs only), and
    deterministic diagnostics for unsupported constructs.
  - It is decomposed for output-granular incrementality. Whitespace and
    key-reorder edits backdate (zero writes). A description-only edit rewrites
    only the doc. A property type or requiredness change rewrites the affected
    model and its reference-graph closure, each only if its bytes change. Adding
    or removing a definition touches only that definition's files plus the
    index.
  - It is stdlib-only (JSON parsed with `json` plus dict walking) and built only
    on pyinc's public API, so no JSON-Schema concept lives in `src/pyinc`. Its
    public surface is `generate`, `generate_outputs`, `schema_analysis`, and the
    `SchemaModel` / `FieldModel` / `Diagnostic` / `SchemaAnalysis` result types.
    A sample schema and a runnable demo are in `examples/`, and
    `docs/codegen-guide.md` documents it.
- A reproducible benchmark and correctness harness (`bench/`), shipped outside
  the wheel. It exercises four targets (synthetic kernel query graphs, the calc
  fixture, JSON-Schema codegen, and action reconciliation) across a canonical
  edit sequence: cold, unchanged, unreferenced edit, comment-only edit,
  localized edit, high-fan-out shared edit, removed artifact, tampered output,
  checkpoint restore. It compares pyinc against full recomputation, a naive
  per-key cache, and `joblib.Memory`, and records wall time, peak memory,
  dependency-graph size, and cache size. It writes a CSV and Markdown report
  under `bench/results/`. Every scenario pairs its timing with a correctness
  assertion that pyinc's incremental output equals a fresh, cache-free run. The
  tampered-output scenarios drive the real action reconcile path. `joblib` is a
  new `bench` optional-dependency group, imported lazily and never by
  `src/pyinc` or `src/pyinc_codegen`. Run the harness with
  `PYTHONPATH=src python -m bench.run`.
- The `pyinc-tools` LSP `serverInfo.version` is bumped from `"2.1.0"` to
  `"2.6.0"` to match the package version pinned in `pyproject.toml`.

### Fixed

- Wildcard version-specifier prefix matching in `requirement_evaluation` uses
  the full spec release. `==X.Y.*` / `!=X.Y.*` specifiers used to trim trailing
  zeros from the spec's release before comparing, which shortened the prefix, so
  `==1.0.*` wrongly matched any `1.x` release (such as `1.5`). The full spec
  release is now the prefix, so `==1.0.*` matches only `1.0.x`.

- The checkpoint warm path rejects stale and tampered values. The v2.0.0 warm
  restored records without their dependency edges and trusted whatever bytes the
  store returned, so a warmed cache could serve a value a fresh run would not
  produce. Every such path is closed:
  - restored records carry their real dependency edges and are re-verified
    transitively through them, which replaces the old warm-time bypass;
  - resources are re-probed, or their queries re-executed, live at reload, where
    the stored probe hint used to be trusted blindly;
  - every snapshot read from the store is rejected unless `sha256` of its raw
    bytes matches the digest it was keyed by, and the manifest is re-hashed
    against the checkpoint key before anything is parsed out of it;
  - any dependency that cannot be resolved or verified (runtime-import-reached
    query subgraphs, untracked `report_untracked_read` records, missing or
    corrupt store bytes) refuses the warm and re-executes.
- Code fingerprints are independent of refcounts. `_code_fingerprint` marshals
  code objects with `marshal` format 2 in place of the default. Format ≥3
  encodes interning / `FLAG_REF` state, so a code object's bytes could flip once
  one of its string constants gained a reference at runtime (for example, a
  regex literal retained by `re`'s cache after first use). A query's identity
  then depended on live refcounts and shifted between two keyings in the same
  process. Format 2 fully encodes the code object without shared references, so
  identities are stable within a process and reproducible across processes.
- Dirty-graph saves leave stale records out. `save_checkpoint` omits any record
  whose cached value disagrees with the live graph, meaning a dependency moved
  since the record last executed, with no `get` in between. Persisting it would
  bake in the dependency's new digest while warming the old value on reload.
  Such records re-execute after reload.

## [2.5.0] - 2026-06-05

### Added

- The `pyinc-tools` LSP supports pull diagnostics (`textDocument/diagnostic` and
  `workspace/diagnostic`). The server advertises a `diagnosticProvider`
  (`{"identifier": "pyinc-tools", "interFileDependencies": true,
  "workspaceDiagnostics": true}`) and implements the LSP 3.17 pull-diagnostic
  model alongside the existing `textDocument/publishDiagnostics` push channel.

  - `textDocument/diagnostic` runs `analyze_file` on the requested document and
    returns a full report `{"kind": "full", "resultId", "items"}`. Its `items`
    are the same `Diagnostic` objects the push channel emits for that file
    (codes `missing-import`, `ambiguous-import`, `undeclared-import`,
    `unresolved-symbol`, `ambiguous-symbol`, plus `pyinc.python_source` parse
    errors). A clean file returns a full report with empty `items`. A pull for a
    URI outside the workspace returns an empty full report, and the request
    succeeds.
  - `workspace/diagnostic` runs `analyze_workspace` once and returns
    `{"items": [...]}`, with one report per analyzed `.py` file (plus any config
    / requirements file that carries dependency diagnostics), sorted by path.
    Files that are now clean still get a report with empty `items`, so clients
    can clear stale problems. `version` is always `null`.
  - The pull channel is stateless. Each `resultId` is a SHA-256 over the file's
    diagnostic signatures. When the client echoes a matching `previousResultId`
    (or `previousResultIds: [{uri, value}]` for the workspace request), the
    server answers with an `unchanged` report and skips the resend. The server
    adds no per-document bookkeeping, so the push and pull channels coexist
    without interference.

  It uses only the stable `pyinc.integrations` surface (`analyze_file` /
  `analyze_workspace` already drive the push channel). The kernel contract and
  the integration-layer surface are unchanged. `docs/pyinc-tools-guide.md`
  documents it.
- The `pyinc-tools` LSP handles `textDocument/declaration`. The server
  advertises `declarationProvider: true`, which completes the goto-* family
  (`definition`, `typeDefinition`, `references`, `declaration`). It returns a
  single-entry `Location[]` pointing at the binding statement in the current
  file for the symbol under the cursor.

  `textDocument/definition` differs, because it follows `import` /
  `from … import` chains through to the imported target's file. For a
  declaration, the cursor's identifier is looked up in the current file's
  `ModuleSymbolTable`. An exact `qualified_name` match wins over a bare-name
  match against the last dotted component. The returned range spans the
  bare-name identifier on the matched `Symbol.lineno` line, found by a
  word-boundary scan. Behaviour by symbol kind:

  - `function` / `class` / `method` / `variable` / `class_variable`: the
    declaration coincides with the definition (the def, class or assignment
    line), so `declaration` and `definition` return the same location.
  - `import_alias` / `from_import_alias`: the declaration is the `import` /
    `from … import` statement in the current file, even when the import resolves
    to a stdlib / installed / missing target. For example, clicking on `os` in a
    file that does `import os` returns the `import os` line, where `definition`
    returns `[]` (the LSP surfaces no stdlib targets).
  - `wildcard_import_stub`: the local symbol table records only a literal `*`
    entry and none of the bare names the wildcard brings in, so a bare-name
    reference whose source is `from M import *` returns `[]`.

  Unknown identifiers, whitespace cursor positions, and files outside the
  workspace also return `[]`. New consumer-layer dataclass
  `DeclarationLocation(path, lineno, col_offset, end_col_offset)` (1-based
  `lineno`, 0-based `col_offset` / `end_col_offset`, like the other session
  dataclasses). New entrypoint `WorkspaceSession.declaration_location_at(path,
  qualified_name) -> DeclarationLocation | None`, thread-safe through the same
  `_state_lock` as every other public mutator. It uses only the stable
  `pyinc.integrations` public surface (`module_symbol_table`). The kernel
  contract and the integration-layer surface are unchanged.

- The `pyinc-tools` LSP supports type hierarchy. The server advertises
  `typeHierarchyProvider: true` and implements three new requests:

  - `textDocument/prepareTypeHierarchy` resolves the identifier under the cursor
    through `symbol_resolution.resolve_symbol`. If the target is a workspace
    `class`, it returns a single `TypeHierarchyItem` describing the declaring
    `ClassDef`. The item's `range` spans the whole `class` block (including any
    decorator lines), and `selectionRange` is the bare class-name span on the
    header line. The item's `data` field carries `{"path", "qualified_name"}`,
    so later `supertypes` / `subtypes` requests skip re-resolving. Functions,
    methods, variables, import aliases, `from_import` aliases, wildcard-import
    stubs, and stdlib / installed / ambiguous / missing targets all return
    `null`.
  - `typeHierarchy/supertypes` parses the item's declaring file, finds the
    `ClassDef` matching the item's qualified name, and resolves each entry of
    its `bases` list. `Subscript` bases (`Generic[T]`, `Base[T]`) are unwrapped
    to their `value` once before resolution, so generic base classes still
    navigate. Bare `Name(id=X)` bases resolve `X` through the declaring module's
    imports. `Name.attr` bases resolve the LHS to a workspace module and then
    `attr` inside it, mirroring `find_references`'s LHS-bare-Name handling. Deep
    attribute chains (`pkg.subpkg.Foo`), `Starred` bases, and call expressions
    produce no entry. Only workspace `class` targets contribute items, so stdlib
    / installed / ambiguous / missing bases are dropped. Duplicates by
    `(path, qualified_name)` are collapsed.
  - `typeHierarchy/subtypes` walks the workspace once through
    `workspace_analysis` and visits every `ClassDef` recursively (qualified-name
    nesting follows `module_symbol_table`: `Outer.Inner`). Each base in a
    candidate's `bases` list is unwrapped (subscript dropped) and resolved
    through the candidate's module imports by the same rules as `supertypes`. A
    candidate is a subtype if and only if at least one resolved base points at
    the target `(path, qualified_name)`. The target itself is excluded. Only
    direct subtypes are returned, and clients drill down by calling the endpoint
    recursively. Output is sorted by `(path, qualified_name)`.

  New consumer-layer dataclass `TypeHierarchyItem(name, kind, path,
  qualified_name, detail, range_start_line, range_start_character,
  range_end_line, range_end_character, selection_start_line,
  selection_start_character, selection_end_line, selection_end_character)`, with
  all position fields 0-based (LSP-style) and `kind` typed as
  `TypeHierarchyItemKind = Literal["class"]`. Three new `WorkspaceSession`
  methods: `prepare_type_hierarchy(path, line, character)`,
  `type_hierarchy_supertypes(path, qualified_name)`, and
  `type_hierarchy_subtypes(path, qualified_name)`. All three are thread-safe,
  guarded by the same `_state_lock` RLock as every other public mutator. They
  use only the stable `pyinc.integrations` public surface (`workspace_analysis`,
  `module_symbol_table`, `resolve_symbol`). The kernel contract and the
  integration-layer surface are unchanged.

  `docs/pyinc-tools-guide.md` documents the limitations. The main ones come from
  the existing resolver. `prepareTypeHierarchy` handles top-level identifiers
  only. Only workspace `class` targets count, so stdlib / installed base classes
  are dropped. Deep attribute chains (`pkg.subpkg.Foo`) in the `bases` list are
  skipped. To opt in, use `from pkg.subpkg import Foo` or
  `from pkg import subpkg`. Metaclass relationships are not reported.
- The `pyinc-tools` LSP handles `workspace/willDeleteFiles`. The server
  advertises `workspace.fileOperations.willDelete` with a `**/*.py` file filter
  (alongside the existing `willRename`). For each `{uri}` entry, the server
  walks every Python file in the workspace and returns a `WorkspaceEdit` that
  removes the `import` and `from` statements that reference the module name of
  the file about to be deleted:

  - `import <deleted_module> [as alias]`: when this is the only alias in the
    statement, the whole statement is removed (the edit range covers the full
    statement line, trailing newline included). When the statement has other
    surviving aliases (`import a, b` with `a` deleted), only the dead alias and
    its adjacent comma are removed, so the surviving aliases stay intact.
  - `from <deleted_module> import …`: the whole statement is removed, since
    every imported name's source module is gone. Both absolute and relative
    `from` lines are covered. Relative imports are resolved against the
    importer's own package and matched against the deleted module.
  - `from <pkg> import <leaf> [as alias]` where
    `<pkg>.<leaf> == deleted_module`: when this is the only imported name in the
    statement, the whole statement is removed. Otherwise only the dead leaf and
    its adjacent comma are removed.

  Deletions are skipped without error when the path is outside the workspace, is
  not a `.py` file, or is `__init__.py` (package delete is a separate feature).
  The request returns `null` when no edits are needed. Importers that are
  themselves in the same delete batch are skipped, since the client is about to
  remove them. Multiple deletions in one request are batched against the current
  workspace state.

  New consumer-layer dataclass `FileDeletionEdit(path, start_line,
  start_character, end_line, end_character, new_text)` (all position fields
  0-based, LSP-style, and `new_text` is always `""`). New entrypoint
  `WorkspaceSession.import_edits_for_file_deletions(deletions)` accepts an
  iterable of paths and returns a tuple of edits sorted by
  `(path, start_line, start_character)`. It uses only the stable
  `pyinc.integrations` public surface. The kernel contract and the
  integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `workspace/willRenameFiles`. The server
  advertises `workspace.fileOperations.willRename` with a `**/*.py` file filter.
  For each `{oldUri, newUri}` pair, the server walks every Python file in the
  workspace and returns a `WorkspaceEdit` that updates the `import` and `from`
  statements that reference the renamed file's module name:

  - `import <old_module> [as alias]`: the dotted-module span is rewritten to
    `<new_module>`. Any `as` clause is preserved.
  - `from <old_module> import …`: the dotted-module span (including any leading
    dots) is rewritten. When the importer's relative anchor contains both the
    old and the new module, the existing `level` is kept and only the relative
    tail changes. Otherwise the statement is rewritten to absolute form
    (`from <new_module> import …`, `level == 0`).
  - `from <pkg> import <leaf> [as alias]` where `<pkg>.<leaf> == old_module`:
    the leaf is rewritten to `<new_module>`'s leaf when `old_module` and
    `new_module` share the same parent package. The `as` clause is left alone.
    Cross-directory submodule rewrites of this shape are skipped by design. They
    would need either a rewrite of every `<leaf>.attr` usage site or an inserted
    `as <leaf>` clause, and neither is well-defined here.

  Renames are skipped without error when either path is outside the workspace,
  is not a `.py` file, is `__init__.py` (package rename is a separate feature),
  or keeps the same module name. The request returns `null` when no edits are
  needed. Multiple renames in one request are batched against the current
  workspace state, with no chaining, so a swap A↔B produces independent edits
  for each direction.

  New consumer-layer dataclass `FileRenameEdit(path, start_line,
  start_character, end_line, end_character, new_text)` (all position fields
  0-based, LSP-style). New entrypoint
  `WorkspaceSession.import_edits_for_file_renames(renames)` accepts an iterable
  of `(old_path, new_path)` pairs and returns a tuple of edits sorted by
  `(path, start_line, start_character)`. It uses only the stable
  `pyinc.integrations` public surface. The kernel contract and the
  integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/semanticTokens/range`. The server
  advertises `semanticTokensProvider: {legend: {tokenTypes: [...],
  tokenModifiers: [...]}, full: true, range: true}` (previously `range: false`).
  The request returns a delta-encoded `SemanticTokens.data` payload for the part
  of the document inside the requested half-open LSP range
  `[params.range.start, params.range.end)`. It reuses the full-document AST walk
  of `textDocument/semanticTokens/full` and then filters by token start
  position. A token at `(line, character)` is kept if and only if its start
  position is `>= params.range.start` and `< params.range.end`. The kept tokens
  are then delta-encoded on their own. The running cursor is reset, so the first
  token's `deltaLine` / `deltaStart` are absolute. The server holds no
  per-document state, so every `range` request is independent of the others and
  of any earlier `full` request.

  New consumer-layer entrypoint
  `WorkspaceSession.semantic_tokens_range_for_file(path, start_line=0,
  start_character=0, end_line=None, end_character=0)` returns a tuple of
  `SemanticToken` dataclasses filtered to the same half-open range. Omit
  `end_line` to scan from the start position to the end of the file. Coordinates
  are 0-based (LSP-style). Files that fail to parse return `()`. Missing files
  raise `FileNotFoundError` from the consumer entrypoint, and the LSP handler
  converts that to `{"data": []}`. The new method composes
  `semantic_tokens_for_file`, so it inherits all of that walk's classification
  rules and limitations. Use-site classification covers only bare `ast.Name`
  lookups against the file's own `ModuleSymbolTable`. Attribute access,
  function-local shadowing, and cross-module re-export following are out of
  scope, matching the existing `find_references` / `inlayHint` limitations.

  The `full` and `range` LSP handlers share one
  `_encode_semantic_tokens(tokens)` helper. It produces the `[deltaLine,
  deltaStart, length, tokenType, tokenModifiers]` five-tuple wire encoding, with
  `tokenModifiers` as a bitmask over the legend positions, so the two endpoints
  encode equivalent tokens identically. `semanticTokens/full/delta` stays
  unimplemented by design. It is the only request shape that would need
  server-side per-document state, and re-sending the whole token stream on every
  change is fast enough to make that bookkeeping not worth its cost. The range
  feature uses only the stable `pyinc.integrations` public surface. The kernel
  contract and the integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/semanticTokens/full`. The server
  advertises `semanticTokensProvider: {legend: {tokenTypes: [...],
  tokenModifiers: [...]}, full: true, range: false}` and returns a delta-encoded
  `SemanticTokens.data` array for the requested document. The legend's
  `tokenTypes` list is `["namespace", "class", "function", "method",
  "parameter", "variable"]`, and `tokenModifiers` is `["declaration", "async"]`.
  The server parses the document (overlay or on-disk) once with `ast.parse` and
  walks the tree, emitting one token per:
  - `def` / `async def` header: token type `"function"` (or `"method"` when
    nested inside a `ClassDef` body), modifier `"declaration"` (plus `"async"`
    for `async def`). The name span is found on the def's header line with the
    same word-boundary scan that `textDocument/rename` uses, so a decorated
    definition still reports on its `def` line and skips the decorator line.
  - `class` header: token type `"class"`, modifier `"declaration"`.
  - Each function parameter (posonly / positional / vararg / kwonly / kwarg
    slot, in that order): token type `"parameter"`, modifier `"declaration"`.
    Parameter names are read from `ast.arg.col_offset`, which already points
    past any leading `*` / `**`.
  - Each bare `ast.Name` use (Load context) whose identifier matches a top-level
    entry in the file's `ModuleSymbolTable`. The token type follows the matched
    symbol's kind: `function`, `class`, `variable` / `class_variable` →
    `"variable"`, and `import_alias` → `"namespace"`. Dotted qualified-name
    entries (methods / nested classes) and `from_import_alias` /
    `wildcard_import_stub` entries are skipped in the use-site lookup by design,
    because resolving them to their real kind would need cross-module hops. The
    editor's default highlighting handles those names. Function-local shadowing
    is not modeled. A local `foo` inside a function that shadows a top-level
    `foo` is still tagged with the top-level kind, mirroring the documented
    `find_references` / `inlayHint` limitation.

  The walk recurses into decorator lists, default-value expressions, parameter
  annotations, return annotations, and base / keyword-argument class headers. So
  a workspace-resolved decorator (`@my_decorator`), default (`= my_default`), or
  base class (`class Derived(Base):`) all get the matching token kind. Files
  that fail to parse return `{"data": []}`. Missing files raise
  `FileNotFoundError` from the consumer entrypoint, and the LSP handler converts
  that to `{"data": []}`.

  The LSP handler encodes tokens into the LSP wire format. Each token
  contributes five integers `[deltaLine, deltaStart, length, tokenType,
  tokenModifiers]`. `deltaLine` is relative to the previous token's line.
  `deltaStart` is relative to the previous token's start column when both are on
  the same line, and absolute otherwise. `tokenModifiers` is a bitmask over the
  legend positions. New consumer-layer entrypoint
  `WorkspaceSession.semantic_tokens_for_file(path)` returns a tuple of
  `SemanticToken(line, character, length, token_type, token_modifiers)`
  dataclasses, with `line` / `character` 0-based (LSP-style). `token_type` is
  typed as `SemanticTokenType` (a `Literal` over the six legend names) and
  `token_modifiers` as `tuple[SemanticTokenModifier, ...]`. New public names
  re-exported from `pyinc_tools`: `SemanticToken`, `SemanticTokenType`,
  `SemanticTokenModifier`. It uses only the stable `pyinc.integrations` public
  surface (it composes `module_symbol_table`). The kernel contract and the
  integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/inlayHint`. The server advertises
  `inlayHintProvider: {resolveProvider: false}` and returns `InlayHint[]`
  parameter-name hints at call sites inside the requested LSP range. It walks
  the document's AST (overlay or on-disk) once with `ast.parse` and collects
  every `ast.Call` whose call-function span starts inside the requested range.
  Each callee is resolved through the same bare-`Name` / `Name.attr` resolver
  that `callHierarchy/outgoingCalls` uses (`_resolve_call_target`). The callee's
  signature is looked up through `_lookup_callable_signature`, so a class
  construction surfaces `<Class>.__init__`'s parameters with the leading `self`
  / `cls` stripped, as `signatureHelp` already does. Each positional argument is
  paired with the next positional parameter slot from `Signature.parameters`.
  The pairing walks posonly/positional entries, skips `**kwargs`, and stops at
  the first `*args` parameter, which absorbs the rest of the slots. Each pair
  emits an `InlayHint` with `label = "<paramname>:"`, `kind = "parameter"` (LSP
  value `2`), and `paddingRight = True`. A hint is suppressed when the argument
  is a bare `Name` whose identifier equals the parameter name, the standard
  no-redundant-hint convention of other Python language servers. Iteration also
  stops at the first `ast.Starred` argument in the call, because `*spread`
  consumes an unknown number of slots and the pairing is ambiguous after it.
  Targets resolved as stdlib / installed / ambiguous / missing return `[]`. So
  do calls whose callee shape is not a bare `Name` or `Name.attr` (subscripted
  calls `factory[T](...)`, deep attribute chains `pkg.subpkg.foo(...)`,
  `self.method(...)` / instance-attribute calls, lambdas), and files that fail
  to parse. New consumer-layer entrypoint
  `WorkspaceSession.inlay_hints_for_file(path, start_line=0, start_character=0,
  end_line=None, end_character=0)` returns a tuple of `InlayHint(line,
  character, label, kind, padding_left, padding_right)` dataclasses, with `line`
  / `character` 0-based (LSP-style) and `kind` typed as
  `Literal["parameter", "type"]`. This release emits only `"parameter"`, and
  `"type"` is reserved for future variable-type / return-type hints. Omit
  `end_line` to scan the whole file. New public names re-exported from
  `pyinc_tools`: `InlayHint`, `InlayHintKind`. It uses only the stable
  `pyinc.integrations` public surface (it composes `resolve_symbol` and
  `module_symbol_table` through the existing call-target resolver and signature
  lookup). The kernel contract and the integration-layer surface are unchanged.
- The `pyinc-tools` LSP `serverInfo.version` is bumped from `"2.0.0"` to
  `"2.1.0"` to match the kernel version pinned in `pyproject.toml`.
- The `pyinc-tools` LSP supports call hierarchy. The server advertises
  `callHierarchyProvider: true` and implements all three call-hierarchy methods:
  `textDocument/prepareCallHierarchy`, `callHierarchy/incomingCalls`, and
  `callHierarchy/outgoingCalls`. `prepareCallHierarchy` resolves the identifier
  under the cursor through `symbol_resolution.resolve_symbol`. When the target
  is a workspace `function`, `method`, or `class`, it returns a single
  `CallHierarchyItem`. The item's `range` covers the whole def block (including
  decorator lines, if any), and its `selectionRange` is the bare-name span on
  the header line. Its `data` field carries `{"path", "qualified_name"}`, so the
  incoming and outgoing follow-up calls skip re-resolving the cursor. Variables,
  import aliases, `from_import` aliases, wildcard-import stubs, and stdlib /
  installed / ambiguous / missing targets return `null`. `incomingCalls` runs
  `find_references(include_declaration=False)` on the item's target and groups
  references by their innermost enclosing workspace-known def or class in the
  same file. The qualifier follows `module_symbol_table`'s ClassDef-only
  nesting, so a reference inside `class C: def m(self): ...` is attributed to
  `C.m`. References inside a nested function body bubble up to the next
  enclosing function or class method in the symbol table. Module-top-level
  references are dropped, because they have no caller item to be attributed to.
  `outgoingCalls` parses the declaring file, finds the `def` / `async def` /
  `class` matching the item's qualified name, and walks its body for `ast.Call`
  nodes. It stays out of nested `FunctionDef` / `AsyncFunctionDef` / `ClassDef`
  / `Lambda` scopes, each of which owns its own outgoing-call list. Bare
  `Name(id=name)` calls are resolved against the declaring module's imports.
  `Name.attr` calls are resolved by looking up the LHS as a workspace module and
  then resolving `attr` inside that module, mirroring `find_references`'s
  LHS-bare-Name handling. Subscripted calls (`factory[T](...)`), deep attribute
  chains (`pkg.subpkg.foo(...)`), `self.method(...)` / instance-attribute calls,
  and lambda calls produce no callee. New consumer-layer entrypoints
  `WorkspaceSession.prepare_call_hierarchy(path, line, character)`,
  `WorkspaceSession.call_hierarchy_incoming_calls(path, qualified_name)`, and
  `WorkspaceSession.call_hierarchy_outgoing_calls(path, qualified_name)` return
  tuples of `CallHierarchyItem`, `CallHierarchyIncomingCall(caller,
  call_sites)`, and `CallHierarchyOutgoingCall(callee, call_sites)` dataclasses
  with 0-based LSP-style range fields. New public names re-exported from
  `pyinc_tools`: `CallHierarchyItem`, `CallHierarchyItemKind`,
  `CallHierarchyCallSite`, `CallHierarchyIncomingCall`,
  `CallHierarchyOutgoingCall`. It uses only the stable `pyinc.integrations`
  public surface (it composes `resolve_symbol`, `module_symbol_table`, and
  `find_references`). The kernel contract and the integration-layer surface are
  unchanged.
- The `pyinc-tools` LSP handles `textDocument/typeDefinition`. The server
  advertises `typeDefinitionProvider: true` and returns `Location[]` for the
  type-definition sites of the symbol under the cursor. It resolves the cursor's
  identifier to its declaring `Symbol` through the existing `resolve_symbol`
  pipeline, so the user can stand on either the declaration site or a same-name
  use site inside the declaring module. It reads the declared annotation
  (variable / class-variable `annotation`, or function / method
  `signature.return_annotation`), parses it as a Python expression, and walks
  the result for `Name` and `Attribute(value=Name(...), attr=...)` nodes. Each
  name is resolved against the declaring module. Bare `Name` references go
  through that module's imports. `lhs.attr` references first resolve `lhs` to a
  workspace module and then resolve `attr` inside that module. So generics
  (`list[Foo]`), unions (`Foo | Bar`), and qualified attribute types (`pkg.Foo`,
  `helper.Foo | helper.Bar`) all yield one location per workspace-resolved type,
  deduplicated by `(path, lineno)`. Whole-string forward references (`x: "Foo"`,
  `def f() -> "Foo"`) are unwrapped once before the walk. Partial string
  annotations (`x: "Foo" | None`) stay wrapped, and the string portion
  contributes no location. A class is its own type, so clicking on a class name
  returns its own definition location. Stdlib / installed / ambiguous type names
  (`int`, `list`, `typing.Optional`, and so on) are skipped through the existing
  resolver classification. Import aliases, `from_import` aliases,
  wildcard-import stubs, unannotated variables and functions, and non-workspace
  targets return `[]`. Attribute chains whose LHS is not a bare `Name`
  (`pkg.subpkg.Foo`) are skipped, mirroring the resolver's existing limitation
  for references. New consumer-layer entrypoint
  `WorkspaceSession.type_definitions_at(path, qualified_name)` returns a tuple
  of `TypeDefinitionLocation(path, lineno, col_offset, end_col_offset)`
  dataclasses. `lineno` is the 1-based AST lineno (the LSP layer subtracts 1),
  and `(col_offset, end_col_offset) = (0, 1)`, matching the existing
  `textDocument/definition` shape. New public name re-exported from
  `pyinc_tools`: `TypeDefinitionLocation`. It uses only the stable
  `pyinc.integrations` public surface (`resolve_symbol`, `module_symbol_table`).
  The kernel contract and the integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/codeLens`. The server advertises
  `codeLensProvider: {resolveProvider: false}` and returns one reference-count
  `CodeLens` above every top-level `def` / `async def` / `class` in the
  requested document. It covers each top-level symbol of kind `function` or
  `class`. Dotted-name nested classes and methods are excluded, because
  `find_references` does not reliably resolve attribute calls on instances. For
  each symbol, the server finds the bare-name identifier range on the
  definition's header line with the same `_locate_def_class_name_offsets` helper
  that `find_document_highlights` uses. It then calls `find_references` with
  `include_declaration=False` to count the workspace references, and emits a
  `CodeLens` whose `command` is `{title: "<N> reference[s]", command: ""}`. The
  lens has no clickable action, following the convention of other Python LSP
  servers, so its text appears above the definition without binding to an
  editor-specific command. Non-workspace targets, unparseable files, and files
  with no qualifying symbols return `[]`, the way other LSP requests degrade.
  Decorated definitions report the lens on the `def` line and skip the decorator
  line. New consumer-layer entrypoint
  `WorkspaceSession.code_lenses_for_file(path)` returns a tuple of
  `CodeLens(start_line, start_character, end_line, end_character, title)`
  dataclasses with all four position fields 0-based (LSP-style). New public name
  re-exported from `pyinc_tools`: `CodeLens`. It uses only the stable
  `pyinc.integrations` public surface (it composes `module_symbol_table` and
  `find_references`). The kernel contract and the integration-layer surface are
  unchanged.
- The `pyinc-tools` LSP handles `textDocument/documentLink`. The server
  advertises `documentLinkProvider: {resolveProvider: false}` and returns
  `DocumentLink[]` for the requested document. It walks the document's AST
  (overlay or on-disk) and pairs every `ast.alias` whose enclosing `Import` /
  `ImportFrom` resolves to a workspace file with a link spanning the alias's AST
  `(col_offset, end_col_offset)` range. For `import M` and `import M as alias`,
  the link covers the whole `M [as alias]` clause and points at the resolved
  module file. For `from M import a, b`, each imported name links on its own to
  its own resolved path. For a submodule (`from pkg import child`), that path is
  the submodule file in place of `pkg/__init__.py`. Stdlib, installed, missing,
  ambiguous, and wildcard (`from M import *`) targets emit no link, matching the
  LSP's existing scope of navigating only to workspace-resolved targets. Files
  that fail to parse return `[]`, the way other LSP requests degrade on syntax
  errors. Imports inside `if TYPE_CHECKING:` / `try: ... except ImportError:`
  guard blocks are linked, since `resolved_imports_for_file` walks into both.
  New consumer-layer entrypoint `WorkspaceSession.document_links_for_file(path)`
  returns a tuple of `DocumentLink(start_line, start_character, end_line,
  end_character, target_path)` dataclasses, with all four position fields
  0-based (LSP-style) and `target_path` already remapped from the mirror root to
  the real workspace root. New public name re-exported from `pyinc_tools`:
  `DocumentLink`. It uses only the stable `pyinc.integrations` surface. The
  kernel contract and the integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/selectionRange`. The server
  advertises `selectionRangeProvider: true` and returns one `SelectionRange`
  chain per requested position, innermost first through the recursive `parent`
  field. The server parses the document (overlay or on-disk) once with
  `ast.parse` and collects every AST node whose
  `(lineno, col_offset)`–`(end_lineno, end_col_offset)` span contains the
  cursor. It removes duplicate spans and reduces the candidates to a strict
  containment chain ordered by length, so each parent is strictly larger than
  its child. The cursor offset is computed against a precomputed table of line
  starts, so multi-line spans (function bodies, class bodies, multi-statement
  blocks) map correctly. Files that fail to parse, positions outside the source,
  and positions that no AST node covers all fall back to a single zero-width
  range at the cursor. So the LSP result length always matches the
  `params.positions` length. New consumer-layer entrypoint
  `WorkspaceSession.selection_ranges_at(path, line, character)` returns a flat
  tuple of `SelectionRange(start_line, start_character, end_line,
  end_character)` dataclasses with all four fields 0-based (LSP-style). The LSP
  handler threads that flat tuple into the recursive `parent` shape. New public
  name re-exported from `pyinc_tools`: `SelectionRange`. It uses only the stable
  `pyinc.integrations` surface. The kernel contract and the integration-layer
  surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/foldingRange`. The server
  advertises `foldingRangeProvider: true` and returns `FoldingRange[]` for the
  requested document. It parses the file's source (overlay or on-disk) once with
  `ast.parse` and walks the tree for foldable spans. Every `def` / `async def` /
  `class` block becomes a `region` fold. Its `startLine` is the header line (or
  the first decorator line, if decorators are attached), and its `endLine` is
  the AST `end_lineno`. The walk recurses into class bodies, so methods fold
  independently of their enclosing class. Runs of consecutive top-level `import`
  / `from … import` statements are coalesced into one `imports` fold from the
  first to the last line of the run. Multi-line parenthesised imports
  (`from x import (\n    a,\n    b,\n)`) collapse on their own. Single-line
  definitions and single-line single imports emit no fold, since a one-line fold
  does nothing in the editor. Files that fail to parse return `[]`, the way
  other LSP requests degrade on syntax errors. The LSP `kind` field is omitted
  for generic `region` folds and emitted as `"imports"` for the import-group
  case, so older clients that only recognise `"imports"` / `"comment"` still
  work. New consumer-layer entrypoint
  `WorkspaceSession.folding_ranges_for_file(path)` returns a tuple of
  `FoldingRange(start_line, end_line, kind)` dataclasses with `kind` typed as
  `Literal["imports", "comment", "region"]`. They use 1-based AST linenos, so
  the shape matches sibling entrypoints like `find_document_highlights`, and the
  LSP layer subtracts 1 to produce the LSP 0-based `startLine` / `endLine`. New
  public names re-exported from `pyinc_tools`: `FoldingRange`,
  `FoldingRangeKind`. It uses only the stable `pyinc.integrations` surface. The
  kernel contract and the integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/signatureHelp`. The server
  advertises `signatureHelpProvider: {triggerCharacters: ["(", ","],
  retriggerCharacters: [","]}` and returns a `SignatureHelp` payload for the
  call expression enclosing the cursor. A forward source scanner skips comments
  and string literals (single, double, and triple-quoted) and tracks a stack of
  open brackets. The topmost open `(` whose preceding token is a usable
  identifier names the function being called, and the accumulated comma count
  gives `activeParameter`. `def name(` and `class Name(` definition headers and
  a `(` after a Python keyword are rejected, so only call sites qualify. The
  identifier is resolved through the existing `symbol_resolution.resolve_symbol`
  pipeline, so cross-module re-exports hop through. Only workspace-resolved
  targets produce a signature. Functions surface their declared `Signature`
  directly. Classes surface `<Class>.__init__`'s signature with a leading
  `self`/`cls` parameter stripped, or an empty constructor signature when no
  `__init__` is defined. Stdlib/installed/ambiguous targets, attribute calls
  (`obj.method(`), subscripted calls (`factory[T](`), and same-file calls whose
  enclosing `(` is still unclosed (which makes the file unparseable for symbol
  extraction) all return `null`. Each signature reports parameters as LSP
  `[start, end]` substring offsets into the signature label, so editors can
  highlight the active parameter. New consumer-layer entrypoint
  `WorkspaceSession.signature_help_at(path, line, character)` returns a
  `SignatureHelp(label, parameters, active_parameter)` dataclass with
  `parameters` typed as `tuple[SignatureParameterInfo, ...]`. New public names
  re-exported from `pyinc_tools`: `SignatureHelp`, `SignatureParameterInfo`. It
  uses only the stable `pyinc.integrations` public surface. The kernel contract
  and the integration-layer surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/documentHighlight`. The server
  advertises `documentHighlightProvider: true` and returns `DocumentHighlight[]`
  ranges for the symbol under the cursor, scoped to the current file. The
  declaration site is reported with `kind: 3` (Write), and all other occurrences
  with `kind: 1` (Text). `find_references` emits a synthetic
  `(col=0, end_col=1)` placeholder for `def` / `class` / `async def` declaration
  lines. The server repairs it by finding the real identifier offset on the line
  (the same repair `textDocument/rename` uses), so editors highlight the
  identifier itself. Cross-file references that `find_references` would return
  are filtered out by design, because workspace-wide highlighting is the job of
  `textDocument/references`. Stdlib / installed / ambiguous targets return `[]`.
  New consumer-layer entrypoint `WorkspaceSession.find_document_highlights(path,
  qualified_name)` returns a tuple of `DocumentHighlight(lineno, col_offset,
  end_col_offset, kind)` dataclasses with `kind` typed as
  `Literal["text", "read", "write"]`. New public names re-exported from
  `pyinc_tools`: `DocumentHighlight`, `DocumentHighlightKind`. It uses only the
  stable `pyinc.integrations.find_references` entrypoint. The kernel contract
  and the integration-layer surface are unchanged.
- `find_references` and rename follow `import M; M.foo()` attribute access. The
  resolver used to be strictly name-local, so attribute access on an `import`
  binding (`import a; a.foo()`, `import a as alias; alias.foo()`) returned no
  references, and rename left the call site unchanged.
  `docs/pyinc-tools-guide.md` documented both limitations, and a regression test
  pinned them. The occurrence walker in
  `symbol_resolution._collect_name_occurrences` now carries the LHS Name's `id`
  as an internal verification hint on every
  `Attribute(value=Name(...), attr=...)` occurrence. The hint is a 5th element
  added to the internal `NameOccurrencePayload` and is not part of the public
  surface. `find_references_payload` routes hint-bearing occurrences through a
  two-step verification. It resolves the LHS through its `import_alias` /
  `from_import_alias` to a workspace module, and then resolves the attribute
  inside that module, so cross-module re-exports (`from c import foo`) hop
  through. Only the rightmost-attribute span is reported, so rename rewrites the
  attribute portion and leaves the leading `M.` / `alias.` intact. The same hint
  flows out of the forward-reference string-annotation walker, so
  `def g(x: 'a.Foo')` is covered too. Attribute access whose LHS is itself an
  Attribute (such as `import pkg.subpkg; pkg.subpkg.foo()`) is still not
  counted, which remains a documented limitation. No kernel contract change, and
  the integration public surface is unchanged.

### Fixed

- `FileDeletionEdit` is re-exported from `pyinc_tools`. The previous PR added
  the dataclass to `pyinc_tools.session` alongside
  `WorkspaceSession.import_edits_for_file_deletions`, but left it out of the
  re-export list in `pyinc_tools/__init__.py`. Consumers who imported it from
  the top-level package (the precedent set by `FileRenameEdit` and every other
  consumer-layer dataclass) got an `ImportError`. The symbol is now in both the
  module-level imports and `__all__`.

## [2.1.0] - 2026-05-05

### Added

- Rename rewrites relative `from … import` lines. The
  `WorkspaceSession.rename_symbol` import-edit walker resolves
  `from .pkg import name`, `from .. import name`, and
  `from ..sub.pkg import name` forms against each importer's package. It
  rewrites them when the resolved absolute module matches
  `target.defining_module`. The `as <alias>` clause is preserved, as in the
  absolute-import case. Module-level (`__init__.py`) importers are anchored on
  the package itself, and non-package modules on their parent. This resolves the
  documented v2.0.x rename limitation that relative imports were not rewritten.
  The change is `pyinc_tools`-only, and the kernel and the `pyinc.integrations`
  public surface are unchanged.
- The `pyinc-tools` LSP handles `textDocument/rename` and
  `textDocument/prepareRename`. The server advertises
  `renameProvider: {prepareProvider: true}`. `prepareRename` returns the range
  of the identifier under the cursor and a placeholder when the symbol resolves
  to a workspace target, and `null` otherwise. `rename` returns a
  `WorkspaceEdit` with `changes` keyed by document URI. Edits cover (a) every
  `Name` / `Attribute` occurrence already produced by
  `symbol_resolution.find_references`, (b) the `def`/`class`/`async def`
  declaration site, and (c) every
  `from <defining_module> import <bare_old> [as <alias>]` line in the workspace.
  At the declaration site, the `find_references` synthetic placeholder is
  repaired by finding the identifier offset in the source line. In an import
  line, only the source-name part is rewritten, so any `as <alias>` clause is
  preserved. Invalid identifiers (`"1bad"`, `""`) and Python keywords
  (`"class"`, `"return"`) yield a JSON-RPC `RequestFailed` (-32803) error with a
  human-readable message. Renaming a symbol through an `import ... as` alias
  (such as clicking on `aliased` in `from a import foo as aliased`) is refused
  with a `RequestFailed` error that tells the user to rename the canonical name.
  Same-name and non-workspace targets return `null`. The consumer-layer
  entrypoint `WorkspaceSession.rename_symbol(path, qualified_name, new_name)`
  returns a structured `RenameResult(target, edits, status)`. It carries the
  target's `ResolvedSymbol`, a tuple of `RenameEdit(path, lineno, col_offset,
  end_col_offset, new_text)`, and one of the statuses `"ok"`,
  `"non_workspace_target"`, `"invalid_identifier"`, `"keyword_identifier"`,
  `"same_name"`, or `"alias_rename_unsupported"`. New public names re-exported
  from `pyinc_tools`: `RenameEdit`, `RenameResult`, `RenameStatus`. It uses only
  the stable `pyinc.integrations` surface, with no kernel contract change.

## [2.0.1] - 2026-04-29

### Added

- `find_references` scans forward-reference string annotations.
  `symbol_resolution.find_references`, and the LSP `textDocument/references` it
  backs, detect names inside forward-reference strings such as
  `def g(a: 'Foo')`, `x: 'list[Foo]'`, `x: 'pkg.Foo'`, and `'Foo | None'`.
  `name_occurrences_for_file` runs a second pass over the annotation slots
  `AnnAssign.annotation`, `arg.annotation`, and
  `FunctionDef`/`AsyncFunctionDef.returns`. It re-parses string-valued
  `ast.Constant` nodes with `ast.parse(value, mode="eval")` and emits the inner
  `Name`/`Attribute` references, with offsets translated back to file
  coordinates. Each new occurrence goes through the same
  `resolve_symbol_payload` verification as bare `Name`/`Attribute` references.
  So workspace-only filtering, `MAX_FOLLOW_DEPTH`, and `if TYPE_CHECKING:` /
  `try: except ImportError:` guard handling all carry through unchanged. String
  annotations that span multiple lines, are triple-quoted, contain escape
  sequences, or use implicit string concatenation are skipped, because offset
  reconstruction would be ambiguous. Malformed annotation strings are silently
  ignored. No payload shape or public surface change.

## [2.0.0] - 2026-04-25

v1.2.1 was the last v1 release. This release resolves the items listed under
"Version 1 did not include" in `docs/architecture.md`, except *schedulers or
worker pools*, which stay deferred.

### Added

- New stable integration `pyinc.integrations.notebook` for Jupyter `.ipynb`
  files exposes `notebook_analysis(db, path)` and
  `workspace_notebook_analysis(db, root)`, plus the dataclasses
  `NotebookAnalysis`, `NotebookCell`, and `NotebookDiagnostic`. Code cells'
  Python source is concatenated and parsed with `ast` to surface module-level
  imports and definitions per cell. Cutoff-based backdating works on the parsed
  structure, so whitespace-only and output-only edits are backdated and leave
  downstream consumers valid. Markdown and raw cells are kept with their
  first-line heading (markdown) or kind tag. It is stdlib-only and decodes the
  notebook's JSON with `json`, with no `nbformat` dependency. This resolves the
  v1 architectural non-goal "notebook integration".
- The kernel supports push observers. New `Database.observe(callback, query,
  *args, **kwargs) -> Subscription` registers a callback that fires when the
  identified query node's stored value changes (decision `"executed"`).
  Backdated and reused decisions leave the stored value in place, so they fire
  nothing. Events are delivered as `QueryChangeEvent` frozen dataclasses
  carrying `query_id`, `args_digest`, `decision`, `changed_at`, and
  `verified_at`. Dispatch runs after the outermost request scope completes and
  the kernel lock is released, so a callback may safely call back into the
  database. Callback-level exceptions go to an optional
  `Database(observer_error_hook=...)` hook (default: a one-line stderr log), and
  sibling callbacks still run. `Subscription.unsubscribe()` detaches a callback
  and is idempotent. New public names re-exported from `pyinc`:
  `QueryChangeEvent`, `Subscription`, `ObserverCallback`, `ObserverErrorHook`.
  This resolves the v1 architectural non-goal "push observers in the kernel".
- Mutable object graphs cross cached boundaries. `freeze` / `thaw` memoize
  shared object identity and rebuild cyclic structures through two new snapshot
  variants: `FrozenGraph(nodes, root)`, which wraps the graph, and
  `FrozenRef(index)`, which points into it. The boundary used to raise
  `UnsupportedValueError("Cyclic values cannot cross cached boundaries.")` and
  silently dropped shared identity. Pure-tree inputs still produce the v1 flat
  snapshot shape (zero overhead in the common case), and only inputs with real
  sharing or cycles are wrapped in `FrozenGraph`. `thaw` runs a two-pass
  allocate-then-fill, so a list containing itself round-trips to a
  self-referential list, and shared sub-objects keep their identity. This
  resolves the v1 architectural non-goal "arbitrary mutable object graphs across
  cached boundaries". New public names re-exported from `pyinc`: `FrozenGraph`,
  `FrozenRef`.
- The kernel gains content-addressed artifact storage. The new `ArtifactStore`
  Protocol has two shipped implementations: `InMemoryArtifactStore`
  (dict-backed) and `FileSystemArtifactStore` (git-style two-character fan-out
  under `<root>/objects/<digest[:2]>/<digest[2:]>`, with atomic
  `tempfile`+`os.replace` writes). `Database(store=...)` writes the serialized
  snapshot bytes for every value crossing the membrane, keyed by the
  `fingerprint_snapshot` digest. New `serialize_snapshot(snapshot)` and
  `deserialize_snapshot(payload)` helpers expose the byte form to external
  callers, and both round-trip the full snapshot grammar, including
  `FrozenGraph` / `FrozenRef`. The durable checkpoint API delivers cross-run
  cache reuse. `Database.save_checkpoint(store=None) -> str` serialises all
  current query and resource node records (plus their dependency edges and
  snapshot bytes) to an `ArtifactStore`, and returns a content-addressed
  checkpoint key prefixed with `"ck"`. A later `Database.load_checkpoint(key,
  store=None)` in a fresh process reads the manifest back and verifies that all
  declared input digests and resource probe hints still match. It then pre-warms
  the node record cache, so the next `db.get(query)` reuses the stored result
  without re-executing the query function. If any dependency is stale, the
  affected query is re-executed and the new result is compared against the
  stored snapshot for backdating, which maintains from-scratch consistency. Both
  methods accept an optional `store=` kwarg for call-site store injection.
  `save_checkpoint` also writes all referenced snapshot bytes to the store,
  which makes it self-contained. The checkpoint key is content-addressed, so
  identical database state always produces the same key. New public names
  re-exported from `pyinc`: `ArtifactStore`, `InMemoryArtifactStore`,
  `FileSystemArtifactStore`, `serialize_snapshot`, `deserialize_snapshot`. This
  resolves the v1 architectural non-goal "content-addressed artifact storage".
- `symbol_resolution` supports `try/except ImportError` imports. It recognises
  `try: … except ImportError:` and `try: … except ModuleNotFoundError:` guard
  blocks (and the tuple form `except (ImportError, ModuleNotFoundError):`) at
  the module top level, and walks their bodies for `import` and `from … import`
  statements. The collected symbols appear in `ModuleSymbolTable.symbols` with
  the existing `import_alias` / `from_import_alias` kinds, as if the imports
  were unconditional. The "conditional top-level binding" impurity marker is
  left off files whose only conditional blocks are recognised import-error
  guards. `python_source` also collects import statements and bound names from
  such blocks, so `import_statements_for_file` and the module binding analysis
  agree with the symbol table. Bare `except:` handlers, and handlers for other
  exception types, still set the impurity marker.
- The kernel digest format is bumped to `K2;`. The `fingerprint_snapshot`
  encoder prefixes its byte form with `K2;`, so older `K1;` and unprefixed
  payloads in any external durable cache are never silently accepted. In-memory
  state across a process restart is unaffected. This is the standard path,
  documented in `docs/kernel-contract.md`, under which an encoder change
  requires an identity bump.

### Changed

- The value boundary keeps shared identity. When the same mutable container
  appears at two slots of an input value, both reads from the thawed copy in
  `checked` / `fast` mode now refer to the same Python object. They used to be
  two independent copies. This is consistent with the new mutable graph support.
  The kernel's stored snapshot remains immutable and safe, and the mode table
  (strict / checked / fast) is unchanged. Tests that asserted v1's silent
  identity-drop behaviour were split. The v1-shaped independent-inputs test
  still verifies that two separately constructed dicts thaw independently, and a
  new companion test covers the v2 shared-input case.
- `docs/kernel-contract.md` limitation #4 is amended to describe the outbound
  `ArtifactStore` and the durable `save_checkpoint` / `load_checkpoint` flow.
- The `pyinc-tools` LSP `serverInfo.version` is bumped from `"1.2.0"` to
  `"2.0.0"` to match the kernel.

### Documentation

- Updated `docs/integration-authoring.md` line citations into `python_source.py`
  to the current line numbers.
- Removed the phantom v1.3.0 reference in `docs/pyinc-tools-guide.md`. The
  features described there shipped across v1.2.0 and v1.2.1 and continue in
  v2.0.0.
- Fixed `docs/pyinc-tools-guide.md` to list `try: … except ImportError:` guard
  blocks under "Supported", since this release added them to the
  `symbol_resolution` walker. They are removed from the "Not supported"
  conditional-blocks bullet, which now names only `if sys.version_info >= …`
  style guards.
- Updated the `docs/architecture.md` "Scope" section to replace the crossed-out
  development-cycle tracking list with a clean summary of what v2.0.0 resolved.
- Added `examples/checkpoint_demo.py`, which shows `save_checkpoint` /
  `load_checkpoint` cross-run cache reuse with `FileSystemArtifactStore`. Three
  simulated runs show cold execution, full checkpoint reuse, and partial reuse
  when one input changes.

## [1.2.1] - 2026-04-24

### Added

- `symbol_resolution` supports `if TYPE_CHECKING:` imports. It recognises
  `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:` guard blocks at the module
  top level and walks their bodies for `import` and `from … import` statements.
  The collected symbols appear in `ModuleSymbolTable.symbols` with the existing
  `import_alias` / `from_import_alias` kinds, as if the imports were
  unconditional. As a result, LSP hover and goto-definition work for names
  referenced as bare identifiers (such as `x: Foo`), even when the binding lives
  under a `TYPE_CHECKING` guard. The "conditional top-level binding" impurity
  marker is left off files whose only conditional blocks are `TYPE_CHECKING`
  guards. Other conditional blocks (such as `if sys.version_info >= …`) still
  set the marker. Non-import statements inside a `TYPE_CHECKING` block (unusual)
  are skipped and kept out of the symbol table.

### Notes

- The kernel contract (`src/pyinc`) is unchanged. The minor version bump
  reflects new behaviour in the `symbol_resolution` integration, which is part
  of the stable `pyinc.integrations` public surface.
- One `find_references` limitation remains. The AST name-occurrence walk skips
  forward-reference strings (`'Foo'` in annotations), so reference results leave
  out string-annotation usages.

## [1.2.0] - 2026-04-22

### Added

- `pyinc-tools lsp` handles `textDocument/references`. It advertises
  `referencesProvider` and honors `context.includeDeclaration`. References carry
  per-occurrence character ranges, with `col_offset` / `end_col_offset` from the
  AST in place of the line-0 placeholder some other requests use, so editors can
  highlight every match.
- New stable entrypoint `pyinc.integrations.find_references`, with `Reference`
  and `ReferenceQueryResult` dataclasses. Two new composition-layer `@query`
  functions in `symbol_resolution` back it: `name_occurrences_for_file`
  (full-AST `Name`/`Attribute` walk) and `workspace_name_occurrence_index`. A
  bare-name pre-filter bounds candidate filtering. Each surviving candidate is
  verified through `resolve_symbol_payload`, so results respect the existing
  `MAX_FOLLOW_DEPTH = 8` cross-module re-export semantics. Only
  workspace-resolved targets are indexed. `stdlib`/`installed`/`ambiguous`
  targets return an empty tuple, with the `ResolvedSymbol` carried on the
  result.
- `WorkspaceSession.find_references` is a mirror-path aware wrapper around the
  integration entrypoint. Paths in the returned `Reference` tuples are remapped
  to the real workspace root.
- `PollingWorkspaceWatcher` polls live on a thread.
  `PollingWorkspaceWatcher.start(on_change, *, interval_s, on_error)` starts a
  daemon thread that delivers debounced change batches to a caller-supplied
  callback, and `stop(timeout=5.0)` joins the thread cleanly. Context-manager
  support (`with watcher: ...`) guarantees `stop()` on exit. Exceptions from
  `on_change` go to the optional `on_error` hook, or are logged to stderr by
  default, and the watcher thread keeps running. `poll()` remains available for
  synchronous use, but raises `RuntimeError` while the thread is running (one
  driver at a time).
- `pyinc-tools lsp` polls live. It starts a threaded `PollingWorkspaceWatcher`
  in `initialize` by default, so external file changes (such as `git pull` or
  formatter scripts) publish fresh diagnostics. This works without
  `workspace/didChangeWatchedFiles` from the editor. Opt out with
  `initializationOptions.pyinc.watcher.enabled=false`, and tune with
  `pyinc.watcher.debounceMs` and `pyinc.watcher.intervalMs`. A diagnostic-tuple
  signature cache suppresses repeated `publishDiagnostics` for an unchanged URI.
- New CLI flag `--poll-interval-ms` gives explicit control over the watcher poll
  cadence. `pyinc-tools analyze --watch` drives its loop through the threaded
  watcher API, with unchanged behaviour.

### Changed

- `WorkspaceSession` is thread-safe for its own public surface. A session-level
  `threading.RLock` guards `set_overlay`, `clear_overlay`, `refresh_paths`,
  `analyze_file`, `analyze_workspace`, `resolve_symbol_reference`, and
  `find_references`. Mutators raise `RuntimeError` once `close()` has been
  called, so the watcher thread exits cleanly when the session shuts down. The
  kernel's existing `Database` `RLock` is unchanged.

### Notes

- The kernel contract (`src/pyinc`) is unchanged. The minor version bump
  reflects new public consumer-layer API surface only. Watcher loops and LSP
  wiring remain architectural non-goals for the kernel itself. All new code
  lives in `pyinc_tools`, on top of stable `pyinc.integrations` entrypoints.
- Known limitations for `find_references` in v1.2.0:
  - References via attribute access to a module-level symbol imported only as a
    module (`import a; a.foo()`) are not counted, because the resolver is
    name-local. Use `from a import foo` to opt in.
  - Forward-reference strings (`'Foo'` in annotations) are not scanned.
  - Function-local shadowing is not modeled. A local `foo = 1` inside a function
    is still reported as a reference to a module-level `foo`.
    `symbol_resolution` is module/class-scope only, per
    `docs/integration-contract.md`.

## [1.1.1] - 2026-04-22

### Added

- New consumer-facing guide `docs/pyinc-tools-guide.md`. It covers install,
  `pyinc-tools analyze` (one-shot and `--watch`), `pyinc-tools lsp` (stdio and
  advertised capabilities), editor wiring (Neovim, Emacs/eglot, a VS Code note),
  the `WorkspaceSession` overlay model, a supported-vs.-not-yet table, and
  troubleshooting. `README.md` links to it.
- LSP hardening tests cover single-level wildcard goto-def, the
  `MAX_FOLLOW_DEPTH = 8` boundary, cyclic re-exports returning `ambiguous`,
  ambiguous wildcard lookups, the full eight-kind `documentSymbol` surface, and
  the current `if TYPE_CHECKING:` limitation.

### Notes

- The kernel contract (`src/pyinc`) is unchanged. This patch-level release
  changes docs and test coverage only.

## [1.1.0] - 2026-04-21

### Added

- `pyinc-tools lsp` handles hover and goto-definition, and advertises
  `hoverProvider` and `definitionProvider`. Hover returns a Markdown signature
  for the symbol under the cursor (functions with parameters and return
  annotation, classes, annotated variables, re-exported aliases).
  Goto-definition follows cross-module re-exports through
  `symbol_resolution.resolve_symbol` and returns a `Location` in the defining
  module.
- `WorkspaceSession` gains two methods. `resolve_symbol_reference(path,
  qualified_name)` wraps `resolve_symbol` with mirror-root → real-root path
  remapping. `source_text(path)` returns the active overlay or on-disk contents
  for a tracked file.

### Notes

- The kernel contract (`src/pyinc`) is unchanged, and the minor version bump
  reflects new public API on the `pyinc_tools` consumer layer. LSP wiring and
  push-based watchers remain architectural non-goals for the kernel itself. They
  live in `pyinc_tools`, on top of stable `pyinc.integrations` entrypoints.

## [1.0.1] - 2026-04-21

### Added

- New consumer tooling layer `pyinc_tools`, with a mirror-workspace
  `WorkspaceSession`, polling/debounce watcher support, `pyinc-tools analyze`,
  and `pyinc-tools lsp`. It all lives outside `src/pyinc`, so the kernel
  contract stays stable.
- Focused diagnostics and escape-hatch examples for `inspect_fresh(...)`,
  `explain_query_captures(...)`, and `report_untracked_read(...)`.

### Changed

- Reconciled the stable v1.x release story across `AGENTS.md`, `README.md`,
  `docs/architecture.md`, and `docs/integration-contract.md`.
- Unsupported ambient-capture failures point users to
  `pyinc.explain_query_captures(...)` for preflight inspection.

## [1.0.0] - 2026-04-18

The first stable v1 release.

### Added

- The kernel: pull-based red-green verification, backdating (early cutoff),
  `strict` / `checked` / `fast` value-membrane modes, LRU eviction, cycle
  detection, untracked-read guards, `Database.set_many(...)` batch invalidation,
  `Database.dependency_graph(...)` export, `Database.inspect(...)` /
  `Database.explain(...)` provenance, and `Database.statistics()` /
  `Database.query_profile()` observability.
- Built-in resources: `FileResource`, `FileStatResource`, `EnvResource`, and
  `DirectoryResource`.
- Twelve shipped integrations under `pyinc.integrations`: `python_source`,
  `toml_config`, `requirements_txt` (including `deep_requirements_analysis` for
  recursive `-r` following), `installed_packages`, `json_config`,
  `dependency_check`, `env_file`, `xml_config`, `csv_data`,
  `deep_module_resolution`, `requirement_evaluation` (PEP 440 specifier
  satisfaction and PEP 508 marker evaluation), and `symbol_resolution` (module-
  and class-level symbol tables with bounded cross-module re-export resolution).
- The package ships an inline `py.typed` marker and is `mypy --strict` clean.
- Documentation: `kernel-contract.md`, `integration-contract.md`,
  `integration-authoring.md`, and `architecture.md`.

### Notes

- Zero runtime dependencies. The package is pure Python and stdlib-only.
- Tested on CPython 3.11, 3.12, and 3.13.
- LSP wiring and push-based filesystem watchers are architectural non-goals for
  v1. `docs/architecture.md` describes the scope boundary.

[1.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.0.0
[1.0.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.0.1
[1.1.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.1.0
[1.1.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.1.1
[1.2.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.2.0
[1.2.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.2.1
[2.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.0.0
[2.0.1]: https://github.com/Brumbelow/pyinc/releases/tag/v2.0.1
[2.1.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.1.0
[2.5.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.5.0
[2.6.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.6.0
[3.0.0rc1]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0rc1
[3.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0
[3.1.0]: https://github.com/Brumbelow/pyinc/releases/tag/v3.1.0
[3.1.1]: https://github.com/Brumbelow/pyinc/releases/tag/v3.1.1
[4.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v4.0.0
