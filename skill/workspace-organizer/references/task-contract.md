# Task record, lifecycle, and sensitivity

Use one schema and transition graph for every task type. Keep optional guidance
type-specific, but never fork the lifecycle.

## Keep stable identity and paths

- Use an opaque globally unique ID matching
  `^[a-z0-9]+(?:-[a-z0-9]+)*$` with 3-64 characters.
- Never reuse or change the ID when metadata changes.
- Create new bundles at `20_任务/<task-id>/`. Register an adopted bundle by its
  exact existing root even when its directory name differs from the ID.
- Do not move a registered task for a title, area, type, status, priority, due
  date, tag, or sensitivity change. Permit only the verified archive move.
- Require exactly one root `TASK.md` per registered bundle.

Create role directories only when needed: `inputs/`, `work/`, `deliverables/`,
`records/`, `history/`, and `pending/`. Treat bundle-root files and unknown
directories as unassigned. Keep `pending/` unresolved and out of the material
index.

## Write valid front matter

Start UTF-8 `TASK.md` with `---` and close front matter with the next `---`.
Write each non-empty line as one `key: value` pair. Reject duplicate or unknown
keys, comments, nested mappings, multiline values, aliases, anchors, tags, and
block scalars. Encode human strings, dates, timestamps, and tag arrays as JSON;
write enumerated slugs bare. Keep canonical facts out of the free-form body.

Require these schema-v1 fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1`; require explicit migration for any other value |
| `id` | Stable 3-64 character task ID |
| `title` | Trimmed single-line Unicode, 1-200 characters |
| `status` | One lifecycle status below |
| `area`, `type` | Lowercase ASCII slugs, 1-48 characters |
| `priority` | `urgent`, `high`, `normal`, or `low` |
| `due` | Actual Gregorian `YYYY-MM-DD` date or `null` |
| `sensitivity` | `public`, `internal`, `confidential`, or `restricted` |
| `next_action` | Trimmed single line, 1-500 characters for open states; otherwise `null` |
| `updated` | RFC 3339 with seconds and explicit offset; advance on every semantic edit |
| `closed_at` | `null` while open; transition timestamp while closed or archived |
| `archived_at` | `null` before archive; archive timestamp at or after `closed_at` |
| `tags` | Optional array of at most 32 unique lowercase slugs |

Use `assets/TASK.md` as syntactically valid starter content. Replace its example
identity and facts, validate the result, and only then register the task.

## Apply one lifecycle

Start new tasks as `planned` or `active`. Treat same-status edits as metadata
updates that still advance `updated`.

| Current | Allowed next status |
| --- | --- |
| `planned` | `active`, `cancelled` |
| `active` | `waiting`, `blocked`, `completed`, `cancelled` |
| `waiting` | `active`, `blocked`, `completed`, `cancelled` |
| `blocked` | `active`, `waiting`, `completed`, `cancelled` |
| `completed` | `active`, `archived` |
| `cancelled` | `planned`, `active`, `archived` |
| `archived` | none |

For open states, require a non-null `next_action` and null transition timestamps.
For `completed` or `cancelled`, set `next_action` to null, set `closed_at`, keep
`archived_at` null, and keep `updated` at or after `closed_at`. Clear `closed_at`
when reopening before archive. Treat `archived` as terminal; reverse an erroneous
archive only through verified rollback of that exact operation.

## Enforce sensitivity

Order sensitivity from least to most restrictive as `public < internal <
confidential < restricted`. Default new records to `internal`. Use the maximum
restriction of every declaration that covers reusable or adopted material; never
let a child or later declaration weaken inherited sensitivity.

Include only `public` and `internal` tasks and material in default local views.
Exclude `confidential`, `restricted`, missing, unknown, and malformed sensitivity
before rendering, counting, sorting, or hashing. Never infer that `public` means
permission to publish. Never copy task bodies or file contents into a view.
