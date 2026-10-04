# Getting Started

This guide builds a small incremental program from the public `pyinc` API. It
covers the five choices every application makes: stable input keys, query
boundaries, tracked resources, an execution mode, and where side effects occur.

## Install

```console
python -m pip install pyinc
```

The wheel has no runtime dependencies and includes `pyinc`, `pyinc_tools`, and
`pyinc_codegen`.

## 1. Declare keyed inputs and queries

An `Input` is a base value supplied by your application. Give it a stable,
non-empty key. The key is its identity in a `Database` and in checkpoints.

```python docs-check
from pyinc import Database, Input, query

SOURCE = Input[str]("guide.source")


@query
def words(db: Database) -> tuple[str, ...]:
    return tuple(SOURCE.read(db).split())


@query
def word_count(db: Database) -> int:
    return len(words(db))


db = Database()
db.set(SOURCE, "incremental work stays focused")
assert db.get(word_count) == 4

db.set(SOURCE, "incremental work stays very focused")
assert db.get(word_count) == 5
```

Calling one query from another is ordinary Python. While `word_count` runs,
the database records its dependency on `words`, and `words` records its
dependency on `SOURCE`. `db.get(...)` is the top-level request boundary.
Calling a `Query` inside another query delegates to the same database.

A query's default identity is `module:qualified_name`. Pass an explicit
`@query(key="...")` only when your deployment needs a more stable one. An `Input` or
query accepts `eq=` for custom equality or `cutoff=` for a snapshot-safe
comparison token. Use at most one of the two.

## 2. Track files and other resources

Inputs are pushed into a database. Resources are pulled from external state and
re-probed when a request needs them. Built-in resources cover text and binary
files, file metadata, environment variables, directory listings, and symlink-
aware path resolution.

```python docs-check
from pathlib import Path
from tempfile import TemporaryDirectory

from pyinc import Database, FileResource, query

FILES = FileResource()


@query
def nonempty_lines(db: Database, path: str) -> tuple[str, ...]:
    text = FILES.read(db, path)
    return tuple(line.strip() for line in text.splitlines() if line.strip())


with TemporaryDirectory() as directory:
    path = Path(directory, "names.txt")
    path.write_text("Ada\n\nGrace\n", encoding="utf-8")

    db = Database()
    assert db.get(nonempty_lines, str(path)) == ("Ada", "Grace")

    path.write_text("Ada\nLinus\n", encoding="utf-8")
    assert db.get(nonempty_lines, str(path)) == ("Ada", "Linus")
```

Inside a query, the raw reads listed in
[condition 2](kernel-contract.md#2-tracked-ambient-reads) (file opens,
environment access, directory listings) raise `UntrackedReadError` outside a
resource. Some ambient reads escape the guard, such as `os.open()`, subprocess
output, time, random values, network calls, or C extensions. For those, call
`db.report_untracked_read(reason)`. That node then executes on every request and
never backdates.

A custom resource implements `probe`, `load`, and `label`. `read`,
`probe_and_load`, and `identity` have working defaults you may override. The
instance itself must be snapshot-safe. Use a frozen dataclass whose fields are
snapshot-safe, or a class whose `identity()` returns a snapshot-safe value.
Otherwise the kernel refuses the first `get()` of a query that captures it. The
kernel probes a resource when a request needs that node verified. It runs at most
one standalone probe per request per resource key, and only for nodes the request
reaches. Read the
[kernel contract](kernel-contract.md#conditions-for-from-scratch-consistency)
before relying on a custom probe across checkpoints.

## 3. Choose a mode

The mode changes boundary exposure and mutation checking. Dependency and
ambient-read tracking work the same in every mode.

| Mode | Values seen by queries and callers | In-query mutation |
|---|---|---|
| `strict` | Frozen snapshot views such as `FrozenList`, `FrozenDict`, `FrozenSet`, and `FrozenRecord` | An ordinary write fails immediately. `object.__setattr__` can still rebind a field, so the kernel rebuilds every exposed view. |
| `checked` | Owned thawed copies | A before/after fingerprint detects mutation. |
| `fast` | Owned thawed copies | Unchecked. The caller must keep queries deterministic and non-mutating. |

Start with `Database(mode="strict")`. Move a measured workload to `checked` or
`fast` only when its code needs ordinary containers at a boundary.

`freeze()` converts a value to a snapshot, and the round trip can change its
shape. A mapping comes back in canonical order. A dataclass thaws to a
dictionary unless a `ValueAdapter` reconstructs it.
[Condition 1](kernel-contract.md#1-value-boundary-ownership) of the kernel
contract states the rules.

## 4. Inspect what happened

`inspect()` is observational. It reports the most recently recorded decision
and starts no verification pass. Use `inspect_fresh()` when the inspection must
first verify current inputs and resources.

```python docs-check
from pyinc import Database, Input, query

NUMBER = Input[int]("guide.number")


@query
def doubled(db: Database) -> int:
    return NUMBER.read(db) * 2


db = Database()
db.set(NUMBER, 3)
assert db.get(doubled) == 6
assert db.inspect(doubled).last_decision == "executed"

db.set(NUMBER, 3)
assert db.get(doubled) == 6
assert db.inspect(doubled).last_decision == "reused"

print(db.explain(doubled))
print(db.statistics())
print(db.query_profile())
```

Wrap a batch of reads in `db.request_span()` when they should all see one
world. The batch shares a single resource-validation pass. A `db.set` inside
the span that changes an input rolls the span, so later reads
re-derive. A caller that changes the world some other way declares it with
`db.request_inputs_changed()`, which is a no-op outside a span. Spans nest, and
only the outermost close ends the request.

Use `dependency_graph()` for a machine-readable graph. `statistics()` reports
work counts and cache decisions. `query_profile()` reports timing aggregates.
These APIs give better correctness and performance signals than wall-clock time
alone.

## 5. Reconcile a first output

Queries describe values and remain free of side effects. An `@action` is a
separate top-level boundary that turns a complete desired `Output` set into
files.

```python docs-check
from pathlib import Path
from tempfile import TemporaryDirectory

from pyinc import Database, Input, Output, action, query

NAME = Input[str]("guide.action.name")


@query
def greeting(db: Database) -> str:
    return f"Hello, {NAME.read(db)}!\n"


@action(tool="getting-started/greeting-v1")
def write_greeting(db: Database) -> tuple[Output, ...]:
    return (Output.text("greeting.txt", greeting(db)),)


with TemporaryDirectory() as directory:
    db = Database()
    db.set(NAME, "Ada")

    plan = write_greeting.plan(db, root=directory)
    assert plan.created == ("greeting.txt",)
    assert plan.dry_run

    result = write_greeting.reconcile(db, root=directory)
    assert result.created == ("greeting.txt",)
    assert Path(directory, "greeting.txt").read_text() == "Hello, Ada!\n"
```

`plan()` runs the same preflight and locking as `reconcile()` and leaves the
outputs and the ownership ledger unchanged. The action owns only the paths
recorded in its validated ledger. Review the [action contract](action-contract.md)
before sharing an output root between tools.

## Next steps

- Depend on the guarantee: [Kernel contract](kernel-contract.md)
- Analyze source and configuration: [Integration contract](integration-contract.md)
- Generate a file set: [Codegen guide](codegen-guide.md)
- Run analysis or connect an editor: [`pyinc-tools` guide](pyinc-tools-guide.md)
- Understand package boundaries: the [package map](README.md#packages)
