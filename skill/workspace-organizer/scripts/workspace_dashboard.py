#!/usr/bin/env python3
"""Generate and verify the deterministic read-only workspace dashboard."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import stat
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import workspace_organizer as core


DASHBOARD_RELATIVE = ".workspace-organizer/dashboard"
DASHBOARD_FILES = ("index.html", "styles.css", "app.js", "manifest.json")
VISIBLE_SENSITIVITIES = {"public", "internal"}
STATUS_ORDER = ("active", "blocked", "waiting", "planned")
STATUS_LABELS = {
    "active": ("In motion", "Currently advancing"),
    "blocked": ("Blocked", "Needs an obstacle cleared"),
    "waiting": ("Waiting", "Pending an external signal"),
    "planned": ("Planned", "Queued for a deliberate start"),
}
PRIORITY_ORDER = ("urgent", "high", "normal", "low")
MAX_INPUT_BYTES = 4 * 1024 * 1024
HTML_MARKER = re.compile(
    rb"<!-- workspace-organizer:dashboard-generated schema=1 asset=index "
    rb"source_fingerprint=[0-9a-f]{64} -->"
)
CSS_MARKER = b"/* workspace-organizer:dashboard-asset schema=1 asset=styles */"
JS_MARKER = b"/* workspace-organizer:dashboard-asset schema=1 asset=app */"


class DashboardError(ValueError):
    """The dashboard cannot be generated or verified safely."""


class StaleDashboard(DashboardError):
    """Canonical data and a derived source or output do not agree."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot(value: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> Tuple[int, int, int]:
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _open_directory(path: Path, context: str) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise DashboardError(f"{context}: cannot open no-follow directory: {exc}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise DashboardError(f"{context}: must be a directory")
    return descriptor


def _open_directory_at(parent_fd: int, name: str, context: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise DashboardError(f"{context}: cannot open no-follow directory: {exc}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise DashboardError(f"{context}: must be a directory")
    return descriptor


def _dashboard_entry(control_fd: int) -> Optional[str]:
    target_key = unicodedata.normalize("NFC", "dashboard").casefold()
    matches = [
        name
        for name in os.listdir(control_fd)
        if unicodedata.normalize("NFC", name).casefold() == target_key
    ]
    if len(matches) > 1 or (matches and matches[0] != "dashboard"):
        rendered = ", ".join(sorted(repr(name) for name in matches))
        raise DashboardError(
            f".workspace-organizer: normalized dashboard name collision: {rendered}"
        )
    return matches[0] if matches else None


class _DashboardDirectory:
    def __init__(
        self, root: Path, root_fd: int, control_fd: int, dashboard_fd: int
    ) -> None:
        self.root = root
        self.root_fd = root_fd
        self.control_fd = control_fd
        self.dashboard_fd = dashboard_fd
        self.root_identity = _directory_identity(os.fstat(root_fd))
        self.control_identity = _directory_identity(os.fstat(control_fd))
        self.dashboard_identity = _directory_identity(os.fstat(dashboard_fd))

    def close(self) -> None:
        for descriptor in (self.dashboard_fd, self.control_fd, self.root_fd):
            if descriptor >= 0:
                os.close(descriptor)
        self.dashboard_fd = -1
        self.control_fd = -1
        self.root_fd = -1

    def assert_bound(self) -> None:
        reopened_root: Optional[int] = None
        reopened_control: Optional[int] = None
        reopened_dashboard: Optional[int] = None
        try:
            try:
                root_info = self.root.lstat()
            except OSError as exc:
                raise DashboardError(
                    f"{self.root}: canonical workspace binding is unavailable: {exc}"
                ) from exc
            if stat.S_ISLNK(root_info.st_mode) or _directory_identity(root_info) != self.root_identity:
                raise DashboardError(f"{self.root}: canonical workspace binding changed")
            reopened_root = _open_directory(self.root, str(self.root))
            if _directory_identity(os.fstat(reopened_root)) != self.root_identity:
                raise DashboardError(f"{self.root}: canonical workspace binding changed")

            control_info = os.stat(
                ".workspace-organizer", dir_fd=self.root_fd, follow_symlinks=False
            )
            if (
                stat.S_ISLNK(control_info.st_mode)
                or _directory_identity(control_info) != self.control_identity
            ):
                raise DashboardError(
                    ".workspace-organizer: canonical control-directory binding changed"
                )
            reopened_control = _open_directory_at(
                self.root_fd, ".workspace-organizer", ".workspace-organizer"
            )
            if _directory_identity(os.fstat(reopened_control)) != self.control_identity:
                raise DashboardError(
                    ".workspace-organizer: canonical control-directory binding changed"
                )
            _dashboard_entry(self.control_fd)

            dashboard_info = os.stat(
                "dashboard", dir_fd=self.control_fd, follow_symlinks=False
            )
            if (
                stat.S_ISLNK(dashboard_info.st_mode)
                or _directory_identity(dashboard_info) != self.dashboard_identity
            ):
                raise DashboardError(
                    f"{DASHBOARD_RELATIVE}: canonical dashboard binding changed"
                )
            reopened_dashboard = _open_directory_at(
                self.control_fd, "dashboard", DASHBOARD_RELATIVE
            )
            if _directory_identity(os.fstat(reopened_dashboard)) != self.dashboard_identity:
                raise DashboardError(
                    f"{DASHBOARD_RELATIVE}: canonical dashboard binding changed"
                )
        except FileNotFoundError as exc:
            raise DashboardError(
                f"{DASHBOARD_RELATIVE}: canonical parent binding changed"
            ) from exc
        finally:
            for descriptor in (reopened_dashboard, reopened_control, reopened_root):
                if descriptor is not None:
                    os.close(descriptor)


def _read_regular(path: Path, context: str, *, limit: int = MAX_INPUT_BYTES) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise DashboardError(f"{context}: cannot stat regular file: {exc}") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise DashboardError(f"{context}: must be a no-follow regular file")
    if before.st_size > limit:
        raise DashboardError(f"{context}: exceeds the {limit}-byte safety limit")
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if _snapshot(opened) != _snapshot(before):
            raise DashboardError(f"{context}: file changed before read")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise DashboardError(f"{context}: changed beyond the safety limit")
        after = os.fstat(descriptor)
        if _snapshot(opened) != _snapshot(after):
            raise DashboardError(f"{context}: file changed during read")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_display(value: Any) -> str:
    """Make controls and bidi overrides visible before HTML escaping."""
    text = unicodedata.normalize("NFC", str(value))
    return "".join(
        "\ufffd" if unicodedata.category(character).startswith("C") else character
        for character in text
    )


def _text(value: Any) -> str:
    return html.escape(_safe_display(value), quote=True)


def _record_href(record: str) -> str:
    normalized = core.validate_relative_path(record, "dashboard record")
    if PurePosixPath(normalized).name != "TASK.md":
        raise DashboardError("dashboard record: canonical link must end in TASK.md")
    return html.escape("../../" + quote(normalized, safe="/-._~"), quote=True)


def _validate_catalog_shape(catalog: Any, view: str) -> Mapping[str, Any]:
    if not isinstance(catalog, dict):
        raise DashboardError(f"{view} catalog: must be an object")
    if set(catalog) != {"schema_version", "view", "profile", "source_sha256", "items"}:
        raise DashboardError(f"{view} catalog: has an unknown or missing field")
    if catalog["schema_version"] != 1 or catalog["view"] != view or catalog["profile"] != "default":
        raise DashboardError(f"{view} catalog: does not use the stable v1 default interface")
    if not isinstance(catalog["source_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", catalog["source_sha256"]
    ):
        raise DashboardError(f"{view} catalog: invalid source digest")
    if not isinstance(catalog["items"], list):
        raise DashboardError(f"{view} catalog: items must be an array")
    for item in catalog["items"]:
        sensitivity = item.get("sensitivity") if isinstance(item, dict) else None
        if not isinstance(sensitivity, str) or sensitivity not in VISIBLE_SENSITIVITIES:
            raise DashboardError(
                f"{view} catalog: sensitivity is not provably visible"
            )
    calculated = _sha256(_canonical_json(catalog["items"]))
    if calculated != catalog["source_sha256"]:
        raise DashboardError(f"{view} catalog: item digest does not match source_sha256")
    return catalog


def _load_catalog(root: Path, view: str, expected: bytes) -> Tuple[Mapping[str, Any], bytes]:
    relative = core.GENERATED_PATHS[view][0]
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        actual = _read_regular(path, relative)
    except DashboardError as exc:
        raise StaleDashboard(f"{exc}; run the v1 index command first") from exc
    if actual != expected:
        raise StaleDashboard(
            f"{relative}: catalog is stale or not canonical; run the v1 index command first"
        )
    try:
        value = json.loads(actual.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DashboardError(f"{relative}: invalid UTF-8 JSON") from exc
    return _validate_catalog_shape(value, view), actual


def _canonical_provenance(root: Path) -> Dict[str, Mapping[str, Any]]:
    config = core.load_config(root)
    records = core._registered_tasks(root, config, include_archived=False)
    result: Dict[str, Mapping[str, Any]] = {}
    for record in records:
        data = record["data"]
        sensitivity = data.get("sensitivity")
        if not isinstance(sensitivity, str) or sensitivity not in VISIBLE_SENSITIVITIES:
            continue
        if data["status"] not in core.OPEN_STATUSES:
            continue
        result[record["record"]] = record
    return result


def _cross_validate(
    todo: Mapping[str, Any], timeline: Mapping[str, Any], provenance: Mapping[str, Mapping[str, Any]]
) -> None:
    safe_todo: Dict[str, Mapping[str, Any]] = {}
    for item in todo["items"]:
        if not isinstance(item, dict):
            raise DashboardError("todo catalog: every item must be an object")
        sensitivity = item.get("sensitivity")
        if not isinstance(sensitivity, str) or sensitivity not in VISIBLE_SENSITIVITIES:
            raise DashboardError("todo catalog: sensitivity is not provably visible")
        record = item.get("record")
        if not isinstance(record, str):
            raise DashboardError("todo catalog: record must be a relative TASK.md path")
        _record_href(record)
        canonical = provenance.get(record)
        if canonical is None:
            raise DashboardError("todo catalog: record has no canonical task provenance")
        data = canonical["data"]
        expected = {
            "id": data["id"],
            "title": data["title"],
            "status": data["status"],
            "priority": data["priority"],
            "due": data["due"],
            "sensitivity": data["sensitivity"],
            "next_action": data["next_action"],
            "record": canonical["record"],
        }
        if item != expected:
            raise DashboardError("todo catalog: item disagrees with its canonical TASK.md")
        if item["id"] in safe_todo:
            raise DashboardError("todo catalog: duplicate task ID")
        safe_todo[item["id"]] = item

    expected_timeline = []
    for item in safe_todo.values():
        if item["due"] is None:
            continue
        expected_timeline.append(
            {
                "date": item["due"],
                "event": "due",
                "id": item["id"],
                "title": item["title"],
                "status": item["status"],
                "priority": item["priority"],
                "sensitivity": item["sensitivity"],
                "record": item["record"],
            }
        )
    rank = {name: index for index, name in enumerate(PRIORITY_ORDER)}
    expected_timeline.sort(key=lambda item: (item["date"], rank[item["priority"]], item["id"]))
    for item in timeline["items"]:
        sensitivity = item.get("sensitivity") if isinstance(item, dict) else None
        if not isinstance(sensitivity, str) or sensitivity not in VISIBLE_SENSITIVITIES:
            raise DashboardError("timeline catalog: item is malformed or not provably visible")
    if timeline["items"] != expected_timeline:
        raise DashboardError("timeline catalog: items disagree with canonical TODO due events")


def _load_view_model(
    root: Path,
) -> Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], str]:
    root = core._workspace_root(root)
    expected = core.build_generated_outputs(root)
    todo, todo_bytes = _load_catalog(root, "todo", expected[core.GENERATED_PATHS["todo"][0]])
    timeline, timeline_bytes = _load_catalog(
        root, "timeline", expected[core.GENERATED_PATHS["timeline"][0]]
    )
    _cross_validate(todo, timeline, _canonical_provenance(root))
    descriptor = {
        "schema_version": 1,
        "todo_catalog_sha256": _sha256(todo_bytes),
        "todo_source_sha256": todo["source_sha256"],
        "timeline_catalog_sha256": _sha256(timeline_bytes),
        "timeline_source_sha256": timeline["source_sha256"],
    }
    return todo, timeline, descriptor, _sha256(_canonical_json(descriptor))


def _task_card(item: Mapping[str, Any], sequence: int) -> str:
    due = item["due"]
    due_markup = (
        f'<time datetime="{_text(due)}">Due {_text(due)}</time>' if due else "No due date"
    )
    return f'''          <article class="task-sheet" data-task-sheet data-sequence="{sequence}" data-priority="{_text(item['priority'])}" data-status="{_text(item['status'])}">
            <a class="task-link" href="{_record_href(item['record'])}" aria-label="Open canonical TASK.md for {_text(item['title'])}">
              <p class="task-id">{_text(item['id'])}</p>
              <h4 dir="auto">{_text(item['title'])}</h4>
              <div class="task-meta"><span class="priority-chip" data-priority="{_text(item['priority'])}">{_text(item['priority'])}</span><span>{due_markup}</span><span>{_text(item['sensitivity'])}</span></div>
            </a>
            <div class="next-action"><p class="group-kicker">Next action</p><p dir="auto">{_text(item['next_action'])}</p></div>
          </article>'''


def _todo_markup(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return '''      <div class="empty-state" role="status">
        <div><strong>The docket is clear.</strong><p>No public or internal open tasks are present in the canonical v1 view.</p></div>
      </div>'''
    groups: Dict[str, List[Mapping[str, Any]]] = {status: [] for status in STATUS_ORDER}
    for item in items:
        groups[item["status"]].append(item)
    sections: List[str] = []
    sequence = 0
    for index, status in enumerate(STATUS_ORDER, start=1):
        records = groups[status]
        if not records:
            continue
        title, explanation = STATUS_LABELS[status]
        cards = []
        for item in records:
            sequence += 1
            cards.append(_task_card(item, sequence))
        sections.append(
            f'''      <section class="status-group" data-status-group="{status}" aria-labelledby="status-{status}">
        <div class="group-heading"><p class="group-kicker">{index:02d} / {_text(explanation)}</p><h3 id="status-{status}">{_text(title)}</h3><span class="group-count" aria-label="{len(records)} tasks">{len(records):02d}</span></div>
        <div class="task-stack">\n{chr(10).join(cards)}\n        </div>
      </section>'''
        )
    return "\n".join(sections)


def _timeline_markup(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return '''      <div class="empty-state" role="status">
        <div><strong>No dated dispatches.</strong><p>Visible open tasks have no due dates in the canonical timeline.</p></div>
      </div>'''
    entries = []
    for item in items:
        entries.append(
            f'''        <li class="timeline-entry" data-timeline-entry data-priority="{_text(item['priority'])}">
          <time class="timeline-date" datetime="{_text(item['date'])}">{_text(item['date'])}</time>
          <div class="timeline-body"><p class="timeline-event">Due / {_text(item['priority'])} / {_text(item['status'])}</p><h3 dir="auto">{_text(item['title'])}</h3><a href="{_record_href(item['record'])}">Open {_text(item['id'])} TASK.md</a></div>
        </li>'''
        )
    return '<ol class="timeline-ledger">\n' + "\n".join(entries) + "\n      </ol>"


def _render_html(
    todo: Mapping[str, Any], timeline: Mapping[str, Any], fingerprint: str
) -> bytes:
    items = todo["items"]
    dated = timeline["items"]
    blocked = sum(1 for item in items if item["status"] == "blocked")
    urgent = sum(1 for item in items if item["priority"] == "urgent")
    marker = (
        "<!-- workspace-organizer:dashboard-generated schema=1 asset=index "
        f"source_fingerprint={fingerprint} -->"
    )
    document = f'''{marker}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'none'; font-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
  <meta name="referrer" content="no-referrer">
  <title>Local operations docket</title>
  <link rel="stylesheet" href="styles.css">
  <script src="app.js" defer></script>
</head>
<body>
  <div class="shell">
    <header class="masthead">
      <div><p class="eyebrow">Workspace Organizer / Local file 07</p><h1>Work in motion</h1></div>
      <div class="masthead-note"><p>A static editorial docket of public and internal task metadata. Canonical decisions remain in each linked <code>TASK.md</code>.</p><span class="read-only-stamp">Read-only projection</span></div>
    </header>
    <div class="control-rail">
      <nav class="view-tabs" role="tablist" aria-label="Dashboard views">
        <button class="tab" type="button" role="tab" id="todo-tab" aria-controls="todo-view" aria-selected="true" tabindex="0" data-view-target="todo">TODO docket</button>
        <button class="tab" type="button" role="tab" id="timeline-tab" aria-controls="timeline-view" aria-selected="false" tabindex="-1" data-view-target="timeline">Timeline</button>
      </nav>
      <div class="priority-filters" aria-label="Filter TODO by priority"><span class="filter-label">Priority</span><button class="filter" type="button" aria-pressed="true" data-priority-filter="all">All</button><button class="filter" type="button" aria-pressed="false" data-priority-filter="urgent">Urgent</button><button class="filter" type="button" aria-pressed="false" data-priority-filter="high">High</button><button class="filter" type="button" aria-pressed="false" data-priority-filter="normal">Normal</button><button class="filter" type="button" aria-pressed="false" data-priority-filter="low">Low</button></div>
    </div>
    <main class="docket">
      <section class="metrics" aria-label="Visible task summary">
        <div class="metric"><span class="metric-label">Open tasks</span><strong class="metric-value">{len(items):02d}</strong></div>
        <div class="metric"><span class="metric-label">Dated</span><strong class="metric-value">{len(dated):02d}</strong></div>
        <div class="metric metric--signal"><span class="metric-label">Blocked</span><strong class="metric-value">{blocked:02d}</strong></div>
        <div class="metric metric--signal"><span class="metric-label">Urgent</span><strong class="metric-value">{urgent:02d}</strong></div>
      </section>
      <section class="view-panel" id="todo-view" role="tabpanel" aria-labelledby="todo-tab" tabindex="-1" data-view-panel="todo">
        <div class="section-heading"><h2>Task docket</h2><p>Grouped by lifecycle status, ordered by the stable v1 TODO contract. Filters alter only this local display.</p></div>
{_todo_markup(items)}
      </section>
      <section class="view-panel" id="timeline-view" role="tabpanel" aria-labelledby="timeline-tab" tabindex="-1" data-view-panel="timeline" hidden>
        <div class="section-heading"><h2>Dated dispatches</h2><p>Due events in canonical date, priority, and task-ID order. Undated tasks remain in the TODO docket.</p></div>
      {_timeline_markup(dated)}
      </section>
      <footer class="snapshot-footer">
        <div><p class="folio">Static snapshot / derived, disposable, non-authoritative</p><p>Refresh only after regenerating the v1 catalogs. No task body, file content, confidential record, or restricted record is included.</p></div>
        <div><p class="snapshot-label">Source fingerprint</p><p class="snapshot-fingerprint">{fingerprint}</p><p><code>python3 scripts/workspace_dashboard.py verify WORKSPACE</code></p></div>
      </footer>
    </main>
  </div>
</body>
</html>
'''
    return document.encode("utf-8")


def _asset_templates() -> Tuple[bytes, bytes]:
    asset_root = Path(__file__).resolve().parents[1] / "assets" / "dashboard"
    styles = _read_regular(asset_root / "styles.css", "dashboard styles asset")
    app = _read_regular(asset_root / "app.js", "dashboard application asset")
    if not styles.startswith(CSS_MARKER + b"\n"):
        raise DashboardError("dashboard styles asset: missing package marker")
    if not app.startswith(JS_MARKER + b"\n"):
        raise DashboardError("dashboard application asset: missing package marker")
    return styles, app


def build_dashboard_outputs(root: Path) -> Tuple[Dict[str, bytes], str]:
    todo, timeline, sources, fingerprint = _load_view_model(root)
    styles, app = _asset_templates()
    html_bytes = _render_html(todo, timeline, fingerprint)
    outputs = {"index.html": html_bytes, "styles.css": styles, "app.js": app}
    manifest = {
        "schema_version": 1,
        "generator": "workspace-organizer-dashboard",
        "derived": True,
        "source_fingerprint": fingerprint,
        "sources": sources,
        "outputs": {name: _sha256(payload) for name, payload in sorted(outputs.items())},
    }
    outputs["manifest.json"] = _pretty_json(manifest)
    return outputs, fingerprint


def _valid_existing(name: str, payload: bytes) -> bool:
    if name == "index.html":
        return bool(HTML_MARKER.fullmatch(payload.splitlines()[0] if payload.splitlines() else b""))
    if name == "styles.css":
        return payload.startswith(CSS_MARKER + b"\n")
    if name == "app.js":
        return payload.startswith(JS_MARKER + b"\n")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get("schema_version") == 1
        and manifest.get("generator") == "workspace-organizer-dashboard"
        and manifest.get("derived") is True
    )


def _open_dashboard_directory(root: Path, *, create: bool) -> _DashboardDirectory:
    root_fd: Optional[int] = None
    control_fd: Optional[int] = None
    dashboard_fd: Optional[int] = None
    try:
        root_fd = _open_directory(root, str(root))
        control_fd = _open_directory_at(
            root_fd, ".workspace-organizer", ".workspace-organizer"
        )
        entry = _dashboard_entry(control_fd)
        if entry is None:
            if not create:
                raise StaleDashboard(f"{DASHBOARD_RELATIVE}: dashboard is missing")
            os.mkdir("dashboard", mode=0o700, dir_fd=control_fd)
            entry = _dashboard_entry(control_fd)
            if entry != "dashboard":
                raise DashboardError(
                    f"{DASHBOARD_RELATIVE}: canonical dashboard entry was not created"
                )
        dashboard_fd = _open_directory_at(control_fd, "dashboard", DASHBOARD_RELATIVE)
        binding = _DashboardDirectory(root, root_fd, control_fd, dashboard_fd)
        root_fd = None
        control_fd = None
        dashboard_fd = None
        binding.assert_bound()
        return binding
    except BaseException:
        for descriptor in (dashboard_fd, control_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise


def _read_regular_at(directory_fd: int, name: str) -> bytes:
    context = f"{DASHBOARD_RELATIVE}/{name}"
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise DashboardError(f"{context}: must be a regular file")
    if info.st_size > MAX_INPUT_BYTES:
        raise DashboardError(f"{context}: exceeds safety limit")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=directory_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if _snapshot(opened) != _snapshot(info):
            raise DashboardError(f"{context}: changed before read")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_INPUT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_INPUT_BYTES:
                raise DashboardError(f"{context}: exceeds safety limit")
        if _snapshot(opened) != _snapshot(os.fstat(descriptor)):
            raise DashboardError(f"{context}: changed during read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _capture_existing(directory_fd: int) -> Dict[str, bytes]:
    names = sorted(os.listdir(directory_fd))
    unknown = [name for name in names if name not in DASHBOARD_FILES]
    if unknown:
        raise DashboardError(
            f"{DASHBOARD_RELATIVE}: contains user-owned or unknown entries: {', '.join(unknown)}"
        )
    existing: Dict[str, bytes] = {}
    for name in names:
        payload = _read_regular_at(directory_fd, name)
        if not _valid_existing(name, payload):
            raise DashboardError(f"{DASHBOARD_RELATIVE}/{name}: refusing to overwrite unmarked content")
        existing[name] = payload
    return existing


def _write_outputs(
    binding: _DashboardDirectory,
    outputs: Mapping[str, bytes],
    existing: Mapping[str, bytes],
    test_hook: Optional[Callable[[str], None]] = None,
) -> None:
    directory_fd = binding.dashboard_fd
    temporary: List[str] = []
    installed: List[Tuple[str, Optional[str]]] = []
    try:
        for name in DASHBOARD_FILES:
            temporary_name = f".workspace-dashboard-{secrets.token_hex(12)}"
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o444,
                dir_fd=directory_fd,
            )
            temporary.append(temporary_name)
            try:
                payload = outputs[name]
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise DashboardError(f"{DASHBOARD_RELATIVE}/{name}: short write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for temporary_name, name in zip(list(temporary), DASHBOARD_FILES):
            if test_hook is not None:
                test_hook(f"before-install:{name}")
            binding.assert_bound()
            if name in existing:
                core._exchange_entries_at(
                    directory_fd, temporary_name, directory_fd, name
                )
                if _read_regular_at(directory_fd, temporary_name) != existing[name]:
                    core._exchange_entries_at(
                        directory_fd, temporary_name, directory_fd, name
                    )
                    raise DashboardError(
                        f"{DASHBOARD_RELATIVE}/{name}: concurrent replacement was preserved"
                    )
                installed.append((name, temporary_name))
            else:
                core._rename_noreplace_at(directory_fd, temporary_name, name)
                installed.append((name, None))
            temporary.remove(temporary_name)
            if _read_regular_at(directory_fd, name) != outputs[name]:
                raise DashboardError(f"{DASHBOARD_RELATIVE}/{name}: installed bytes changed")
        if test_hook is not None:
            test_hook("before-success")
        binding.assert_bound()
        os.fsync(directory_fd)
    except BaseException:
        for name, backup in reversed(installed):
            try:
                if _read_regular_at(directory_fd, name) != outputs[name]:
                    continue
                if backup is None:
                    os.unlink(name, dir_fd=directory_fd)
                else:
                    core._exchange_entries_at(directory_fd, backup, directory_fd, name)
                    temporary.append(backup)
            except (DashboardError, OSError, core.WorkspaceError):
                continue
        os.fsync(directory_fd)
        raise
    else:
        for _, backup in installed:
            if backup is not None:
                os.unlink(backup, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        for name in temporary:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def generate_dashboard(
    root: Path, *, _test_hook: Optional[Callable[[str], None]] = None
) -> Mapping[str, Any]:
    root = core._workspace_root(root)
    outputs, fingerprint = build_dashboard_outputs(root)
    binding = _open_dashboard_directory(root, create=True)
    try:
        existing = _capture_existing(binding.dashboard_fd)
        if existing == outputs:
            if _test_hook is not None:
                _test_hook("before-success")
            binding.assert_bound()
            status = "unchanged"
        else:
            _write_outputs(binding, outputs, existing, _test_hook)
            if _capture_existing(binding.dashboard_fd) != outputs:
                raise DashboardError(f"{DASHBOARD_RELATIVE}: post-write verification failed")
            binding.assert_bound()
            status = "generated"
    finally:
        binding.close()
    return {
        "schema_version": 1,
        "operation": "dashboard-generate",
        "status": status,
        "derived_path": DASHBOARD_RELATIVE,
        "source_fingerprint": fingerprint,
        "files": list(DASHBOARD_FILES),
    }


def verify_dashboard(
    root: Path, *, _test_hook: Optional[Callable[[str], None]] = None
) -> Mapping[str, Any]:
    root = core._workspace_root(root)
    try:
        outputs, fingerprint = build_dashboard_outputs(root)
        binding = _open_dashboard_directory(root, create=False)
        try:
            existing = _capture_existing(binding.dashboard_fd)
            if _test_hook is not None:
                _test_hook("before-verify-success")
            binding.assert_bound()
        finally:
            binding.close()
    except StaleDashboard as exc:
        return {
            "schema_version": 1,
            "operation": "dashboard-verify",
            "status": "stale",
            "derived_path": DASHBOARD_RELATIVE,
            "reason": str(exc),
        }
    mismatches = [name for name in DASHBOARD_FILES if existing.get(name) != outputs[name]]
    if mismatches:
        return {
            "schema_version": 1,
            "operation": "dashboard-verify",
            "status": "stale",
            "derived_path": DASHBOARD_RELATIVE,
            "source_fingerprint": fingerprint,
            "mismatches": mismatches,
        }
    return {
        "schema_version": 1,
        "operation": "dashboard-verify",
        "status": "current",
        "derived_path": DASHBOARD_RELATIVE,
        "source_fingerprint": fingerprint,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("root", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "generate":
            result = generate_dashboard(arguments.root)
        else:
            result = verify_dashboard(arguments.root)
    except (DashboardError, core.WorkspaceError, OSError) as exc:
        sys.stderr.write(f"workspace-dashboard: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if result["status"] not in {"stale"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
