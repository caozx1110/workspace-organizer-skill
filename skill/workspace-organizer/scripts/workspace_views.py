#!/usr/bin/env python3
"""Clean-slate Markdown projections for the workspace organizer.

This module deliberately has no dependency on the v1 runtime.  The public
surface is small so that the task/capture model can evolve independently:

``collect_records(root)``
    Read task and capture front matter from a workspace and return ordinary
    Python mappings.  It is a convenience adapter, not a second source of
    truth.

``build_views(tasks, captures, ...)``
    Pure, deterministic rendering.  No files are touched.

``write_views(root, bundle)``
    Safely commit all five generated pages.  Existing files must carry this
    module's marker; unmarked files are user-owned and are never overwritten.
    All rendering happens before the first replacement and a rollback is
    attempted if a replacement fails.

``generate_views(root, now=None, profile="internal", focus_ids=())``
    The normal end-to-end entry point used by the CLI.

The generated pages are projections only.  They contain links and summaries,
never canonical task bodies or hidden sensitivity records.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence, Union
from urllib.parse import quote


SCHEMA_VERSION = 2
VIEW_NAMES = ("TODAY", "NEXT", "INBOX", "WAITING", "ARCHIVE_INDEX")
VIEW_RELATIVE_PATHS = {name: f"00_总览/{name}.md" for name in VIEW_NAMES}
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")
SENSITIVITY_RANK = {name: i for i, name in enumerate(SENSITIVITY_ORDER)}
PRIORITY_ORDER = ("urgent", "high", "normal", "low")
PRIORITY_RANK = {name: i for i, name in enumerate(PRIORITY_ORDER)}
OPEN_STATUSES = {"planned", "active", "waiting", "blocked"}
CLOSED_STATUSES = {"completed", "cancelled"}
ARCHIVED_STORAGE = "archived"

_MARKER_RE = re.compile(
    rb"^<!-- workspace-organizer:generated view=([A-Z_]+) schema=(\d+) "
    rb"source_sha256=([0-9a-f]{64}) profile=([a-z]+) -->\r?\n?$"
)
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_PATH_BAD_RE = re.compile(r"(?:^|/)\.\.?(?:/|$)")


class ViewError(ValueError):
    """A view cannot be generated or committed safely."""


class UserOwnedViewError(ViewError):
    """An output path exists without the exact generated-view marker."""


class ViewCommitError(ViewError):
    """A multi-file commit failed and was rolled back (or needs reconcile)."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> Any:
    """Return a JSON-safe, order-independent canonical value.

    Input list order is not semantically meaningful for the record snapshots;
    sorting list members prevents discovery order from affecting a source
    digest while preserving scalar values exactly.
    """

    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=lambda k: str(k))}
    if isinstance(value, (list, tuple, set, frozenset)):
        members = [_canonical(item) for item in value]
        return sorted(members, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Dates and datetimes are accepted by the convenience API but must hash
    # as their stable textual representation.
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _text(value: Any, field: str, *, required: bool = False, maximum: int = 500) -> Optional[str]:
    if value is None:
        if required:
            raise ViewError(f"{field}: required")
        return None
    if not isinstance(value, str):
        raise ViewError(f"{field}: must be text")
    value = _nfc(value).strip()
    if required and not value:
        raise ViewError(f"{field}: must not be empty")
    if len(value) > maximum or "\n" in value or "\r" in value:
        raise ViewError(f"{field}: invalid single-line text")
    return value


def _id(value: Any, field: str = "id") -> str:
    result = _text(value, field, required=True, maximum=128)
    assert result is not None
    if not _ID_RE.fullmatch(result):
        # Task IDs are normally slugged.  Capture IDs may contain an
        # underscore, but still must be safe in links and stable in sorting.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", result):
            raise ViewError(f"{field}: unsafe identifier")
    return result


def _path(value: Any, field: str = "record_path") -> str:
    result = _text(value, field, required=True, maximum=1024)
    assert result is not None
    if result.startswith("/") or "\\" in result or "\x00" in result or "//" in result or _PATH_BAD_RE.search(result):
        raise ViewError(f"{field}: must be a workspace-relative POSIX path")
    try:
        pure = PurePosixPath(result)
    except Exception as exc:  # pragma: no cover - defensive
        raise ViewError(f"{field}: invalid path") from exc
    if pure.is_absolute() or str(pure) != result:
        raise ViewError(f"{field}: must be normalized")
    return result


def _date(value: Any, field: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        value = value.date().isoformat()
    elif isinstance(value, date):
        value = value.isoformat()
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ViewError(f"{field}: must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ViewError(f"{field}: invalid date") from exc
    return value


def _timestamp(value: Any, field: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ViewError(f"{field}: datetime must be timezone-aware")
        value = value.isoformat(timespec="seconds")
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise ViewError(f"{field}: must be RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ViewError(f"{field}: invalid timestamp") from exc
    return value


def _sensitivity(value: Any, field: str = "sensitivity") -> str:
    if value not in SENSITIVITY_RANK:
        raise ViewError(f"{field}: unknown sensitivity")
    return str(value)


def _profile_rank(profile: Any) -> tuple[str, int]:
    if profile is None:
        profile = "internal"
    if isinstance(profile, Mapping):
        profile = profile.get(
            "max_sensitivity",
            profile.get("view_max_sensitivity", profile.get("sensitivity", "internal")),
        )
    if not isinstance(profile, str) or profile not in SENSITIVITY_RANK:
        raise ViewError("profile: must be public, internal, confidential, or restricted")
    return profile, SENSITIVITY_RANK[profile]


def _visible(raw: Mapping[str, Any], profile_rank: int) -> bool:
    # This is intentionally the first field accessed after the mapping check.
    # A hidden record's title/path/metadata is never inspected or counted.
    sensitivity = _sensitivity(raw.get("sensitivity"))
    return SENSITIVITY_RANK[sensitivity] <= profile_rank


def _record_path(raw: Mapping[str, Any], *, archived: bool = False) -> str:
    explicit = raw.get("record_path", raw.get("path"))
    if explicit:
        return _path(explicit, "record_path")
    task_id = _id(raw.get("id", raw.get("task_id")), "id")
    if archived:
        area = _slugish(raw.get("area", "general"))
        # ``closed_at`` is an RFC3339 timestamp in the canonical task schema;
        # accepting a date here as well keeps the adapter useful for imported
        # records without making the path depend on local formatting.
        closed_raw = raw.get("closed_at")
        closed = str(closed_raw or "")
        year = closed[:4] if re.fullmatch(r"\d{4}", closed[:4]) else "undated"
        return f"90_归档/{area}/{year}/{task_id}/{task_id}.md"
    return f"20_任务/{task_id}/{task_id}.md"


def _slugish(value: Any) -> str:
    text = _text(value, "area", required=True, maximum=128)
    assert text is not None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text):
        # Display labels are not safe as path components; use a deterministic
        # fallback rather than silently introducing traversal.
        return "general"
    return text


def _normalize_task(raw: Mapping[str, Any]) -> dict[str, Any]:
    kind = raw.get("kind", "task")
    if kind != "task":
        raise ViewError("task record: kind must be task")
    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise ViewError("task record: unsupported schema_version")
    task_id = _id(raw.get("id", raw.get("task_id")), "task.id")
    status = _text(raw.get("status"), "task.status", required=True, maximum=32)
    assert status is not None
    if status not in OPEN_STATUSES | CLOSED_STATUSES:
        raise ViewError(f"task {task_id}: unknown status")
    storage_state = raw.get("storage_state")
    if storage_state is None:
        storage_state = "archived" if status == "archived" else "active"
    if storage_state not in {"active", "archived"}:
        raise ViewError(f"task {task_id}: unknown storage_state")
    archived_at = _timestamp(raw.get("archived_at"), f"task {task_id}.archived_at")
    if status in OPEN_STATUSES and storage_state != "active":
        raise ViewError(f"task {task_id}: open task cannot be archived")
    if status in CLOSED_STATUSES and storage_state == "archived" and archived_at is None:
        raise ViewError(f"task {task_id}: archived task requires archived_at")
    title = _text(raw.get("title", raw.get("name")), f"task {task_id}.title", required=True, maximum=300)
    area = _text(raw.get("area"), f"task {task_id}.area", required=True, maximum=128)
    typ = _text(raw.get("type"), f"task {task_id}.type", required=False, maximum=128) or "general"
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", area or ""):
        raise ViewError(f"task {task_id}: area must be a lowercase key")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", typ):
        raise ViewError(f"task {task_id}: type must be a lowercase key")
    priority = _text(raw.get("priority"), f"task {task_id}.priority", required=False, maximum=32) or "normal"
    if priority not in PRIORITY_RANK:
        raise ViewError(f"task {task_id}: unknown priority")
    sensitivity = _sensitivity(raw.get("sensitivity"), f"task {task_id}.sensitivity")
    scheduled_on = _date(raw.get("scheduled_on", raw.get("scheduled")), f"task {task_id}.scheduled_on")
    due_on = _date(raw.get("due_on", raw.get("due")), f"task {task_id}.due_on")
    follow_up_on = _date(raw.get("follow_up_on", raw.get("follow_up")), f"task {task_id}.follow_up_on")
    next_action = _text(raw.get("next_action"), f"task {task_id}.next_action", maximum=1000)
    waiting_on = _text(raw.get("waiting_on"), f"task {task_id}.waiting_on", maximum=300)
    closure_summary = _text(raw.get("closure_summary"), f"task {task_id}.closure_summary", maximum=2000)
    closed_at = _timestamp(raw.get("closed_at"), f"task {task_id}.closed_at")
    record_path = _record_path(raw, archived=(storage_state == "archived" or status == "archived"))
    if PurePosixPath(record_path).stem != task_id:
        raise ViewError(f"task {task_id}: canonical note filename must equal task id")
    if status in OPEN_STATUSES and not next_action:
        raise ViewError(f"task {task_id}: open task requires next_action")
    if status in OPEN_STATUSES and (closed_at is not None or closure_summary is not None or archived_at is not None):
        raise ViewError(f"task {task_id}: open task has closure metadata")
    if status in CLOSED_STATUSES:
        if not closed_at:
            raise ViewError(f"task {task_id}: closed task requires closed_at")
        if not closure_summary:
            raise ViewError(f"task {task_id}: closed task requires closure_summary")
        if next_action is not None:
            raise ViewError(f"task {task_id}: closed task next_action must be null")
        if storage_state == "active" and archived_at is not None:
            raise ViewError(f"task {task_id}: active task has archived_at")
    result = dict(raw)
    result.update(
        id=task_id,
        title=title,
        status=status,
        storage_state=storage_state,
        area=area,
        type=typ,
        priority=priority,
        sensitivity=sensitivity,
        scheduled_on=scheduled_on,
        due_on=due_on,
        follow_up_on=follow_up_on,
        next_action=next_action,
        waiting_on=waiting_on,
        closure_summary=closure_summary,
        closed_at=closed_at,
        record_path=record_path,
    )
    return result


def _normalize_capture(raw: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("kind", "capture") != "capture":
        raise ViewError("capture record: kind must be capture")
    if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ViewError("capture record: unsupported schema_version")
    capture_id = _id(raw.get("id", raw.get("capture_id", raw.get("artifact_id"))), "capture.id")
    title = _text(raw.get("title", raw.get("summary", raw.get("name"))), f"capture {capture_id}.title", required=False, maximum=300)
    path_value = raw.get("path", raw.get("payload_path", raw.get("record_path")))
    path_value = _path(path_value, f"capture {capture_id}.path") if path_value else None
    captured_at = raw.get("captured_at", raw.get("created_at"))
    if isinstance(captured_at, (date, datetime)):
        captured_at = captured_at.isoformat()
    elif captured_at is not None:
        captured_at = _text(captured_at, f"capture {capture_id}.captured_at", maximum=128)
    state = raw.get("triage_state", raw.get("state", raw.get("status", "inbox")))
    state = _text(state, f"capture {capture_id}.triage_state", required=True, maximum=32)
    assert state is not None
    if state not in {"inbox", "pending", "untriaged", "captured", "deferred", "triaged"}:
        raise ViewError(f"capture {capture_id}: unknown triage state")
    sensitivity = _sensitivity(raw.get("sensitivity"), f"capture {capture_id}.sensitivity")
    result = dict(raw)
    result.update(id=capture_id, title=title or (Path(path_value).stem if path_value else capture_id), path=path_value, captured_at=captured_at, triage_state=state, sensitivity=sensitivity)
    return result


def _is_inbox(capture: Mapping[str, Any]) -> bool:
    return capture.get("triage_state", "inbox").lower() in {"inbox", "pending", "untriaged", "captured"}


def _safe_focus_ids(focus_ids: Optional[Iterable[str]]) -> tuple[str, ...]:
    if focus_ids is None:
        return ()
    result: list[str] = []
    for value in focus_ids:
        value = _id(value, "focus id")
        if value not in result:
            result.append(value)
    if len(result) > 3:
        raise ViewError("focus_ids: at most three focus tasks are allowed")
    return tuple(sorted(result))


def _normalize_today(value: Any) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    result = _date(value, "now")
    assert result is not None
    return result


def _task_sort_key(task: Mapping[str, Any]) -> tuple[Any, ...]:
    due = task.get("due_on") or "9999-12-31"
    scheduled = task.get("scheduled_on") or "9999-12-31"
    return (PRIORITY_RANK.get(str(task.get("priority")), len(PRIORITY_RANK)), due, scheduled, str(task.get("area", "")).casefold(), str(task.get("title", "")).casefold(), str(task.get("id")))


def _archive_sort_key(task: Mapping[str, Any]) -> tuple[Any, ...]:
    closed = str(task.get("closed_at") or "9999-12-31")[:10]
    return (str(task.get("area", "")).casefold(), closed, str(task.get("type", "")).casefold(), str(task.get("title", "")).casefold(), str(task.get("id")))


def _md_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _link(root_output: str, record_path: str, label: str) -> str:
    # All paths have already been validated as workspace-relative.  Quote only
    # URL-reserved characters while retaining POSIX separators for Obsidian.
    base = PurePosixPath(root_output).parent
    rel = PurePosixPath(os.path.relpath(record_path, base.as_posix()).replace(os.sep, "/"))
    href = quote(rel.as_posix(), safe="/-._~:@")
    label = str(label).replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({href})"


def _marker(view: str, source_sha256: str, profile: str) -> str:
    return f"<!-- workspace-organizer:generated view={view} schema={SCHEMA_VERSION} source_sha256={source_sha256} profile={profile} -->"


def _page(view: str, source_sha256: str, profile: str, body: str) -> bytes:
    return (_marker(view, source_sha256, profile) + "\n# " + view + "\n\n" + body.rstrip() + "\n").encode("utf-8")


def _grouped_tasks(tasks: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for task in tasks:
        groups[str(task.get("area") or "general")].append(task)
    return {key: sorted(groups[key], key=_task_sort_key) for key in sorted(groups, key=str.casefold)}


def _task_line(task: Mapping[str, Any], *, output_path: str = "00_总览/TODAY.md", include_status: bool = True, include_next: bool = True, area_labels: Optional[Mapping[str, str]] = None) -> str:
    title = _md_escape(task["title"])
    link = _link(output_path, task["record_path"], title)
    bits: list[str] = []
    if include_status:
        bits.append(f"`{_md_escape(task['status'])}`")
    bits.append(f"priority `{_md_escape(task['priority'])}`")
    if task.get("scheduled_on"):
        bits.append(f"scheduled {task['scheduled_on']}")
    if task.get("due_on"):
        bits.append(f"due {task['due_on']}")
    area = str(task.get("area") or "general")
    if area_labels and area in area_labels:
        bits.append(f"area `{_md_escape(area_labels[area])}`")
    line = "- " + link + " — " + ", ".join(bits)
    if include_next and task.get("next_action"):
        line += "\n  - Next: " + _md_escape(task["next_action"])
    return line


def _render_today(tasks: Sequence[Mapping[str, Any]], captures: Sequence[Mapping[str, Any]], today: str, focus_ids: Sequence[str], area_labels: Optional[Mapping[str, str]], source: str, profile: str) -> bytes:
    active = [t for t in tasks if t.get("storage_state") == "active" and t.get("status") in OPEN_STATUSES]
    focus_set = set(focus_ids)
    focus = [t for t in active if t["id"] in focus_set]
    focus_ids_seen = {t["id"] for t in focus}
    actionable_active = [t for t in active if t.get("status") not in {"waiting", "blocked"}]
    overdue = [t for t in actionable_active if t["id"] not in focus_ids_seen and t.get("due_on") and t["due_on"] < today]
    overdue_ids = {t["id"] for t in overdue}
    scheduled = [t for t in actionable_active if t["id"] not in (focus_ids_seen | overdue_ids) and t.get("scheduled_on") == today]
    scheduled_ids = {t["id"] for t in scheduled}
    due_today = [t for t in actionable_active if t["id"] not in (focus_ids_seen | overdue_ids | scheduled_ids) and t.get("due_on") == today]
    waiting_followup = [t for t in active if t.get("status") in {"waiting", "blocked"} and t.get("follow_up_on") and t["follow_up_on"] <= today]
    # Focus is user-selected; the actionable section is intentionally bounded
    # so TODAY does not become a dump of the entire NEXT queue.
    excluded = {t["id"] for t in focus + overdue + due_today + scheduled + waiting_followup}
    actionable = [t for t in sorted(active, key=_task_sort_key) if t["id"] not in excluded and t.get("status") not in {"waiting", "blocked"}][:5]
    sections: list[str] = []
    for heading, records in (
        ("Focus", sorted(focus, key=_task_sort_key)),
        ("Overdue", sorted(overdue, key=_task_sort_key)),
        ("Scheduled today", sorted(scheduled, key=_task_sort_key)),
        ("Due today", sorted(due_today, key=_task_sort_key)),
        ("Next actions", actionable),
        ("Waiting follow-up", sorted(waiting_followup, key=_task_sort_key)),
    ):
        sections.append(f"## {heading}\n\n")
        sections.append("\n".join(_task_line(t, area_labels=area_labels) for t in records) if records else "_None._")
        sections.append("\n")
    sections.append("## Inbox\n\n")
    sections.append(f"{len(captures)} item(s) awaiting triage." if captures else "_Inbox is clear._")
    sections.append("\n\n")
    sections.append(f"_Date: {today}. Generated from visible canonical records._")
    return _page("TODAY", source, profile, "".join(sections))


def _render_next(tasks: Sequence[Mapping[str, Any]], area_labels: Optional[Mapping[str, str]], source: str, profile: str) -> bytes:
    active = [t for t in tasks if t.get("storage_state") == "active" and t.get("status") in OPEN_STATUSES]
    sections: list[str] = []
    for area, records in _grouped_tasks(active).items():
        label = area_labels.get(area, area) if area_labels else area
        sections.extend([f"## {_md_escape(label)}\n\n", "\n".join(_task_line(t, output_path="00_总览/NEXT.md", area_labels=None) for t in records) or "_None._", "\n\n"])
    if not sections:
        sections.append("_No open tasks._\n\n")
    sections.append("_All open tasks are shown here; TODAY is intentionally bounded._")
    return _page("NEXT", source, profile, "".join(sections))


def _render_inbox(captures: Sequence[Mapping[str, Any]], source: str, profile: str) -> bytes:
    rows: list[str] = []
    ordered = sorted(captures, key=lambda c: (str(c.get("captured_at") or "9999"), str(c.get("title", "")).casefold(), str(c.get("id"))))
    for capture in ordered:
        label = _md_escape(capture.get("title") or capture["id"])
        if capture.get("path"):
            link = _link("00_总览/INBOX.md", capture["path"], label)
        else:
            link = label
        bits = [f"`{_md_escape(capture['id'])}`"]
        if capture.get("captured_at"):
            bits.append(_md_escape(capture["captured_at"]))
        rows.append(f"- {link} — " + ", ".join(bits))
    body = "\n".join(rows) if rows else "_Inbox is clear._"
    return _page("INBOX", source, profile, body)


def _render_waiting(tasks: Sequence[Mapping[str, Any]], source: str, profile: str) -> bytes:
    waiting = [t for t in tasks if t.get("storage_state") == "active" and t.get("status") in {"waiting", "blocked"}]
    rows: list[str] = []
    for task in sorted(waiting, key=lambda t: (str(t.get("follow_up_on") or "9999-12-31"), _task_sort_key(t))):
        line = _task_line(task, output_path="00_总览/WAITING.md", include_next=False)
        if task.get("waiting_on"):
            line += "\n  - Waiting on: " + _md_escape(task["waiting_on"])
        if task.get("follow_up_on"):
            line += "\n  - Follow up: " + _md_escape(task["follow_up_on"])
        rows.append(line)
    return _page("WAITING", source, profile, "\n\n".join(rows) if rows else "_Nothing is waiting._")


def _render_archive(tasks: Sequence[Mapping[str, Any]], area_labels: Optional[Mapping[str, str]], source: str, profile: str) -> bytes:
    archived = [t for t in tasks if t.get("storage_state") == "archived" or t.get("status") == "archived"]
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for task in archived:
        area = str(task.get("area") or "general")
        closed = str(task.get("closed_at") or "")[:4] or "undated"
        groups[(area, closed)].append(task)
    sections: list[str] = []
    for (area, year), records in sorted(groups.items(), key=lambda item: (item[0][0].casefold(), item[0][1])):
        label = area_labels.get(area, area) if area_labels else area
        sections.extend([f"## {_md_escape(label)} / {year}\n\n"])
        for task in sorted(records, key=_archive_sort_key):
            status = _md_escape(task.get("status", "completed"))
            typ = _md_escape(task.get("type", "general"))
            summary = _md_escape(task.get("closure_summary") or "No closure summary.")
            sections.append(f"- {_link('00_总览/ARCHIVE_INDEX.md', task['record_path'], _md_escape(task['title']))} — `{status}`, `{typ}` — {summary}\n")
        sections.append("\n")
    if not sections:
        sections.append("_No archived tasks._\n\n")
    return _page("ARCHIVE_INDEX", source, profile, "".join(sections).rstrip())


def build_views(
    tasks: Iterable[Mapping[str, Any]],
    captures: Iterable[Mapping[str, Any]] = (),
    *,
    now: Any = None,
    profile: Any = "internal",
    focus_ids: Iterable[str] = (),
    area_labels: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Render all five pages without touching the filesystem.

    The returned mapping has ``source_sha256`` and a ``files`` mapping of
    workspace-relative output paths to UTF-8 bytes.  Records above the chosen
    sensitivity profile are discarded before any other field is inspected.
    """

    profile_name, profile_rank = _profile_rank(profile)
    today = _normalize_today(now)
    requested_focus = _safe_focus_ids(focus_ids)

    visible_tasks: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    for raw in tasks:
        if not isinstance(raw, Mapping):
            raise ViewError("task record must be a mapping")
        if not _visible(raw, profile_rank):
            continue
        task = _normalize_task(raw)
        if task["id"] in seen_task_ids:
            raise ViewError(f"duplicate visible task id: {task['id']}")
        seen_task_ids.add(task["id"])
        visible_tasks.append(task)

    visible_captures: list[dict[str, Any]] = []
    seen_capture_ids: set[str] = set()
    for raw in captures:
        if not isinstance(raw, Mapping):
            raise ViewError("capture record must be a mapping")
        if not _visible(raw, profile_rank):
            continue
        capture = _normalize_capture(raw)
        if not _is_inbox(capture):
            continue
        if capture["id"] in seen_capture_ids:
            raise ViewError(f"duplicate visible capture id: {capture['id']}")
        seen_capture_ids.add(capture["id"])
        visible_captures.append(capture)

    # Never retain a focus identifier that is not visible in this profile.  In
    # addition to avoiding a broken link, this prevents a hidden task from
    # influencing the generated digest or serving as a count side channel.
    visible_ids = {task["id"] for task in visible_tasks}
    safe_focus = tuple(task_id for task_id in requested_focus if task_id in visible_ids)

    labels: dict[str, str] = {}
    if area_labels:
        for key, value in area_labels.items():
            labels[str(key)] = _text(value, f"area_labels.{key}", required=True, maximum=200) or str(key)
    source_payload = {
        "schema_version": SCHEMA_VERSION,
        "profile": profile_name,
        "today": today,
        "focus_ids": safe_focus,
        "area_labels": labels,
        "tasks": sorted((_canonical(task) for task in visible_tasks), key=lambda t: str(t.get("id", ""))),
        "captures": sorted((_canonical(capture) for capture in visible_captures), key=lambda c: str(c.get("id", ""))),
    }
    source_sha256 = _sha256_bytes(_canonical_bytes(source_payload))
    files = {
        VIEW_RELATIVE_PATHS["TODAY"]: _render_today(visible_tasks, visible_captures, today, safe_focus, labels, source_sha256, profile_name),
        VIEW_RELATIVE_PATHS["NEXT"]: _render_next(visible_tasks, labels, source_sha256, profile_name),
        VIEW_RELATIVE_PATHS["INBOX"]: _render_inbox(visible_captures, source_sha256, profile_name),
        VIEW_RELATIVE_PATHS["WAITING"]: _render_waiting(visible_tasks, source_sha256, profile_name),
        VIEW_RELATIVE_PATHS["ARCHIVE_INDEX"]: _render_archive(visible_tasks, labels, source_sha256, profile_name),
    }
    return {"schema_version": SCHEMA_VERSION, "source_sha256": source_sha256, "profile": profile_name, "today": today, "files": files}


def _marker_for_bytes(payload: bytes, expected_view: Optional[str] = None) -> Optional[dict[str, Any]]:
    first = payload.splitlines()[0] if payload.splitlines() else b""
    match = _MARKER_RE.fullmatch(first)
    if not match:
        return None
    view = match.group(1).decode("ascii")
    schema = int(match.group(2))
    digest = match.group(3).decode("ascii")
    profile = match.group(4).decode("ascii")
    if expected_view is not None and view != expected_view:
        return None
    if schema != SCHEMA_VERSION:
        return None
    return {"view": view, "schema": schema, "source_sha256": digest, "profile": profile}


def write_views(root: Union[str, os.PathLike[str]], bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Commit a rendered bundle with marker checks and rollback semantics."""

    root_path = Path(root)
    if root_path.is_symlink() or (root_path.exists() and not root_path.is_dir()):
        raise ViewError(f"{root_path}: workspace root must be a real directory")
    files = bundle.get("files")
    source_sha256 = bundle.get("source_sha256")
    if not isinstance(files, Mapping) or not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ViewError("bundle: invalid files or source_sha256")
    expected: dict[Path, bytes] = {}
    for relative, payload in files.items():
        if relative not in VIEW_RELATIVE_PATHS.values():
            raise ViewError(f"bundle: unsupported output path {relative!r}")
        if not isinstance(payload, (bytes, bytearray)):
            raise ViewError(f"bundle: {relative}: payload must be bytes")
        view = next(name for name, path in VIEW_RELATIVE_PATHS.items() if path == relative)
        parsed_marker = _marker_for_bytes(bytes(payload), view)
        if parsed_marker is None:
            raise ViewError(f"bundle: {relative}: missing or invalid generated marker")
        if parsed_marker["source_sha256"] != source_sha256 or parsed_marker["profile"] != bundle.get("profile"):
            raise ViewError(f"bundle: {relative}: marker does not match bundle metadata")
        expected[root_path / relative] = bytes(payload)
    if set(files) != set(VIEW_RELATIVE_PATHS.values()):
        raise ViewError("bundle: all five generated views are required")
    output_dir = root_path / "00_总览"
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise ViewError(f"{output_dir}: output directory must be a real directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    originals: dict[Path, Optional[bytes]] = {}
    for target in expected:
        # ``Path.exists`` is false for a dangling symlink; check the link
        # itself first so a broken user-owned path is not replaced.
        if target.is_symlink() or target.exists():
            if target.is_symlink() or not target.is_file():
                raise UserOwnedViewError(f"{target}: refusing to replace non-regular file")
            old = target.read_bytes()
            view = next(name for name, path in VIEW_RELATIVE_PATHS.items() if root_path / path == target)
            if _marker_for_bytes(old, view) is None:
                raise UserOwnedViewError(f"{target}: existing file is user-owned (marker required)")
            originals[target] = old
        else:
            originals[target] = None

    stage = Path(tempfile.mkdtemp(prefix=".workspace-organizer-views-", dir=str(output_dir)))
    replaced: list[Path] = []
    try:
        staged: dict[Path, Path] = {}
        for target, payload in expected.items():
            stage_file = stage / target.name
            stage_file.write_bytes(payload)
            staged[target] = stage_file
        changed: list[str] = []
        for target in sorted(expected, key=lambda p: p.name):
            payload = expected[target]
            if originals[target] == payload:
                continue
            # CAS-check the output immediately before replacement.  Obsidian
            # may have changed a generated page after our initial marker
            # inspection; never turn that race into last-write-wins.
            old = originals[target]
            if target.is_symlink():
                raise ViewCommitError(f"{target}: output binding changed before commit")
            if old is None:
                if target.exists():
                    raise ViewCommitError(f"{target}: appeared before commit")
            elif not target.is_file() or target.read_bytes() != old:
                raise ViewCommitError(f"{target}: changed before commit")
            os.replace(staged[target], target)
            replaced.append(target)
            changed.append(target.relative_to(root_path).as_posix())
        return {"status": "generated" if changed else "unchanged", "source_sha256": source_sha256, "changed": changed, "paths": sorted(VIEW_RELATIVE_PATHS.values())}
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            old = originals[target]
            try:
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    rollback_file = stage / (target.name + ".rollback")
                    rollback_file.write_bytes(old)
                    os.replace(rollback_file, target)
            except Exception as rollback_exc:  # pragma: no cover - rare filesystem failure
                rollback_errors.append(f"{target}: {rollback_exc}")
        message = f"view commit failed: {exc}"
        if rollback_errors:
            message += "; rollback incomplete: " + ", ".join(rollback_errors)
        raise ViewCommitError(message) from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw in {"", "null", "Null", "NULL", "~"}:
        return None
    if raw in {"true", "True", "TRUE"}:
        return True
    if raw in {"false", "False", "FALSE"}:
        return False
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        try:
            return json.loads(raw) if raw.startswith('"') else ast.literal_eval(raw)
        except Exception:
            return raw[1:-1]
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            pass
    return raw


def _frontmatter(path: Path) -> Optional[dict[str, Any]]:
    # Read only the frontmatter prefix.  Task bodies can contain confidential
    # material and must not be loaded merely to build a low-sensitivity view.
    raw_lines: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as stream:
            for raw_line in stream:
                total += len(raw_line)
                if total > 1024 * 1024:
                    return None
                raw_lines.append(raw_line)
                if raw_line.rstrip(b"\r\n") == b"---" and len(raw_lines) > 1:
                    break
    except OSError:
        return None
    try:
        lines = b"".join(raw_lines).decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    data: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return data
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key.strip()):
            continue
        data[key.strip()] = _parse_scalar(raw)
    return None


def _config_data(root: Path) -> dict[str, Any]:
    """Read the tiny, non-secret subset of workspace config used by views.

    The clean-slate config is ordinary YAML.  We intentionally parse only
    scalar values and the JSON-form ``areas`` list; policy validation remains
    the model layer's responsibility.
    """

    for filename in ("config.yaml", "config.yml", "config.json"):
        config_path = root / ".workspace-organizer" / filename
        if not config_path.is_file() or config_path.is_symlink():
            continue
        try:
            if config_path.suffix == ".json":
                value = json.loads(config_path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            data: dict[str, Any] = {}
            for raw_line in config_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                data[key.strip()] = _parse_scalar(raw)
            return data
        except (OSError, UnicodeDecodeError, ValueError):
            return {}
    return {}


def _config_area_labels(root: Path) -> dict[str, str]:
    config = _config_data(root)
    areas = config.get("areas")
    labels: dict[str, str] = {}
    if isinstance(areas, list):
        for item in areas:
            if isinstance(item, Mapping):
                key = item.get("key", item.get("id"))
                label = item.get("label", key)
                if isinstance(key, str) and isinstance(label, str) and key and label:
                    labels[key] = label
    elif isinstance(areas, Mapping):
        for key, item in areas.items():
            if isinstance(item, Mapping):
                label = item.get("label", key)
            else:
                label = item
            if isinstance(key, str) and isinstance(label, str):
                labels[key] = label
    return labels


def _workspace_today(root: Path, now: Any) -> Any:
    if now is not None:
        return now
    config = _config_data(root)
    timezone_name = config.get("timezone")
    if isinstance(timezone_name, str) and timezone_name:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(timezone_name)).date()
        except Exception:
            pass
    return date.today()


def _focus_from_user_file(root: Path) -> tuple[str, ...]:
    """Read only stable Task IDs from the user-owned FOCUS.md page."""

    path = root / "01_导航" / "FOCUS.md"
    if path.is_symlink() or not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    values: list[str] = []
    for match in re.finditer(r"\[\[20_任务/([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:[|#\]])", text):
        value = match.group(1)
        if value not in values:
            values.append(value)
    return tuple(values[:3])


def _iter_markdown_files(base: Path) -> Iterable[Path]:
    """Yield Markdown files without crossing symlink or nested-Git boundaries."""

    if not base.is_dir() or base.is_symlink():
        return
    def is_git_boundary(directory: Path) -> bool:
        marker = directory / ".git"
        return marker.is_symlink() or marker.exists()

    for current, directories, filenames in os.walk(str(base), topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path != base and is_git_boundary(current_path):
            # A nested repository is a hard ownership boundary.  Do not
            # inspect either its metadata or ordinary files beneath it.
            directories[:] = []
            continue
        directories[:] = sorted(
            name
            for name in directories
            if name != ".git"
            and not (current_path / name).is_symlink()
            and not is_git_boundary(current_path / name)
        )
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            candidate = current_path / name
            if not candidate.is_symlink() and candidate.is_file():
                yield candidate


def collect_records(root: Union[str, os.PathLike[str]]) -> dict[str, list[dict[str, Any]]]:
    """Collect task/capture metadata from canonical Markdown files.

    Files are read only for front matter.  Non-record Markdown is ignored.
    Record validation is deferred to :func:`build_views` so sensitivity can be
    checked before any other field; a hidden malformed title must not affect a
    lower-sensitivity projection.
    """

    root_path = Path(root)
    tasks: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    task_roots = [root_path / "20_任务", root_path / "90_归档"]
    seen_paths: set[Path] = set()
    for task_root in task_roots:
        if not task_root.is_dir() or task_root.is_symlink():
            continue
        for path in _iter_markdown_files(task_root):
            if path.is_symlink() or path in seen_paths:
                continue
            seen_paths.add(path)
            data = _frontmatter(path)
            if not data or data.get("kind") not in {"task", "Task"}:
                continue
            task_id = data.get("id", data.get("task_id"))
            if not isinstance(task_id, str) or path.stem != task_id or path.parent.name != task_id:
                # Only the bundle-root note is canonical.  A Markdown task
                # embedded in an artifact directory is not silently promoted
                # into the global task queue.
                continue
            data["record_path"] = path.relative_to(root_path).as_posix()
            tasks.append(data)

    inbox_root = root_path / "10_收件箱"
    if inbox_root.is_dir() and not inbox_root.is_symlink():
        for path in _iter_markdown_files(inbox_root):
            if path.is_symlink():
                continue
            data = _frontmatter(path)
            if not data or data.get("kind") not in {"capture", "Capture"}:
                continue
            data["path"] = path.relative_to(root_path).as_posix()
            captures.append(data)
    return {"tasks": tasks, "captures": captures, "artifacts": []}


def generate_views(
    root: Union[str, os.PathLike[str]],
    now: Any = None,
    profile: Any = "internal",
    focus_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Collect canonical records, render and commit all five pages."""

    root_path = Path(root)
    records = collect_records(root_path)
    selected_focus = tuple(focus_ids) if focus_ids else _focus_from_user_file(root_path)
    config = _config_data(root_path)
    effective_profile = profile
    if profile is None:
        effective_profile = config.get("view_max_sensitivity", config.get("default_sensitivity", "internal"))
    bundle = build_views(
        records["tasks"],
        records["captures"],
        now=_workspace_today(root_path, now),
        profile=effective_profile,
        focus_ids=selected_focus,
        area_labels=_config_area_labels(root_path),
    )
    receipt = write_views(root_path, bundle)
    receipt.update(source_sha256=bundle["source_sha256"], profile=bundle["profile"], today=bundle["today"])
    return receipt


__all__ = [
    "SCHEMA_VERSION",
    "VIEW_NAMES",
    "VIEW_RELATIVE_PATHS",
    "ViewError",
    "UserOwnedViewError",
    "ViewCommitError",
    "build_views",
    "collect_records",
    "generate_views",
    "write_views",
]
