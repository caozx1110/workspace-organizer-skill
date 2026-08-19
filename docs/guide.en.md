# Workspace Organizer v1 — English guide

This guide describes the public v1 package. The normative behavior remains in
the [workspace model](workspace-model.md), and detailed CLI guarantees remain in
the skill's [tooling reference](../skill/workspace-organizer/references/tooling.md).

<!-- coverage:purpose -->
## Purpose

`workspace-organizer` keeps canonical `TASK.md` records, reusable materials,
generated TODO/timeline/material views, and verified archives in one durable
local workspace. It supports different task families through shared metadata
and one lifecycle rather than task-specific workflows.

The safety sequence for every structural change is:

```text
scan -> proposal -> dry-run -> exact approval -> apply -> verify
```

A scan or proposal is not permission to write. `approve --yes` records an
explicit decision bound to the exact plan bytes; changing the plan invalidates
that approval. The default is no overwrite, no deletion, no publication, and no
inferred ownership.

<!-- coverage:prerequisites -->
## Prerequisites

- Python 3.9 or later; the package has no third-party Python dependencies.
- macOS or Linux with the atomic no-replace/exchange filesystem operations used
  by the safe apply pipeline. Unsupported POSIX filesystems fail closed.
- A local clone of this repository for installation and repository validation.
- An existing consumer repository root when installing at repository scope.
- User-selected absolute paths for the workspace and a private plan directory.
  Plan/approval/evidence files can contain metadata and hashes; keep them local.

Commands below use these shell variables. Set them to real existing locations
before running an operational example:

```sh
export WO_DISTRIBUTION_ROOT="$PWD"
export WO_CONSUMER_REPO="/absolute/path/to/consumer-repository"
export WO_ROOT="/absolute/path/to/workspace"
export WO_PLAN_ROOT="/absolute/path/to/private-plan-directory"
export WO_TOOL="$WO_CONSUMER_REPO/.agents/skills/workspace-organizer/scripts/workspace_organizer.py"
```

<!-- coverage:installation -->
## Install and discover the skill

From the distribution checkout, inspect the exact no-replace destination, then
confirm the copy:

```sh
python3 scripts/install_skill.py --target-root "$WO_CONSUMER_REPO"
python3 scripts/install_skill.py --target-root "$WO_CONSUMER_REPO" --yes
```

The installer creates
`$WO_CONSUMER_REPO/.agents/skills/workspace-organizer` and refuses an existing
destination or a symlinked package. It never updates, replaces, or removes an
installed skill. Source and target traversal is descriptor-anchored and
no-follow. A random mode-`0700` staging directory is created directly under
`.agents`, outside the scanned `.agents/skills` directory; a complete verified
copy is published across those directories with one atomic no-replace rename.
A race or partial failure exposes no final destination. The installer never
recursively deletes staging: failed evidence remains quarantined outside the
skill scan at `.agents/.workspace-organizer.install-<random>`. Inspect that
directory manually before removing it; a retry uses a new random name. If a
post-rename identity or parent check fails, the current canonical entry is
atomically moved back to that non-scanned quarantine without deletion; an
unreconcilable move is reported as an unknown publish state. Codex scans
repository `.agents/skills` locations as described by the
[official OpenAI skill documentation](https://learn.chatgpt.com/docs/build-skills#where-to-save-skills).
Restart Codex only if a newly installed skill does not appear.

A confirmed install normally reports `installed`. If the atomic rename commits
but parent-directory durability cannot be confirmed, the installer revalidates
the canonical destination and reports `installed-with-durability-warning` as a
successful install that should be checked again after a system restart.

This is a local/repository-scoped installation, not a release or publication.
The official documentation recommends a plugin for broader reusable
distribution; plugin packaging is outside v1 and outside this repository gate.

<!-- coverage:concepts -->
## Workspace concepts

The canonical inputs are `.workspace-organizer/config.json` and one root
`TASK.md` per registered task. `00_总览/` and the catalog JSON files are derived
views, never a second source of truth.

| Path | Role |
| --- | --- |
| `00_总览/` | Generated TODO, timeline, and material projections |
| `10_收件箱/` | Unclassified, restricted-by-default incoming content |
| `20_任务/` | Stable active task bundles |
| `30_资料库/` | Reusable non-task materials |
| `90_归档/` | Verified terminal task bundles |
| `99_待整理/` | Content awaiting an explicit decision |
| `.workspace-organizer/` | Local configuration, plans, catalogs, and evidence |

Task IDs are stable lowercase ASCII slugs. Metadata updates do not move a task;
the only normal bundle move is a verified archive. Default views include only
`public` and `internal` inputs and filter `confidential`, `restricted`, unknown,
or malformed sensitivity before rendering, counting, sorting, or hashing.

<!-- coverage:safety -->
## Safe operating model

1. Run `inventory` or `scan`; both are read-only.
2. Create an immutable `plan-init`, `plan-organize`, or `plan-archive` file.
3. Run `dry-run` and review every intended mutation, collision, and skipped
   boundary.
4. Only after a person accepts the exact plan, create a separate approval with
   `approve --yes`.
5. Run `apply`, then require `verify` to report verified evidence.

Never reuse an approval with edited plan bytes. Never bypass the CLI with ad hoc
moves. A stale source, destination collision, symlink, nested Git boundary,
unmarked generated view, or incomplete prior operation stops the pipeline.

<!-- coverage:initialize -->
## Initialize a new workspace

Create the empty workspace and private plan directory yourself. Keep the
initialization plan outside the not-yet-initialized workspace. Then run:

```sh
python3 "$WO_TOOL" inventory "$WO_ROOT"
python3 "$WO_TOOL" plan-init "$WO_ROOT" --workspace-id example-workspace --output "$WO_PLAN_ROOT/init.json"
python3 "$WO_TOOL" dry-run "$WO_ROOT" --plan "$WO_PLAN_ROOT/init.json"
python3 "$WO_TOOL" approve --plan "$WO_PLAN_ROOT/init.json" --output "$WO_PLAN_ROOT/init.approval.json" --yes
python3 "$WO_TOOL" apply "$WO_ROOT" --plan "$WO_PLAN_ROOT/init.json" --approval "$WO_PLAN_ROOT/init.approval.json"
python3 "$WO_TOOL" verify "$WO_ROOT" --plan "$WO_PLAN_ROOT/init.json"
```

Choose a stable 3–64 character lowercase workspace ID. New records default to
`internal` sensitivity unless the user explicitly selects another value.

<!-- coverage:adoption -->
## Adopt an existing workspace in place

Inventory first. A person must select each legacy task and material root;
adoption does not infer ownership, rename, move, normalize, or delete existing
content. Existing managed entries must be accepted explicitly. For example:

```sh
python3 "$WO_TOOL" inventory "$WO_ROOT"
python3 "$WO_TOOL" plan-init "$WO_ROOT" --workspace-id adopted-workspace --adopt-task "Existing Projects/研究 α" --adopt-material "Legacy Library/资料 with spaces=internal" --accept-existing-managed "10_收件箱" --output "$WO_PLAN_ROOT/adopt.json"
python3 "$WO_TOOL" dry-run "$WO_ROOT" --plan "$WO_PLAN_ROOT/adopt.json"
python3 "$WO_TOOL" approve --plan "$WO_PLAN_ROOT/adopt.json" --output "$WO_PLAN_ROOT/adopt.approval.json" --yes
python3 "$WO_TOOL" apply "$WO_ROOT" --plan "$WO_PLAN_ROOT/adopt.json" --approval "$WO_PLAN_ROOT/adopt.approval.json"
python3 "$WO_TOOL" verify "$WO_ROOT" --plan "$WO_PLAN_ROOT/adopt.json"
```

Before planning, each selected task root must have one valid `TASK.md`. Keep
unselected or uncertain content unmanaged and in place.

<!-- coverage:task-updates -->
## Create or update a task

Start from the installed `assets/TASK.md`, replace every example fact, and put
one record at the registered bundle root. Preserve the stable ID and bundle
path, follow the shared lifecycle, advance `updated` for every semantic edit,
and validate the whole workspace before and after the edit:

```sh
python3 scripts/validate_workspace_model.py "$WO_ROOT"
```

There is no v1 task-edit command. A record edit is an intentional user/agent
text edit, not permission to restructure files. Invalid or duplicate records
block generation rather than being guessed into shape.

<!-- coverage:views -->
## Generate local views

Regenerate all three JSON catalogs and three Markdown projections atomically:

```sh
python3 "$WO_TOOL" index "$WO_ROOT"
```

An unchanged input produces byte-identical views. The command replaces only
files carrying the matching v1 generated marker; an unmarked overview is
user-owned and blocks generation. Task bodies and file contents are never copied
into views, and sensitive metadata is filtered before hashes or counts are made.

<!-- coverage:archive -->
## Archive a terminal task

Only a valid `completed` or `cancelled` task with no pending/unassigned bundle
content is eligible. The sole destination is
`90_归档/<closed-year>/<area>/<task-id>/`. Review and approve the exact plan:

```sh
python3 "$WO_TOOL" plan-archive "$WO_ROOT" --task-id example-task --archived-at "2026-08-19T14:00:00+08:00" --output "$WO_PLAN_ROOT/archive.json"
python3 "$WO_TOOL" dry-run "$WO_ROOT" --plan "$WO_PLAN_ROOT/archive.json"
python3 "$WO_TOOL" approve --plan "$WO_PLAN_ROOT/archive.json" --output "$WO_PLAN_ROOT/archive.approval.json" --yes
python3 "$WO_TOOL" apply "$WO_ROOT" --plan "$WO_PLAN_ROOT/archive.json" --approval "$WO_PLAN_ROOT/archive.approval.json"
python3 "$WO_TOOL" verify "$WO_ROOT" --plan "$WO_PLAN_ROOT/archive.json"
```

The verified archive updates the canonical record and moves the whole bundle
once. It never overwrites an existing destination.

<!-- coverage:rollback -->
## Rollback and failure expectations

V1 records immutable intent, write-ahead stages, pre/post hashes, prior task
bytes, and configuration transitions. Index generation automatically restores
the prior six-file set when a controlled write fails. Structural apply is
idempotent only after a verified success.

There is intentionally no generic automatic rollback command. After an
interruption, failed verification, or incomplete intent, stop; preserve the
workspace, plan, approval, WAL, verification evidence, and both sides of a
partial copy. Do not delete a source or overwrite a destination as recovery.
Use the exact retained evidence for bounded manual recovery and prepare a new
plan only after the prior state is reconciled. An erroneous archive may be
reversed only as a verified rollback of that exact archive operation.

<!-- coverage:limits -->
## Known limits

- V1 is local and filesystem-based; it does not publish, sync, schedule,
  deduplicate, OCR, embed content, or provide a database/web service.
- The hidden control directory is organizational, not an access-control
  boundary. Filesystem permissions remain the user's responsibility.
- Inventory never follows symlinks or enters nested Git repositories. Compressed
  originals are metadata-only unless bounded listing is separately confirmed;
  the tool never extracts them.
- Large-file hashes may be deferred during scan. Default views still hash only
  eligible visible material when building the material catalog.
- V1 has no HTML dashboard and needs none to initialize, adopt, update tasks,
  generate views, or archive. Issue #7 owns a later read-only static consumer of
  the same canonical, sensitivity-filtered data; it must not become editable or
  authoritative.

<!-- coverage:validation -->
## Validate the distribution

From a clean repository checkout with Python 3.9 or later, these commands are
self-contained and make no network requests:

```sh
python3 scripts/check_public_content.py
python3 scripts/forward_test_distribution.py
python3 scripts/run_release_gate.py
```

The gate runs the model validator, focused package/model/tooling tests, scenario
matrix, distribution tests, full suite, public-content/link hygiene, and an
isolated installed-package forward test. To add the official `skill-creator`
quick validator, point to a separately available `skill-creator` package using
an environment variable; no developer-machine path is assumed:

```sh
python3 scripts/run_release_gate.py --skill-creator-root "$SKILL_CREATOR_ROOT"
```

Prerequisite for the last command: `SKILL_CREATOR_ROOT` names a directory that
contains `scripts/quick_validate.py`. The gate does not tag, release, publish,
modify rulesets, or operate on a user workspace.
