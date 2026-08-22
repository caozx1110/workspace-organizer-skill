# Read-only static dashboard

The dashboard is an optional v2 projection over the stable v1 canonical task and
generated-view interfaces. It is derived and disposable. Its HTML, manifest,
styles, and application script never become task state or operation evidence.

## Refresh in one direction

First regenerate the six v1 views, then generate the dashboard from the installed
skill package:

```sh
python3 scripts/workspace_organizer.py index WORKSPACE
python3 scripts/workspace_dashboard.py generate WORKSPACE
python3 scripts/workspace_dashboard.py verify WORKSPACE
```

The renderer writes only `.workspace-organizer/dashboard/`. It refuses symlinks,
unknown entries, and unmarked collisions. It recomputes the v1 views from
canonical configuration and `TASK.md` records, then requires the on-disk TODO and
timeline catalogs to match those bytes exactly. A hand-edited, stale, or forged
catalog therefore cannot become dashboard authority.

`verify` exits successfully only when the canonical inputs, v1 catalogs,
manifest, and all four dashboard outputs agree. The visible source fingerprint is
derived only from sensitivity-filtered v1 catalogs and contains no wall-clock
value, so unchanged inputs produce byte-identical output. Run `verify` to decide
whether a local snapshot is stale; do not infer freshness from file dates.

## Keep it local and read-only

- Only `public` and `internal` open-task metadata may reach the view model.
  Unknown, missing, malformed, `confidential`, and `restricted` sensitivity fails
  closed before rendering, counting, grouping, or dashboard hashing.
- Every task link is a percent-encoded workspace-relative link to its canonical
  `TASK.md`. Task bodies and material content are never copied.
- Workspace-derived text is normalized, control and bidi formatting characters
  are neutralized, and HTML text, attributes, and URLs are escaped by context.
- The static application performs local tab switching and TODO priority filtering
  only. It has no form, editable content, fetch, database, service worker,
  telemetry, mutation endpoint, approval action, or archive action.
- The page uses external packaged CSS and JavaScript under a restrictive CSP;
  workspace data is never placed in a script context.

Deleting `.workspace-organizer/dashboard/`, or never generating it, does not
affect initialization, adoption, task records, indexing, approval, verification,
or archive. Run the v1 CLI directly whenever dashboard assets are unavailable.
