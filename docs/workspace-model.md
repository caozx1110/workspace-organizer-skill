# Workspace model contract (v1)

Status: normative for schema version 1. This document is the design contract
for the skill package and operation tooling. `MUST`, `MUST NOT`, `SHOULD`, and
`MAY` have their usual requirements-language meanings.

## 1. Authority and model

A workspace is a directory whose root contains
`.workspace-organizer/config.json`. The configuration identifies the workspace
and any exact, adopted paths. A task is registered only when its bundle has one
valid `TASK.md`. The front matter in that file is the sole source of truth for
task facts. Generated catalogs and Markdown views are projections, never
editable task stores.

The model has three kinds of state:

1. **Canonical human state:** `TASK.md`, user materials, and the workspace
   configuration.
2. **Auditable operation state:** approved plans and verification evidence.
3. **Disposable derived state:** catalogs, rendered views, and caches.

No derived file may override canonical state. If canonical inputs are invalid
or ambiguous, generation or mutation MUST stop without replacing the last
known-good result.

All paths stored in configuration, catalogs, plans, or verification records
MUST be workspace-relative POSIX paths. They MUST be Unicode NFC, MUST NOT be
absolute, contain an empty, `.` or `..` segment, contain a backslash or NUL, or
resolve outside the workspace through a symlink. Tools preserve the filesystem
spelling but compare normalized paths; an NFC or case-fold collision is an
error, not a reason to pick one entry.

## 2. Directory and control-plane roles

The seven managed root entries have fixed roles.

| Path | Role | Invariants |
| --- | --- | --- |
| `00_总览/` | Human-readable generated views | Contains only generator-owned `TODO.md`, `TIMELINE.md`, and `MATERIALS.md`. A file without the v1 generated marker is user-owned and MUST NOT be overwritten. |
| `10_收件箱/` | Unclassified incoming material | Content is unassigned and treated as `restricted` until a person confirms its task, role, and sensitivity. Nothing here is automatically deleted or published. |
| `20_任务/` | Canonical active task bundles | New tasks use `20_任务/<task-id>/`. Each direct child that is a registered task has exactly one root `TASK.md`; IDs are globally unique. |
| `30_资料库/` | Reusable reference material | Holds templates, references, and assets that are not owned by one task. Materials inherit the configured default sensitivity unless an adopted-root declaration is stricter. |
| `90_归档/` | Terminal task bundles | The only valid destination is `90_归档/<closed-year>/<area>/<task-id>/`. Archived bundles are read-only during normal operation. |
| `99_待整理/` | Material requiring a decision | Holds uncertain-purpose items, suspected duplicates, temporary files, and compressed originals only after an approved plan. Presence here never authorizes deletion. |
| `.workspace-organizer/` | Hidden control plane | Contains configuration, derived catalogs, immutable approved plans and verification records, and disposable caches. It is not a security boundary. |

The control plane has these roles; directories MAY be created lazily:

```text
.workspace-organizer/
  config.json             # canonical workspace configuration
  catalog/                # disposable generated JSON views
    todo.json
    timeline.json
    materials.json
  plans/                  # immutable operation proposals/approvals (wave 3)
  verification/           # immutable apply/rollback evidence (wave 3)
  cache/                  # disposable and never authoritative
```

`config.json` MUST validate against
[`schemas/workspace-config.schema.json`](../schemas/workspace-config.schema.json).
Its `default_sensitivity` is `internal` for new records unless a user chooses a
different explicit value. `adopted_task_paths` lists exact bundle roots, never
search roots. `adopted_material_roots` classifies exact inventory roots without
making them tasks. `exclude_paths` lists exact roots that tools must not enter.
Built-in exclusions (`.git/`, every nested Git repository,
`.workspace-organizer/cache/`, and every symlink component or target) apply even
when not listed.

Configuration path relationships are compared after NFC normalization and
case-folding. Adopted task roots are pairwise disjoint and MUST NOT overlap an
adopted material root. Exact duplicate material roots are invalid; nested
material roots are allowed only because they retain the same material ownership
and can make a subtree more restrictive. Exclusion roots are pairwise disjoint.
An exclusion MAY be a strict descendant of a task or material root as an
explicit carve-out, but a registered root MUST NOT equal or sit below an
exclusion. These rules reject ambiguous ownership before any registered path is
read.

## 3. Task bundles and stable identity

The canonical path for a new, non-archived task is:

```text
20_任务/<task-id>/
  TASK.md                 # required canonical task record
  inputs/                 # source material received by the task
  work/                   # editable working material
  deliverables/           # intended outputs
  records/                # decisions, approvals, and correspondence
  history/                # meaningful retained snapshots, not a cache
  pending/                # task-owned items awaiting a classification decision
```

Only `TASK.md` is required; role directories are created as needed. A regular
file at the bundle root or in an unknown subdirectory is `unassigned` and is
not moved or placed in the default material index. `pending/` is also excluded
from that index because its classification is unresolved.

A task ID is 3-64 lowercase ASCII characters matching
`^[a-z0-9]+(?:-[a-z0-9]+)*$`. It is opaque, globally unique across active,
adopted, and archived tasks, and never reused. Status, title, area, type, due
date, or path changes MUST NOT change the ID. For a canonical task the bundle
directory name equals the ID. An adopted bundle may retain a different legacy
directory name; its exact root is registered in `adopted_task_paths` and remains
stable after registration.

Changing title, status, priority, area, type, due date, tags, or sensitivity
does not move a registered bundle. There is exactly one normal lifecycle move:
the verified archive move described in section 5.

## 4. `TASK.md` syntax and schema

`TASK.md` is UTF-8 Markdown with a restricted YAML 1.2 front matter mapping.
The first line MUST be `---`; the next `---` line closes the mapping. Each
non-empty mapping line is one `key: value` pair. Duplicate keys, YAML anchors,
tags, aliases, block scalars, multiline scalars, comments, and nested mappings
are invalid. Human strings, dates, timestamps, and tag arrays use JSON syntax;
enumerated slug values may be unquoted. The Markdown body is free-form and is
never parsed for canonical task facts.

Example:

```markdown
---
schema_version: 1
id: research-agent-safety
title: "Survey safe agent evaluation"
status: active
area: research
type: research
priority: high
due: "2026-09-30"
sensitivity: internal
next_action: "Compare the retained benchmark protocols"
updated: "2026-08-17T10:00:00+08:00"
closed_at: null
archived_at: null
tags: ["agents", "evaluation"]
---

# Survey safe agent evaluation
```

The normalized front matter MUST validate against
[`schemas/task.schema.json`](../schemas/task.schema.json). The following rules
are additionally normative where JSON Schema cannot compare versions or
timestamps:

| Field | Rule |
| --- | --- |
| `schema_version` | Required integer `1`. A different value requires an explicit migration; tools MUST NOT guess. |
| `id` | Required stable ID using the rule in section 3. |
| `title` | Required trimmed, single-line Unicode string, 1-200 characters. |
| `status` | One of `planned`, `active`, `waiting`, `blocked`, `completed`, `cancelled`, or `archived`. |
| `area` | Required extensible lowercase ASCII slug, 1-48 characters. It groups ownership but does not control the active path. |
| `type` | Required extensible lowercase ASCII slug, 1-48 characters. It selects optional guidance, never a separate lifecycle. |
| `priority` | One of `urgent`, `high`, `normal`, or `low`. |
| `due` | An actual Gregorian `YYYY-MM-DD` date or `null`. It is a date in the user's workspace context, not a UTC instant. |
| `sensitivity` | One of `public`, `internal`, `confidential`, or `restricted`; it is always explicit on a valid task. |
| `next_action` | A trimmed, single-line string of 1-500 characters for open states; `null` for closed or archived states. |
| `updated` | RFC 3339 timestamp with seconds and an explicit `Z` or numeric offset. Every semantic edit makes it later than the previous value. |
| `closed_at` | `null` in open states; a timestamp in `completed`, `cancelled`, and `archived`. Cleared when a task is reopened before archival. |
| `archived_at` | `null` before archival; a timestamp at or after `closed_at` in `archived`. |
| `tags` | Optional array of at most 32 unique lowercase ASCII slugs. Tags do not change lifecycle behavior. |

When a closing or archive transition is recorded, `updated` MUST be at or after
the corresponding transition timestamp. Unknown front matter keys are invalid
in schema version 1 rather than silently ignored.

`area` and `type` deliberately have syntax, not enumerated taxonomies. The
representative families use types `research`, `review`, `reimbursement`,
`presentation`, `competition`, `collaboration`, and `contract`, all with the
same schema and transition graph.

## 5. Lifecycle, path stability, and archive eligibility

The machine-readable transition graph is
[`contracts/lifecycle.json`](../contracts/lifecycle.json). New tasks start in
`planned` or `active`. A same-status edit is a metadata update, not a lifecycle
transition, and still advances `updated`.

| Current | Allowed next status |
| --- | --- |
| `planned` | `active`, `cancelled` |
| `active` | `waiting`, `blocked`, `completed`, `cancelled` |
| `waiting` | `active`, `blocked`, `completed`, `cancelled` |
| `blocked` | `active`, `waiting`, `completed`, `cancelled` |
| `completed` | `active`, `archived` |
| `cancelled` | `planned`, `active`, `archived` |
| `archived` | none |

`completed` and `cancelled` are closed, pre-archive states. They may be reopened
only by the transitions above, before archival. `archived` is the terminal
normal state. An erroneous archive can be reversed only as a verified rollback
of the exact archive operation; that rollback restores the pre-archive path and
record and is not a normal transition.

A bundle is eligible for archive only when all of these are true:

- its `TASK.md` and workspace configuration are valid and its ID is unique;
- its status is `completed` or `cancelled`, `next_action` is `null`, and
  `closed_at` is present;
- it is at its stable canonical or registered adopted path;
- `pending/` is absent or empty and no bundle file is unassigned;
- no unverified or partially applied operation refers to the bundle;
- the destination does not exist and its parent has no normalized collision;
- an explicit operation plan names the entire bundle move and is approved.

The only destination is
`90_归档/<YYYY>/<area>/<task-id>/`, where `YYYY` is the four-digit year in the
literal `closed_at` timestamp and `area` is the current validated area slug. A
successful archive sets `status: archived` and `archived_at`, advances
`updated`, moves the whole bundle once, and verifies the destination before the
source can be considered absent. If the source was adopted, the same approved
operation removes its old exact entry from `adopted_task_paths`; the archived
record is then discovered only at its archive path. A collision, changed
source, partial move, or failed verification stops safely and never authorizes
overwrite or deletion.

## 6. Deterministic generated views

The three machine views validate against
[`schemas/generated-view.schema.json`](../schemas/generated-view.schema.json):

| Machine catalog | Human projection | Purpose |
| --- | --- | --- |
| `.workspace-organizer/catalog/todo.json` | `00_总览/TODO.md` | Open tasks and their canonical next actions. |
| `.workspace-organizer/catalog/timeline.json` | `00_总览/TIMELINE.md` | Dated `due` events for open tasks. |
| `.workspace-organizer/catalog/materials.json` | `00_总览/MATERIALS.md` | Safe-to-display material paths and file evidence. |

### 6.1 Inputs and failure behavior

Task inputs are valid records at canonical direct-child paths under `20_任务/`
plus the exact roots in `adopted_task_paths`. Archived records do not enter the
default overview. A duplicate ID, invalid record, missing adopted path, path
escape, or normalized collision is a validation error. A generator MUST leave
all existing generated files unchanged rather than publish a partial refresh.

Before reading any `TASK.md`, a conforming loader validates the lexical relative
path, rejects a symlink in any component or at the file, resolves the regular
file strictly inside the resolved workspace root, and checks exclusions and
nested Git boundaries. Canonical or archived candidates under `exclude_paths`
or a nested Git repository are ignored without reading them. An explicitly
registered adopted task under either boundary is a configuration error rather
than a silently missing task.

The default projection is fixed to `public` and `internal` tasks. Filtering
happens before sorting and hashing. `confidential`, `restricted`, missing,
unknown, or malformed sensitivity never contributes text, paths, counts, or a
source digest to a default view.

Each JSON catalog contains `schema_version`, `view`, `profile`,
`source_sha256`, and `items`. Normalize all strings to NFC, sort items by the
rules below, serialize the `items` array as UTF-8 JSON with lexicographically
sorted object keys, no insignificant whitespace, and literal Unicode, then set
`source_sha256` to the lowercase SHA-256 hex digest of those bytes. Do not add
wall-clock generation timestamps. Unchanged visible inputs therefore produce
byte-identical outputs.

Catalog JSON is UTF-8 with literal Unicode, two-space indentation, LF line
endings, and one final newline. Top-level keys use the order `schema_version`,
`view`, `profile`, `source_sha256`, `items`; item keys use their order in the
generated-view schema. This formatting is part of the golden example contract.

All six outputs are replaced as one generation unit or not at all. A Markdown
projection starts with this exact marker shape:

```text
<!-- workspace-organizer:generated view=<view> schema=1 source_sha256=<64 lowercase hex> -->
```

A generator may replace a prior file only when its first line is a valid marker
for the same view. This collision rule protects user-authored files.

### 6.2 TODO

TODO inputs are tasks in `planned`, `active`, `waiting`, or `blocked` after the
default sensitivity filter. Sort by priority (`urgent`, `high`, `normal`,
`low`), then dated tasks before undated tasks, ascending due date, then ID.
Every item contains only the fields required by the generated-view schema and
points to the canonical workspace-relative `TASK.md`.

`TODO.md` has the marker, `# TODO`, and a table with columns `Priority`, `Due`,
`Status`, `Task`, and `Next action`. A null due date renders as `—`. The task
cell links the stable ASCII ID to `../` plus the percent-encoded UTF-8 record
path, keeping only `/`, `-`, `.`, `_`, and `~` unescaped.

### 6.3 Timeline

Timeline inputs are the TODO input set with a non-null `due`. Each item is a
`due` event. Sort by ascending date, priority rank, then ID. `TIMELINE.md` has
the marker, `# Timeline`, and columns `Date`, `Event`, `Priority`, `Status`, and
`Task`.

### 6.4 Material index

The material inventory includes regular files from any non-archived registered
task under the known task roles
`inputs`, `work`, `deliverables`, `records`, and `history`, plus regular files
under `30_资料库/` and confirmed `adopted_material_roots`. It excludes
`TASK.md`, bundle-root or unknown-role files, `pending/`, `10_收件箱/`,
`99_待整理/`, `90_归档/`, generated views, the control plane, VCS boundaries,
symlinks, sockets, devices, and anything under an excluded path.

Task material inherits the task sensitivity. For library and adopted material,
start with `default_sensitivity`, collect every `adopted_material_roots`
declaration whose root contains the file, and choose the most restrictive value
using `public < internal < confidential < restricted`. Therefore a `public`
adopted declaration cannot downgrade an `internal` default, while a nested
`restricted` declaration excludes its subtree. Apply the default sensitivity
filter only after this reduction. An item contains the workspace-relative path,
role, owning task ID or `null`, effective sensitivity, byte size, and lowercase
SHA-256 content digest. Sort by normalized path code point order.
`MATERIALS.md` has the marker, `# Materials`, and columns `Role`, `Task`,
`Material`, `Bytes`, and `SHA-256`. The material link uses the same
percent-encoding rule as task links.

For every Markdown table, replace `\\` with `\\\\`, `|` with `\\|`, and a
line break with one space in human text. Machine catalogs remain the primary
derived interface; Markdown files are deterministic renderings of them.

## 7. Sensitivity contract

Sensitivity describes the most restrictive handling needed by the task and its
paths:

| Value | Default handling |
| --- | --- |
| `public` | May appear in local default and explicitly public projections. Publication still requires a separate user action. |
| `internal` | May appear in the local default projection; MUST NOT be treated as publishable. This is the new-record default. |
| `confidential` | Excluded from every default generated view and dashboard input. |
| `restricted` | Excluded from every default generated view and used for unclassified or unknown material. |

The order in this table is normative from least to most restrictive. Whenever
multiple declarations apply to the same non-task material, the effective value
is their maximum. Configuration never uses last-entry-wins or a longest-prefix
override, and no declaration can reduce an inherited restriction.

The task body and file contents are never copied into generated views.
`next_action` and material filenames are considered sensitive metadata and are
filtered with the task before hashing. Control-plane plans and verification
records may contain paths and hashes, so they are at least `internal`, remain
local, and are never automatically copied into a view or publication artifact.
The hidden directory is organizational, not an access-control mechanism.

An explicit future non-default projection may include a stricter sensitivity
only when a user approves both the levels and destination for that invocation.
No such opt-in is stored as the v1 default.

## 8. Adopt-in-place contract

Adoption does not mean immediate normalization. It has ordered gates:

1. **Read-only inventory.** Identify entries, managed-name collisions,
   symlinks, VCS boundaries, normalized collisions, and candidate task roots.
   Resolve every candidate within the workspace; do not follow links or enter
   excluded paths or nested repositories, and do not infer task ownership.
2. **Confirm the workspace root.** With explicit approval, create the control
   configuration and only missing, collision-free managed directories. Existing
   entries are not overwritten or reinterpreted silently.
3. **Register exact roots.** A person selects existing task bundles and material
   roots. Add a valid `TASK.md` to each accepted task root and list that exact
   relative root in `adopted_task_paths`; classify material roots explicitly.
   Reject ambiguous task/material/exclusion overlap. Nested material roots may
   only tighten sensitivity through the most-restrictive rule. Inventory
   candidates that are not selected remain unmanaged.
4. **Generate only safe projections.** Apply schema, uniqueness, path, and
   sensitivity checks. An existing unmarked overview file blocks generation.
5. **Propose optional migrations separately.** Any rename, move, role
   classification, quarantine, deduplication, or archival action belongs to an
   explicit scan -> proposal -> dry-run -> approval -> apply -> verify plan.

Adoption by itself MUST NOT rename, move, delete, overwrite, hash-upload,
publish, or globally reorganize existing content. Existing managed directories
may be accepted only after their role is confirmed. Uncertain files stay in
place until a plan is approved; `99_待整理/` is a destination for a decision,
not an automatic dumping ground.

Once an existing root is registered as a task, it follows the same schema,
lifecycle, sensitivity, stable-path, and archive rules as a canonical task. If
a user wants it moved to `20_任务/<task-id>/`, that migration must happen and
verify before registration; after registration the only normal move is archive.

## 9. Conformance examples

[`examples/workspace/`](../examples/workspace/) contains seven synthetic task
families, safe sample materials, configuration, and golden generated catalogs
and Markdown views. [`examples/adoption/config.json`](../examples/adoption/config.json)
demonstrates exact Unicode and space-containing adopted paths plus nested
material sensitivity declarations without moving anything.

Run these dependency-free checks from the repository root:

```sh
python3 scripts/validate_workspace_model.py examples/workspace
python3 -m unittest discover -s tests -v
```

The validator is design-time conformance tooling only. It reads task records
and configuration; it does not initialize, adopt, move, archive, overwrite, or
delete workspace content.
