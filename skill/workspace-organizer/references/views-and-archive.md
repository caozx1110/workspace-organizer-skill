# Generated views and archive behavior

Use deterministic tooling for every index or archive operation. Treat generated
files as disposable projections and archive as the only normal bundle move.

## Generate one atomic view set

Generate these machine and human pairs:

| Catalog | Projection | Content |
| --- | --- | --- |
| `.workspace-organizer/catalog/todo.json` | `00_总览/TODO.md` | Open tasks and next actions |
| `.workspace-organizer/catalog/timeline.json` | `00_总览/TIMELINE.md` | Dated open-task due events |
| `.workspace-organizer/catalog/materials.json` | `00_总览/MATERIALS.md` | Safe visible material evidence |

Load canonical direct-child tasks under `20_任务/` plus exact adopted task roots.
Reject invalid records, duplicate IDs, path escape, missing adopted roots, and
normalized collisions. Ignore canonical candidates under exclusions or nested
Git repositories; reject an explicitly adopted task under either boundary.

Filter to `public` and `internal` before rendering, counting, sorting, or hashing.
Never expose a filtered title, next action, path, count, or digest. Exclude task
bodies and file contents from every view.

Build all six outputs as one unit. On any error, leave the previous set unchanged.
Replace a Markdown projection only when its first line has a valid marker for the
same view:

```text
<!-- workspace-organizer:generated view=<view> schema=1 source_sha256=<64 lowercase hex> -->
```

Never overwrite an unmarked or differently marked file. Serialize catalogs with
the v1 key order, literal Unicode, two-space indentation, LF endings, and one
final newline. Hash the NFC-normalized, correctly sorted `items` array serialized
as compact UTF-8 JSON with sorted object keys and literal Unicode. Add no clock
timestamp so unchanged visible input remains byte-identical.

Treat `assets/empty-generated-views/` as validator fixtures demonstrating the
empty v1 shape and digest. Never copy them into a workspace; invoke the index
tooling so it checks live canonical inputs and collision markers.

## Populate each view

- Sort TODO items by priority (`urgent`, `high`, `normal`, `low`), dated before
  undated, ascending due date, then task ID.
- Build timeline items only from TODO tasks with a due date. Sort by date,
  priority, then task ID.
- Index regular files only from task roles `inputs`, `work`, `deliverables`,
  `records`, and `history`, plus `30_资料库/` and confirmed adopted material
  roots. Exclude `TASK.md`, root/unknown-role files, `pending/`, inbox, staging,
  archive, control-plane, VCS, symlinks, non-regular files, and exclusions.
- Record a material's path, role, owning task ID or null, effective sensitivity,
  byte size, and lowercase SHA-256 content digest. Sort by normalized path.
- Percent-encode UTF-8 link targets and escape backslashes, pipes, and line breaks
  in Markdown table text exactly as required by the v1 contract.

## Archive only eligible bundles

Require every condition before proposing archive:

- Validate configuration and `TASK.md`; require a globally unique task ID.
- Require `completed` or `cancelled`, null `next_action`, and present `closed_at`.
- Require the stable canonical or registered adopted path.
- Require empty or absent `pending/` and no unassigned bundle files.
- Require no unverified or partially applied operation referring to the bundle.
- Require an absent destination with no normalized sibling collision.
- Require an explicit approved plan naming the entire bundle move.

Derive the sole destination as `90_归档/<closed-year>/<area>/<task-id>/`, using
the literal year in `closed_at`. In one verified operation, set `status` to
`archived`, set `archived_at`, advance `updated`, move the complete bundle once,
and verify destination content before considering the source absent. For an
adopted task, remove its exact registration only after the verified move.

Stop on collision, source change, partial move, or failed verification. Never
overwrite a destination, delete a source as recovery, or report an archive as
complete without deterministic verification evidence.
