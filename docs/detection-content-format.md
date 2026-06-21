# Detection-Content Format and Supported Grammar

The `pyinc.integrations.detection_rules` compiler consumes a **deliberately
small, normalized detection-rule format** expressed in stdlib-readable JSON. It is
Sigma/YARA-*inspired* but **claims no compliance** with Sigma, YARA, Splunk,
Elastic, or Sentinel. Adapters for those real formats are possible follow-up work;
they are out of scope here (arbitrary YAML and full SIEM semantics conflict with
the stdlib-only, deterministic design).

## Input layout

All inputs live under one rules root:

```
<root>/
  rules/<rule_id>.json     one rule per file (the file stem is the rule id)
  mappings.json            logical field -> { backend: physical field }
  macros.json              reusable named expressions
  tests/<rule_id>.json     optional rule-test fixtures
```

Missing optional files are treated as empty. All reads are tracked through the
kernel's resource API; the compiler performs no network, subprocess, clock, or
environment access.

## Rule file

```json
{
  "id": "powershell_enc",
  "title": "PowerShell Encoded Command",
  "description": "Detects encoded PowerShell command lines.",
  "severity": "high",
  "attack": ["T1059.001"],
  "references": ["https://attack.mitre.org/techniques/T1059/001/"],
  "detection": { "all": [
    {"field": "process.name", "op": "equals", "value": "powershell.exe"},
    {"macro": "encoded_command"}
  ]}
}
```

`id`, `title`, `description`, `severity`, `attack` (technique ids), and
`references` are metadata. `detection` is the expression (below).

## Expression grammar (complete supported set)

```
expr     := leaf | all | any | not | macro
leaf     := {"field": <logical-field>, "op": <operator>, "value": <scalar | [scalars]>}
all      := {"all": [expr, ...]}        // logical AND
any      := {"any": [expr, ...]}        // logical OR
not      := {"not": expr}               // logical NOT
macro    := {"macro": <macro-name>}     // expands a shared expression
operator := equals | not_equals | in | contains | startswith | endswith
          | exists | gt | gte | lt | lte | regex
```

- `in` takes a list value; all other operators take a scalar.
- `regex` uses Python's stdlib `re`.
- Macros may reference other macros (expansion is depth-limited and a recursion
  diagnostic is emitted past the limit).
- A logical field used in a rule is mapped to a backend-specific physical field
  via `mappings.json`; an unmapped field falls back to its logical name.

Anything outside this grammar — an unknown operator, an undefined macro, a
non-object expression node, a missing `detection` block, invalid rule JSON —
produces an explicit deterministic `DetectionDiagnostic`. A rule with diagnostics
is reported (in `bundle.json`, its coverage fragment, and its doc) but is **never
rendered with approximated output**.

## Field mappings and macros

```json
// mappings.json
{ "process.name": {"splunk": "Image", "elastic": "process.name", "sentinel": "ProcessName"} }

// macros.json
{ "encoded_command": {"any": [
    {"field": "command_line", "op": "contains", "value": "-enc"},
    {"field": "command_line", "op": "contains", "value": "-EncodedCommand"}
]}}
```

A rule depends only on the fields and macros it actually references. Editing an
unrelated field mapping or macro performs **zero** output writes; editing a used
mapping regenerates only the affected backend queries.

## Rule-test fixtures

```json
// tests/<rule_id>.json
{ "events": [ {"process.name": "powershell.exe", "command_line": "... -enc ..."} ],
  "expect": [true] }
```

Events are evaluated against the rule's logical IR (mapping-independent). The
compiler emits a deterministic `tests/<rule_id>.json` result artifact per rule
with a fixture.

## Backends

Three small, explicitly-scoped renderers ship: `splunk` (SPL-like, `.spl`),
`elastic` (KQL/query-string-like, `.kql`), and `sentinel` (KQL-like, `.kql`).
They cover the operator set above with deterministic per-backend escaping. They
are **not** complete SIEM query languages and are never executed against a real
backend.

## Generated outputs

```
queries/<backend>/<rule_id>.<ext>   one rendered query per valid rule × backend
tests/<rule_id>.json                rule-test results (rules with a fixture)
coverage/<rule_id>.json             per-rule coverage fragment
docs/<rule_id>.md                   rule documentation
bundle.json                         normalized detection bundle (all rules' IR)
coverage_matrix.json                aggregate technique → rules matrix
```

Output ordering and bytes are deterministic. Aggregates (`bundle.json`,
`coverage_matrix.json`) legitimately rewrite when a constituent rule's contribution
changes; tests distinguish those unavoidable aggregate rewrites from unrelated
per-rule rewrites.

## Provenance

`rule_provenance(db, root, rule_id, backend)` returns a `DetectionProvenance`
naming the output path, source rule path + id, referenced mappings and macros, the
backend, and the transformation query nodes (derived from `Database.inspect`).
`Database.explain(rendered_rule, root, rule_id, backend)` shows the same
dependency graph for human inspection.

## Out of scope

- Sigma / YARA / Splunk / Elastic / Sentinel standards compliance or validation.
- Executing queries against a live SIEM or running a YARA engine.
- Arbitrary YAML rule ingestion.
- A general template engine (rendering uses fixed, documented backend renderers).
- Network, subprocess, clock, or environment access.
