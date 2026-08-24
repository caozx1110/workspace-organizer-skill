# Clean-slate canonical model

Machine-readable contracts live in [`schemas/task-v2.schema.json`](../../../schemas/task-v2.schema.json),
[`schemas/capture-v2.schema.json`](../../../schemas/capture-v2.schema.json),
[`schemas/artifact-v2.schema.json`](../../../schemas/artifact-v2.schema.json), and
[`schemas/workspace-config-v2.schema.json`](../../../schemas/workspace-config-v2.schema.json).
The lifecycle graph is recorded in [`contracts/lifecycle-v2.json`](../../../contracts/lifecycle-v2.json).

Read this reference before creating, updating, closing, triaging, attaching, or
archiving a record.

## Authority

Task, Capture, and Artifact Markdown records are canonical. Generated views,
Obsidian Bases, caches, and chat history are not canonical and never write facts
back into records. Every canonical note starts with `kind` and
`schema_version: 2`; a loader must not interpret arbitrary vault Markdown as a
managed record.

## Task identity and bundle

Create a stable ID such as `20260824T135501-renew-passport` and use it for both
the bundle and note:

```text
20_任务/<id>/
  <id>.md
  01_输入/
  02_工作/
  03_交付/
  04_记录/
```

Only the note is required. Create role directories on demand. Do not move an
active bundle when title, area, type, priority, or status changes. The normal
bundle move is an approved archive operation.

An open task has `status` in `planned`, `active`, `waiting`, or `blocked`,
`storage_state: active`, and a non-empty `next_action`. A closed task keeps
`status: completed|cancelled`, sets `next_action: null`, and records both
`closed_at` and a useful `closure_summary`. Archive changes only
`storage_state` and `archived_at`; it never erases whether the outcome completed
or was cancelled.

Core keys and transition guards fail closed. Preserve unknown safe extension
keys, Obsidian properties (`aliases`, `tags`, `cssclasses`), the Markdown body,
and their ordering during a CLI edit. Do not silently correct misspelled core
keys. Use expected SHA-256/CAS for edits and stop if Obsidian changed the file
after it was read.

## Capture and triage

Capture preserves the original input under `10_收件箱/`. It is not a Task until
triage identifies an outcome, owner, area, and first action. Triage can create a
Task, attach an Artifact to a unique existing Task, promote a shared item to
`30_资料库/`, or defer it. Do not invent a target when candidates are ambiguous.

## Artifact custody

Keep payload bytes separate from an Artifact Markdown record. The record stores
an immutable artifact ID plus current workspace-relative path, one canonical
owner or `null` for the Library, role, effective sensitivity, provenance,
byte-level SHA-256, and derivation lineage. OCR, conversion, redaction, summary,
and export each create a new Artifact with `derived_from` and tool/version
evidence. Copy and hash by default; deleting the original needs a separate
explicit instruction.

## Disclosure and Agent access

Do not derive Agent access from sensitivity. `sensitivity` is the disclosure
risk (`public` through `restricted`); `agent_access` is the current capability
(`none`, `metadata`, `content`). Task, Capture, body and Artifact payload are
separate boundaries. Missing v2 access fields use least-privilege migration
defaults (`metadata` for Task/Capture, `none` for Artifact); unknown values fail
closed. Increasing access is a human authorization, not a semantic Agent edit.

The local cockpit is private and complete through `restricted`. A share/export
projection has an explicit sensitivity ceiling and filters before counts,
ordering, digest generation or rendering.

Sensitivity is explicit metadata chosen by the human or an accepted workspace
rule, not a confidence score inferred from prose. When owner, purpose or
sensitivity is uncertain, keep the Capture in Inbox and use the conservative
`restricted` value until a human confirms it. Never derive `agent_access` from
the sensitivity label.

Effective sensitivity is the most restrictive applicable declaration. Lowering
sensitivity, publishing, or sending content outside the workspace is a risk
operation and requires explicit approval.
