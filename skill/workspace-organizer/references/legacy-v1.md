# Historical v1 boundary

The repository previously used one `TASK.md` per task, a JSON configuration,
catalog files, and an optional static dashboard. Those files remain available so
old checkouts and migration tooling can be inspected, but they are not the
clean-slate source model.

For new work, use `scripts/clean_slate.py` and schema version 2 records. Do not
silently convert a v1 record: inspect it, make a deliberate migration plan, and
keep the original until the new canonical note has been validated. A v1 dashboard
or generated index must never be treated as evidence about a v2 Task.
