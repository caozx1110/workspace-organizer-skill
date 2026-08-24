# Clean-slate user guide

Obsidian is the daily cockpit, Chat/Agent is the operation layer, and Markdown is
the only source of truth. Most days you need only a handful of pages.

## The daily reading loop

1. Open `01_导航/HOME.md`, then `00_总览/TODAY.md`.
2. Keep one to three task links in the user-owned `01_导航/FOCUS.md`.
3. Act on the `Next` lines in TODAY; open `NEXT.md` only for the complete queue.
4. Check `WAITING.md` for external dependencies and `INBOX.md` for a small batch of new input.
5. Explicitly complete/cancel outcomes with a result in the evening; update the next action for unfinished work.

| Page | Owner | Purpose |
| --- | --- | --- |
| `HOME.md` | human | stable entry point and habits |
| `FOCUS.md` | human | today's one to three priorities |
| `TODAY.md` | generator | focus, overdue, scheduled, next actions and Inbox signal |
| `NEXT.md` | generator | every open Task |
| `INBOX.md` | generator | untriaged Captures |
| `WAITING.md` | generator | waiting/blocked work and follow-ups |
| `ARCHIVE_INDEX.md` | generator | readable navigation of closed bundles |

## CLI examples

```sh
WO=skill/workspace-organizer/scripts/clean_slate.py
python3 "$WO" init /path/to/vault                 # inspect the preview first
python3 "$WO" init /path/to/vault --yes
python3 "$WO" task create /path/to/vault --title "Renew passport" --outcome "Receive the receipt" --yes
python3 "$WO" capture create /path/to/vault --text "Supplier sent a contract" --yes
python3 "$WO" views generate /path/to/vault
python3 "$WO" views export /path/to/vault --profile internal --output /path/to/share
```

A Capture is not a Task. Triage, attachment, library promotion, archive, and
restore are structural operations: preview the exact plan, approve that plan, apply
it, and verify the result. An uncertain owner or sensitivity stays in Inbox.

Humans may edit Task bodies and HOME/FOCUS directly. Agent metadata edits use CAS,
so an Obsidian save made after the read becomes a visible conflict instead of a
last-write-wins overwrite. Generated pages replace only their own marker; HOME and
FOCUS are never replaced. The private cockpit includes valid records through
`restricted`; only explicit `views export` applies a sensitivity profile before
counting, sorting, or rendering. Never publish the cockpit directory itself.

`sensitivity` describes disclosure harm, while `agent_access` controls Agent
reading. `none` returns a minimal stub with a `[restricted]` title; `metadata`
(the Task/Capture default) allows scheduling and lifecycle updates but not the
body; `content` is required for `task show --include-body`. Artifacts default to
`none` and require their own `content` grant even when the owning Task grants
content. Raising access requires explicit human authorization:

```sh
python3 "$WO" task update /path/to/vault --task-id ID --agent-access content --authorize-access --actor human
python3 "$WO" artifact update-access /path/to/vault --artifact-id ID --agent-access content --authorize-access --actor human
```

Sensitivity is chosen explicitly by the human or an accepted workspace rule;
it is not guessed from body keywords. If owner, purpose, or sensitivity is
uncertain, leave the input in Inbox as `restricted` until a human confirms it.
