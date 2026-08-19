---
name: workspace-organizer
description: Initialize and safely organize durable filesystem workspaces for tasks, materials, local TODOs, timelines, static dashboards, and archives. Use when Codex is asked to create a managed workspace, adopt existing folders in place, create or update canonical TASK.md records, classify workspace materials, regenerate workspace-organizer views, generate the optional read-only dashboard, or archive closed tasks. Apply the v1 contract without silently moving, overwriting, deleting, publishing, or exposing sensitive content.
---

# Workspace Organizer

Organize durable work around canonical task records while preserving user files and
making every structural change reviewable.

## Establish the operation

1. Confirm the workspace root and the user's requested outcome.
2. Treat `.workspace-organizer/config.json` and each registered task's `TASK.md`
   as canonical; treat catalogs, Markdown overviews, and caches as derived.
3. Inspect before proposing a write. Reject absolute or escaping paths, symlinks,
   nested Git repositories, normalized collisions, and ambiguous ownership.
4. Select only the references needed for the request:
   - Read [references/initialization-and-adoption.md](references/initialization-and-adoption.md)
     before initializing a workspace, adopting existing content, or inspecting
     compressed originals in inbox/staging areas.
   - Read [references/task-contract.md](references/task-contract.md) before creating
     or changing a task, status, metadata, or sensitivity.
   - Read [references/views-and-archive.md](references/views-and-archive.md) before
     indexing, regenerating views, checking archive eligibility, or archiving.
   - Read [references/dashboard.md](references/dashboard.md) before generating or
     verifying the optional read-only static dashboard.
   - Read [references/tooling.md](references/tooling.md) before invoking the
     deterministic CLI or integrating its Python API.

## Preserve safety boundaries

- Keep all stored paths workspace-relative POSIX paths in Unicode NFC.
- Keep task IDs stable and keep registered bundles at their registered path until
  an approved archive operation.
- Treat unclassified inbox content and unknown sensitivity as `restricted`.
- Filter `confidential` and `restricted` records before rendering, counting,
  sorting, or hashing default views.
- Never infer ownership, weaken sensitivity, cross a VCS or exclusion boundary,
  overwrite an unmarked generated-view path, or use generated files as truth.
- Never automatically overwrite, delete, publish, upload hashes, deduplicate,
  rename, move, or migrate user content.
- Require explicit approval of the exact proposal before any structural mutation.

## Route operations

### Initialize or adopt

Follow the ordered gates in `initialization-and-adoption.md`. Use
[`assets/workspace-config.json`](assets/workspace-config.json) only as valid
starter content: replace its example identity with the approved stable workspace
ID before writing. Create only missing, collision-free managed entries after
approval. Register existing task and material roots exactly; do not normalize
their locations as part of adoption.

### Create or update a task record

Follow `task-contract.md`. Use [`assets/TASK.md`](assets/TASK.md) only as valid
starter content and replace every example fact before registration. Preserve the
restricted front matter syntax, validate the whole record, advance `updated` for
every semantic edit, and leave the bundle path unchanged.

### Scan, organize, generate, or archive

Delegate every mutation-heavy `scan -> proposal -> dry-run -> approval -> apply
-> verify` flow, plus `index` and `archive`, to
[`scripts/workspace_organizer.py`](scripts/workspace_organizer.py). Follow the
CLI/API sequence in `references/tooling.md`; do not reimplement those operations
with ad hoc shell commands or general-purpose file writes.

If the deterministic scripts are not installed, stop after a read-only inspection
and a non-executable proposal. State that apply, verification, index generation,
and archive are unavailable; do not approximate them. Treat
[`assets/empty-generated-views/`](assets/empty-generated-views/) as contract
fixtures for tooling, never as files to copy directly into a live workspace.

### Generate or verify the optional dashboard

Follow `references/dashboard.md` and delegate dashboard generation and freshness
checks to [`scripts/workspace_dashboard.py`](scripts/workspace_dashboard.py).
Generate the v1 indexes first. Treat `.workspace-organizer/dashboard/` as a
disposable derived location: its controls only filter or navigate locally, and
its absence never blocks a v1 operation. Never use dashboard HTML or its
manifest as task truth.

## Finish safely

Validate canonical inputs before and after an allowed record-only edit. For a
structural operation, require the tooling's verification evidence and report
changed paths, unchanged protected paths, collisions, skipped boundaries, and
remaining decisions. Leave existing known-good generated outputs untouched on
any validation or generation failure.
