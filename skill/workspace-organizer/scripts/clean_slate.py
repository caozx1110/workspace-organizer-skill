#!/usr/bin/env python3
"""Clean-slate command line interface for workspace-organizer.

The CLI is intentionally JSON-first so Chat/Agent integrations can consume
receipts without scraping prose.  Markdown records remain canonical; generated
views and operation plans are disposable or auditable respectively.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore

from clean_slate_model import (
    Artifact,
    Capture,
    CASConflict,
    ModelError,
    StateTransitionError,
    Task,
    append_event,
    archive_task,
    cas_update_path,
    format_timestamp,
    parse_date,
    parse_record,
    parse_record_bytes,
    parse_timestamp,
    render_frontmatter,
    restore_task,
    sha256_bytes,
    transition_task,
    effective_agent_access,
    validate_artifact,
    validate_capture,
    validate_task,
)
from workspace_views import ViewError, build_views, collect_records, generate_views, write_views


ROOT_DIRS = (
    "00_总览",
    "01_导航",
    "10_收件箱",
    "20_任务",
    "30_资料库",
    "90_归档",
    "99_待整理",
    ".workspace-organizer",
)
ROLE_DIRS = {
    "input": "01_输入",
    "work": "02_工作",
    "deliverable": "03_交付",
    "record": "04_记录",
}
SENSITIVITY_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
AGENT_ACCESS_RANK = {"none": 0, "metadata": 1, "content": 2}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class CLIError(ValueError):
    """An operation cannot be completed safely."""


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _safe_single_line(value: Any, context: str, *, maximum: int = 512) -> str:
    """Validate human/configuration text before it becomes a path or label.

    The domain model applies the same rule to record fields.  Configuration is
    read before a record exists, so keep this small boundary check in the CLI
    too; otherwise a newline/control character can create an ambiguous config
    or an archive destination that cannot be reconciled safely.
    """

    if not isinstance(value, str) or not value or value != _nfc(value):
        raise _error(f"{context}: must be non-empty NFC text")
    if len(value) > maximum or value != value.strip() or "\n" in value or "\r" in value:
        raise _error(f"{context}: must be trimmed single-line text")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise _error(f"{context}: control characters are not allowed")
    return value


def _safe_operation_id(value: Any, context: str = "operation_id") -> str:
    """Return an operation id that is safe to use as one filename component."""

    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise _error(f"{context}: unsafe operation identifier")
    return value


def _is_platform_alias(path: Path) -> bool:
    """Allow the standard macOS /tmp and /var aliases.

    ``tempfile`` commonly returns paths beginning with ``/var`` or ``/tmp``
    on macOS, both of which are system-owned symlinks to /private.  They are
    not user-controlled workspace links.  Any other symlink component remains
    untrusted and is rejected by ``_reject_symlink_components``.
    """

    if path not in {Path("/tmp"), Path("/var")}:
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved in {Path("/private/tmp"), Path("/private/var")}


def _reject_symlink_components(path: Path, *, allow_final: bool = False) -> None:
    """Reject user-controlled symlink components in an absolute path.

    This is a lexical no-follow check performed before resolving an external
    source.  System aliases on macOS are explicitly allow-listed above; all
    workspace-managed paths are already rooted at a resolved real directory.
    """

    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink() and not (allow_final and index == len(parts) - 1):
            if not _is_platform_alias(current):
                raise _error(f"{path}: symlink path component is not trusted")


def _mkdir_no_symlink(path: Path) -> None:
    """Create *path* one component at a time without following symlinks."""

    if path.exists() and path.is_symlink():
        raise _error(f"{path}: refusing to use symlink directory")
    missing: List[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    if current.is_symlink() or not current.is_dir():
        raise _error(f"{current}: parent is not a real directory")
    for candidate in reversed(missing):
        parent = candidate.parent
        if parent.is_symlink() or not parent.is_dir():
            raise _error(f"{parent}: parent is not a real directory")
        try:
            candidate.mkdir()
        except FileExistsError:
            if candidate.is_symlink() or not candidate.is_dir():
                raise _error(f"{candidate}: directory appeared as an unsafe object")


def _resolve_creation_path(path: Path) -> Path:
    """Resolve an absolute path's existing ancestor before creating it.

    This keeps the no-follow writer strict while still accepting macOS's
    conventional ``/tmp``/``/var`` aliases.  Missing components are appended
    only after the nearest existing ancestor has been resolved and checked.
    """

    absolute = Path(os.path.abspath(str(path)))
    missing: List[str] = []
    current = absolute
    while not current.exists():
        missing.append(current.name)
        parent = current.parent
        if parent == current:
            raise _error(f"{path}: cannot find an existing parent")
        current = parent
    if current.is_symlink() and not _is_platform_alias(current):
        raise _error(f"{path}: symlink ancestor is not trusted")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise _error(f"{path}: cannot resolve existing parent: {exc}") from exc
    for name in reversed(missing):
        resolved = resolved / name
    return resolved


def _has_frontmatter_prefix(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            first = stream.readline(8)
    except OSError as exc:
        raise _error(f"{path}: cannot inspect record: {exc}") from exc
    return first.rstrip(b"\r\n") == b"---"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pretty(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _error(message: str) -> CLIError:
    return CLIError(message)


def _validate_rel(value: str, context: str = "path") -> str:
    if not isinstance(value, str) or not value or value != _nfc(value):
        raise _error(f"{context}: must be a non-empty NFC relative path")
    if value.startswith("/") or "\\" in value or "\x00" in value or "//" in value:
        raise _error(f"{context}: must be a normalized POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _error(f"{context}: path traversal or empty segment")
    return value


def _root_path(root: Any) -> Path:
    path = Path(root)
    if path.is_symlink():
        raise _error("workspace root must not be a symlink")
    _reject_symlink_components(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _error(f"workspace root cannot be resolved: {exc}") from exc
    if not resolved.is_dir():
        raise _error("workspace root must be a directory")
    return resolved


def _check_nested_git(root: Path, directory: Path) -> None:
    current = directory
    while current != root:
        if current.name == ".git":
            raise _error(f"path crosses nested Git boundary: {directory}")
        marker = current / ".git"
        if marker.is_symlink() or marker.exists():
            raise _error(f"path crosses nested Git boundary: {directory}")
        if current.parent == current:
            raise _error("path escapes workspace")
        current = current.parent


def _managed_markdown_files(root: Path, relative: str) -> Iterable[Path]:
    """Yield Markdown records under a managed root without crossing links/Git.

    Mutation-oriented CLI commands fail closed on a symlink or nested Git
    boundary instead of silently treating hidden content as absent.  The
    projection module has a deliberately more permissive, read-only scanner;
    this stricter iterator is for identity/lifecycle operations.
    """

    base = root / relative
    if not base.exists():
        return
    if base.is_symlink() or not base.is_dir():
        raise _error(f"{relative}: managed root must be a real directory")
    _check_nested_git(root, base)
    for current, directories, filenames in os.walk(str(base), topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise _error(f"{current_path}: symlink directory is not allowed")
        if current_path != root and (current_path / ".git").exists():
            raise _error(f"{current_path}: nested Git boundary")
        kept: List[str] = []
        for name in sorted(directories):
            candidate = current_path / name
            if name == ".git" or candidate.is_symlink():
                raise _error(f"{candidate}: managed scan crosses an unsafe boundary")
            kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            candidate = current_path / name
            if name.endswith(".md"):
                if candidate.is_symlink() or not candidate.is_file():
                    raise _error(f"{candidate}: managed Markdown must be a regular file")
                yield candidate


def _safe_path(root: Path, relative: str, *, kind: Optional[str] = None, allow_missing: bool = False) -> Path:
    relative = _validate_rel(relative, relative)
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            stat_result = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                # Existing parents still need checking.
                if index < len(parts) - 1:
                    continue
                break
            raise _error(f"{relative}: path does not exist")
        except OSError as exc:
            raise _error(f"{relative}: cannot inspect path: {exc}") from exc
        if current.is_symlink():
            raise _error(f"{relative}: symlink components are not allowed")
        if index < len(parts) - 1 and not current.is_dir():
            raise _error(f"{relative}: parent component is not a directory")
    parent = current if current.exists() and current.is_dir() else current.parent
    _check_nested_git(root, parent)
    if not allow_missing and not current.exists():
        raise _error(f"{relative}: path does not exist")
    if kind == "file" and (not current.exists() or not current.is_file()):
        raise _error(f"{relative}: expected a regular file")
    if kind == "directory" and (not current.exists() or not current.is_dir()):
        raise _error(f"{relative}: expected a directory")
    return current


def _prepare_directory(root: Path, relative: str) -> Path:
    """Create a managed relative directory without following symlinks."""

    relative = _validate_rel(relative, "directory")
    current = root
    for part in PurePosixPath(relative).parts:
        candidate = current / part
        if candidate.is_symlink():
            raise _error(f"{relative}: symlink directory component is not allowed")
        # Check siblings even when the requested spelling already resolves to
        # an existing directory.  On a case-insensitive/NFC-normalizing
        # filesystem ``candidate.exists()`` can otherwise bind to a different
        # user-created directory than the spelling in the plan.
        folded = _nfc(part).casefold()
        try:
            siblings = list(current.iterdir())
        except OSError as exc:
            raise _error(f"{current}: cannot inspect directory: {exc}") from exc
        for sibling in siblings:
            if sibling.name != part and _nfc(sibling.name).casefold() == folded:
                raise _error(f"{relative}: normalized sibling collision with {sibling.name}")
        if candidate.exists():
            if not candidate.is_dir():
                raise _error(f"{relative}: directory component is not a directory")
        else:
            try:
                candidate.mkdir()
            except FileExistsError:
                # A concurrent creator may have won the race; re-check the
                # object without following a symlink before accepting it.
                if candidate.is_symlink() or not candidate.is_dir():
                    raise _error(f"{relative}: directory appeared as an unsafe object")
        current = candidate
    _check_nested_git(root, current)
    return current


def _safe_write(path: Path, payload: bytes, *, replace: bool = False) -> None:
    if path.is_symlink():
        raise _error(f"{path}: refusing to write through symlink")
    # Callers normally prepare the managed parent explicitly.  Keep this
    # fallback for control files, but create every missing component with a
    # no-follow check so a parent replacement cannot redirect the write.
    _reject_symlink_components(path.parent)
    _mkdir_no_symlink(path.parent)
    if path.parent.is_symlink():
        raise _error(f"{path.parent}: refusing to write through symlink parent")
    if path.exists() and not replace:
        raise _error(f"{path}: destination already exists")
    temporary = path.with_name("." + path.name + ".tmp-" + secrets.token_hex(8))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Re-check the parent and destination after the write.  os.replace
        # replaces a symlink itself rather than following it, but a swapped
        # parent would still redirect the directory entry.
        if path.parent.is_symlink():
            raise _error(f"{path.parent}: destination parent changed to symlink")
        if replace and path.exists() and path.is_symlink():
            raise _error(f"{path}: destination changed to symlink")
        os.replace(str(temporary), str(path))
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _slug(value: str) -> str:
    value = _nfc(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:48] or "task"


def _zone(config: Mapping[str, Any]) -> Any:
    name = config.get("timezone", "UTC")
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except Exception as exc:
        raise _error(f"invalid workspace timezone: {name}") from exc


def _now(config: Mapping[str, Any]) -> str:
    return datetime.now(_zone(config)).replace(microsecond=0).isoformat()


def _now_after(config: Mapping[str, Any], previous: Optional[str]) -> str:
    """Return a workspace-local timestamp strictly after *previous*.

    CLI commands often run within one wall-clock second.  Lifecycle/CAS
    receipts still require monotonic ``updated_at`` values, so advance by one
    microsecond when the system clock has not moved far enough.
    """

    candidate = datetime.now(_zone(config)).replace(microsecond=0)
    if previous:
        old = parse_timestamp(previous, "previous timestamp")
        if old is not None and candidate <= old:
            candidate = old + timedelta(microseconds=1)
    return format_timestamp(candidate)


def _today(config: Mapping[str, Any]) -> str:
    return datetime.now(_zone(config)).date().isoformat()


def _parse_simple_value(raw: str) -> Any:
    raw = raw.strip()
    if raw in {"", "null", "Null", "NULL", "~"}:
        return None
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        try:
            return json.loads(raw) if raw.startswith('"') else raw[1:-1].replace("''", "'")
        except Exception:
            return raw[1:-1]
    if re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    return raw


def _read_config(root: Path) -> Dict[str, Any]:
    control = root / ".workspace-organizer"
    if control.is_symlink() or not control.is_dir():
        raise _error(".workspace-organizer must be a real directory")
    candidates = [control / "config.yaml", control / "config.json"]
    target = next((path for path in candidates if path.is_file()), None)
    if target is None:
        raise _error("workspace is not initialized: missing .workspace-organizer/config.yaml")
    if target.is_symlink():
        raise _error("workspace config must not be a symlink")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise _error(f"cannot read workspace config: {exc}") from exc
    if target.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _error(f"invalid workspace config: {exc}") from exc
    else:
        data = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, raw = stripped.split(":", 1)
            data[key.strip()] = _parse_simple_value(raw)
    if not isinstance(data, dict):
        raise _error("workspace config must be a mapping")
    required = ("kind", "schema_version", "workspace_id", "timezone", "default_sensitivity", "areas")
    missing = [key for key in required if key not in data]
    if missing:
        raise _error("workspace config missing: " + ", ".join(missing))
    if data["kind"] != "workspace-config" or data["schema_version"] != 2:
        raise _error("workspace config must be kind=workspace-config schema_version=2")
    if not isinstance(data["workspace_id"], str) or not SLUG_RE.fullmatch(data["workspace_id"]):
        raise _error("workspace_id must be a lowercase slug")
    if data["default_sensitivity"] not in SENSITIVITY_RANK:
        raise _error("workspace sensitivity policy is invalid")
    areas = data["areas"]
    if not isinstance(areas, list):
        raise _error("areas must be a JSON list")
    normalized_areas = []
    seen = set()
    seen_folders = set()
    seen_labels = set()
    for item in areas:
        if not isinstance(item, dict):
            raise _error("each area must be an object")
        for key in ("key", "label", "archive_folder"):
            if key not in item or not isinstance(item[key], str) or not item[key].strip():
                raise _error(f"area.{key} is required")
        if not SLUG_RE.fullmatch(item["key"]):
            raise _error(f"area key is not a slug: {item['key']}")
        if item["key"].casefold() in seen:
            raise _error(f"duplicate area key: {item['key']}")
        seen.add(item["key"].casefold())
        _safe_single_line(item["label"], "area.label", maximum=240)
        _safe_single_line(item["archive_folder"], "area.archive_folder", maximum=240)
        _validate_rel(item["archive_folder"], "area.archive_folder")
        if "/" in item["archive_folder"]:
            raise _error("area.archive_folder must be a single directory name")
        folder_key = _nfc(item["archive_folder"]).casefold()
        if folder_key in seen_folders:
            raise _error(f"duplicate normalized archive folder: {item['archive_folder']}")
        seen_folders.add(folder_key)
        label_key = _nfc(item["label"]).casefold()
        if label_key in seen_labels:
            raise _error(f"duplicate normalized area label: {item['label']}")
        seen_labels.add(label_key)
        normalized_areas.append(dict(item))
    if "general" not in seen:
        if "通用".casefold() in seen_folders:
            raise _error("archive folder 通用 is reserved for the general area")
        if "通用".casefold() in seen_labels:
            raise _error("area label 通用 is reserved for the general area")
        normalized_areas.append({"key": "general", "label": "通用", "archive_folder": "通用"})
    data["areas"] = normalized_areas
    return data


def _area_labels(config: Mapping[str, Any]) -> Dict[str, str]:
    return {item["key"]: item["label"] for item in config.get("areas", []) if isinstance(item, dict)}


def _area_folder(config: Mapping[str, Any], key: str) -> str:
    for item in config.get("areas", []):
        if item.get("key") == key:
            return str(item["archive_folder"])
    raise _error(f"unknown area key: {key}")


def _record_path(root: Path, relative: str) -> Path:
    return _safe_path(root, relative, kind="file")


def _record_digest(path: Path) -> str:
    return _sha256_file(path)


def _event(root: Path, receipt: Any) -> None:
    append_event(root / ".workspace-organizer" / "events.jsonl", receipt)


def _find_task(root: Path, task_id: str, *, metadata_only: bool = False) -> Tuple[Path, Task]:
    if not ID_RE.fullmatch(task_id):
        raise _error("task id is invalid")
    candidates: List[Path] = []
    candidates.extend(_managed_markdown_files(root, "20_任务"))
    candidates.extend(_managed_markdown_files(root, "90_归档"))
    matches: List[Tuple[Path, Task]] = []
    for note in candidates:
        canonical = note.parent.name == note.stem
        if not canonical and not _has_frontmatter_prefix(note):
            continue
        try:
            record = _parse_record_frontmatter(note) if metadata_only else parse_record(note)
        except Exception as exc:
            if canonical or _has_frontmatter_prefix(note):
                raise _error(f"{note}: invalid managed task note: {exc}") from exc
            continue
        if isinstance(record, Task):
            if not canonical:
                raise _error(f"{note}: task record is not at the canonical bundle root")
            if record.fields.get("id") != note.stem:
                raise _error(f"{note}: task identity does not match canonical filename")
            if record.fields.get("id") == task_id:
                matches.append((note, record))
    if len(matches) != 1:
        raise _error(f"task {task_id!r} does not identify exactly one valid task")
    return matches[0]


def _all_tasks(root: Path) -> List[Tuple[Path, Task]]:
    records: List[Tuple[Path, Task]] = []
    for relative in ("20_任务", "90_归档"):
        for note in sorted(_managed_markdown_files(root, relative), key=lambda p: p.as_posix()):
            canonical = note.parent.name == note.stem
            if not canonical and not _has_frontmatter_prefix(note):
                # Ordinary body notes are not records and are left untouched.
                continue
            try:
                record = _parse_record_frontmatter(note)
            except Exception as exc:
                raise _error(f"{note}: invalid managed task note: {exc}") from exc
            if not isinstance(record, Task):
                if canonical:
                    raise _error(f"{note}: canonical task note has wrong kind")
                continue
            if not canonical:
                raise _error(f"{note}: task record is not at the canonical bundle root")
            if record.fields.get("id") != note.stem:
                raise _error(f"{note}: task identity does not match canonical filename")
            records.append((note, record))
    ids = {}
    for path, record in records:
        task_id = record.fields["id"]
        if task_id in ids:
            raise _error(f"duplicate task id {task_id}: {path} and {ids[task_id]}")
        ids[task_id] = path
    return records


def _parse_record_frontmatter(path: Path) -> Any:
    """Parse only a bounded frontmatter prefix, never the Markdown body."""

    lines: List[bytes] = []
    total = 0
    closing = False
    with path.open("rb") as stream:
        for line in stream:
            total += len(line)
            if total > 1024 * 1024:
                raise _error(f"{path}: frontmatter exceeds 1 MiB")
            lines.append(line)
            if line.rstrip(b"\r\n") == b"---" and len(lines) > 1:
                closing = True
                break
    if not closing:
        raise _error(f"{path}: missing frontmatter terminator")
    return parse_record_bytes(b"".join(lines) + b"\n", str(path))


def _find_capture(root: Path, capture_id: str) -> Tuple[Path, Capture]:
    if not ID_RE.fullmatch(capture_id):
        raise _error("capture id is invalid")
    matches: List[Tuple[Path, Capture]] = []
    for note in sorted(_managed_markdown_files(root, "10_收件箱"), key=lambda p: p.as_posix()):
        try:
            record = parse_record(note)
        except Exception as exc:
            if _has_frontmatter_prefix(note):
                raise _error(f"{note}: invalid capture note: {exc}") from exc
            continue
        if isinstance(record, Capture) and record.fields.get("capture_id") == capture_id:
            matches.append((note, record))
    if len(matches) != 1:
        raise _error(f"capture {capture_id!r} does not identify exactly one valid capture")
    return matches[0]


def _all_artifacts(root: Path) -> List[Tuple[Path, Artifact]]:
    records: List[Tuple[Path, Artifact]] = []
    for relative in ("20_任务", "30_资料库", "90_归档"):
        for path in sorted(_managed_markdown_files(root, relative), key=lambda item: item.as_posix()):
            if not path.name.endswith(".artifact.md"):
                continue
            if path.is_symlink() or not path.is_file():
                raise _error(f"{path}: artifact record must be a regular file")
            try:
                record = parse_record(path)
            except Exception as exc:
                raise _error(f"{path}: invalid artifact record: {exc}") from exc
            if not isinstance(record, Artifact):
                raise _error(f"{path}: *.artifact.md must have kind=artifact")
            records.append((path, record))
    return records


def _artifact_projection(path: Path, artifact: Artifact, root: Path, *, include_payload: bool = False) -> Dict[str, Any]:
    access = _record_agent_access(artifact.fields, "artifact")
    if include_payload:
        _require_agent_access(artifact.fields, "content", kind="artifact")
    item = dict(artifact.fields)
    item["agent_access"] = access
    if access == "none":
        keep = {"kind", "schema_version", "artifact_id", "owner_task", "role", "sensitivity", "agent_access", "created_at"}
        item = {key: item[key] for key in keep if key in item}
    else:
        item["record"] = path.relative_to(root).as_posix()
        item["sha256"] = artifact.digest
        if include_payload:
            payload = root / str(artifact.fields["payload_path"])
            if payload.is_symlink() or not payload.is_file():
                raise _error(f"{payload}: artifact payload is unavailable")
            item["payload_base64"] = base64.b64encode(payload.read_bytes()).decode("ascii")
    return item


def _artifact_list(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    items = [_artifact_projection(path, artifact, root) for path, artifact in _all_artifacts(root)]
    items.sort(key=lambda item: str(item.get("artifact_id", "")))
    return {"status": "ok", "operation": "artifact.list", "count": len(items), "items": items}


def _find_artifact(root: Path, artifact_id: str) -> Tuple[Path, Artifact]:
    matches = [(path, artifact) for path, artifact in _all_artifacts(root) if artifact.fields.get("artifact_id") == artifact_id]
    if len(matches) != 1:
        raise _error(f"artifact {artifact_id!r} does not identify exactly one record")
    return matches[0]


def _artifact_show(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    path, artifact = _find_artifact(root, args.artifact_id)
    return {"status": "ok", "operation": "artifact.show", "artifact": _artifact_projection(path, artifact, root, include_payload=args.include_payload)}


def _artifact_update_access(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    path, artifact = _find_artifact(root, args.artifact_id)
    old_access = _record_agent_access(artifact.fields, "artifact")
    new_access = args.agent_access
    if AGENT_ACCESS_RANK[new_access] > AGENT_ACCESS_RANK[old_access] and not (args.authorize_access and args.actor == "human"):
        raise _error("raising agent_access requires --authorize-access with --actor human")
    result = cas_update_path(
        path,
        args.expected_sha or artifact.digest,
        {"agent_access": new_access},
        actor=args.actor,
        event_type="artifact.access.updated",
        metadata={"record": path.relative_to(root).as_posix(), "from": old_access, "to": new_access},
        now=_now(config),
    )
    _event(root, result.receipt)
    return {"status": "updated" if result.changed_fields else "unchanged", "operation": "artifact.update-access", "artifact_id": args.artifact_id, "changed_fields": list(result.changed_fields), "sha256": result.after_sha256, "receipt": result.receipt.to_dict()}


def _snapshot_tree(root: Path, relative: str) -> List[Dict[str, Any]]:
    base = _safe_path(root, relative, kind="directory")
    entries: List[Dict[str, Any]] = []
    for path in sorted(base.rglob("*"), key=lambda p: p.as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise _error(f"{rel}: symlink in managed bundle")
        if path.is_dir() and path.name == ".git":
            raise _error(f"{rel}: nested Git boundary in managed bundle")
        if path.is_dir():
            entries.append({"path": rel, "kind": "directory"})
        elif path.is_file():
            entries.append({"path": rel, "kind": "file", "sha256": _sha256_file(path), "bytes": path.stat().st_size})
        else:
            raise _error(f"{rel}: unsupported file type")
    return entries


def _canonical_task_note(bundle: Path, task_id: str) -> Tuple[Path, Task]:
    """Return the one canonical task note in a bundle, failing closed."""

    expected = bundle / f"{task_id}.md"
    if expected.is_symlink() or not expected.is_file():
        raise _error(f"{bundle}: missing canonical task note {task_id}.md")
    try:
        record = parse_record(expected)
    except Exception as exc:
        raise _error(f"{expected}: invalid canonical task note: {exc}") from exc
    if not isinstance(record, Task) or record.fields.get("id") != task_id:
        raise _error(f"{expected}: canonical task identity does not match bundle")
    for candidate in bundle.glob("*.md"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.name != expected.name:
            try:
                other = parse_record(candidate)
            except Exception as exc:
                raise _error(f"{candidate}: invalid managed Markdown record: {exc}") from exc
            if isinstance(other, Task):
                raise _error(f"{bundle}: multiple task records")
    return expected, record


def _validate_bundle_artifacts(root: Path, bundle: Path, task_id: str) -> None:
    """Validate Artifact sidecars and their custody paths before archive."""

    for candidate in sorted(bundle.rglob("*.artifact.md"), key=lambda p: p.as_posix()):
        if candidate.is_symlink() or not candidate.is_file():
            raise _error(f"{candidate}: artifact sidecar must be a regular file")
        try:
            record = parse_record(candidate)
        except Exception as exc:
            raise _error(f"{candidate}: invalid artifact record: {exc}") from exc
        if not isinstance(record, Artifact):
            raise _error(f"{candidate}: *.artifact.md must have kind=artifact")
        if record.fields.get("owner_task") != task_id:
            raise _error(f"{candidate}: artifact owner_task must be {task_id}")
        payload_rel = record.fields.get("payload_path")
        if not isinstance(payload_rel, str):
            raise _error(f"{candidate}: artifact payload_path is required")
        try:
            payload = _safe_path(root, payload_rel, kind="file")
        except CLIError as exc:
            raise _error(f"{candidate}: artifact payload is unsafe: {exc}") from exc
        if bundle not in payload.parents:
            raise _error(f"{candidate}: artifact payload must remain inside task bundle")
        if _sha256_file(payload) != record.fields.get("sha256"):
            raise _error(f"{candidate}: artifact payload hash does not match sidecar")


def _archive_artifact_plan(root: Path, bundle: Path, task_id: str) -> List[Dict[str, Any]]:
    """Describe artifact sidecars whose workspace paths change on archive."""

    source_rel = bundle.relative_to(root).as_posix()
    updates: List[Dict[str, Any]] = []
    for sidecar in sorted(bundle.rglob("*.artifact.md"), key=lambda p: p.as_posix()):
        record = parse_record(sidecar)
        if not isinstance(record, Artifact):
            raise _error(f"{sidecar}: expected an artifact record")
        payload_rel = record.fields.get("payload_path")
        if not isinstance(payload_rel, str) or not (payload_rel == source_rel or payload_rel.startswith(source_rel + "/")):
            raise _error(f"{sidecar}: owner artifact payload must be inside its task bundle")
        suffix = payload_rel[len(source_rel):].lstrip("/")
        updates.append(
            {
                "record": sidecar.relative_to(root).as_posix(),
                "record_sha256": record.digest,
                "artifact_id": record.fields["artifact_id"],
                "payload_path": payload_rel,
                "payload_suffix": suffix,
            }
        )
    return updates


def _relative_destination_path(source_rel: str, destination_rel: str, source_path: str) -> str:
    if source_path == source_rel:
        return destination_rel
    prefix = source_rel + "/"
    if not source_path.startswith(prefix):
        raise _error(f"snapshot path is outside source bundle: {source_path}")
    return destination_rel + source_path[len(source_rel):]


def _expected_moved_snapshot(
    source_snapshot: Sequence[Mapping[str, Any]],
    source_rel: str,
    destination_rel: str,
    *,
    rewritten_files: Mapping[str, bytes],
) -> List[Dict[str, Any]]:
    expected: List[Dict[str, Any]] = []
    for item in source_snapshot:
        source_path = str(item["path"])
        destination_path = _relative_destination_path(source_rel, destination_rel, source_path)
        copied = dict(item)
        copied["path"] = destination_path
        if item.get("kind") == "file" and source_path in rewritten_files:
            payload = rewritten_files[source_path]
            copied["sha256"] = sha256_bytes(payload)
            copied["bytes"] = len(payload)
        expected.append(copied)
    return expected


def _snapshot_at(base: Path, display_root: str = "") -> List[Dict[str, Any]]:
    """Snapshot a directory using POSIX paths relative to *base*."""

    if base.is_symlink() or not base.is_dir():
        raise _error(f"{base}: expected a real directory")
    entries: List[Dict[str, Any]] = []
    for path in sorted(base.rglob("*"), key=lambda p: p.as_posix()):
        if path.is_symlink():
            raise _error(f"{path}: symlinks are not allowed in managed bundles")
        relative = path.relative_to(base).as_posix()
        shown = f"{display_root}/{relative}" if display_root else relative
        if path.is_dir():
            entries.append({"path": shown, "kind": "directory"})
        elif path.is_file():
            entries.append({"path": shown, "kind": "file", "sha256": _sha256_file(path), "bytes": path.stat().st_size})
        else:
            raise _error(f"{shown}: unsupported file type")
    return entries


def _remove_published_source(
    root: Path,
    source: Path,
    source_rel: str,
    expected_snapshot: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Remove a moved source only after a final CAS-style snapshot check.

    The source is first atomically renamed to a private tombstone.  This
    closes the verify→delete race: once the rename succeeds, an editor cannot
    modify the path that will be removed.  A failed cleanup leaves the
    tombstone (or the original source) in place and reports ``partial`` so a
    later reconciliation can safely inspect it; it never silently discards a
    second copy.
    """

    try:
        if _snapshot_tree(root, source_rel) != list(expected_snapshot):
            raise _error("source changed after destination verification; source retained")
        tombstone = source.parent / ("." + source.name + ".cleanup-" + secrets.token_hex(8))
        if tombstone.exists() or tombstone.is_symlink():
            raise _error("source cleanup tombstone collision")
        os.replace(source, tombstone)
        # Verify the atomically renamed directory before deleting it.  The
        # display root preserves the original workspace-relative paths.
        if _snapshot_at(tombstone, source_rel) != list(expected_snapshot):
            raise _error("source changed during atomic cleanup; tombstone retained")
        shutil.rmtree(tombstone)
        return {"status": "verified", "cleanup": "removed"}
    except Exception as exc:
        return {
            "status": "partial",
            "cleanup": "pending",
            "cleanup_error": str(exc),
            "source": source_rel,
        }


def _rewrite_staged_artifact(
    stage: Path,
    source_rel: str,
    destination_rel: str,
    item: Mapping[str, Any],
) -> Tuple[str, bytes]:
    source_record_rel = str(item["record"])
    if not source_record_rel.startswith(source_rel + "/"):
        raise _error(f"artifact record is outside source bundle: {source_record_rel}")
    record_suffix = source_record_rel[len(source_rel) + 1 :]
    target = stage / record_suffix
    if target.is_symlink() or not target.is_file():
        raise _error(f"{source_record_rel}: artifact record changed or disappeared")
    raw = target.read_bytes()
    if sha256_bytes(raw) != item.get("record_sha256"):
        raise _error(f"{source_record_rel}: artifact record changed after approval")
    record = parse_record_bytes(raw, source_record_rel)
    if not isinstance(record, Artifact) or record.fields.get("artifact_id") != item.get("artifact_id"):
        raise _error(f"{source_record_rel}: artifact identity changed")
    payload_suffix = str(item["payload_suffix"])
    new_payload = destination_rel + "/" + payload_suffix if payload_suffix else destination_rel
    updated = dict(record.fields)
    updated["payload_path"] = new_payload
    payload_target = stage / payload_suffix
    if payload_target.is_symlink() or not payload_target.is_file():
        raise _error(f"{source_record_rel}: artifact payload changed or disappeared")
    if _sha256_file(payload_target) != updated.get("sha256"):
        raise _error(f"{source_record_rel}: artifact payload hash mismatch")
    rewritten = render_frontmatter(updated, record.body).encode("utf-8")
    _safe_write(target, rewritten, replace=True)
    return source_record_rel, rewritten


def _plan_digest(plan: Mapping[str, Any]) -> str:
    payload = dict(plan)
    payload.pop("plan_digest", None)
    return sha256_bytes(_canonical_json(payload))


def _operation_dir(root: Path) -> Path:
    control = root / ".workspace-organizer"
    if control.is_symlink() or (control.exists() and not control.is_dir()):
        raise _error(".workspace-organizer must be a real directory")
    directory = control / "operations"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise _error("operations directory must not be a symlink")
    _mkdir_no_symlink(control)
    _mkdir_no_symlink(directory)
    return directory


def _write_operation(root: Path, name: str, value: Mapping[str, Any]) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name or name in {".", ".."}:
        raise _error("operation filename must be one safe path component")
    target = _operation_dir(root) / name
    _safe_write(target, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    return target


def _write_operation_result(root: Path, operation_id: str, value: Mapping[str, Any]) -> Path:
    """Persist one immutable verification receipt beside its plan."""

    operation_id = _safe_operation_id(operation_id)
    status = str(value.get("status", "verified"))
    if status not in {"verified", "partial"}:
        raise _error("verification receipt status is invalid")
    payload = {"schema_version": 2, "operation_id": operation_id, "status": status, **dict(value)}
    if "plan_digest" not in payload or not isinstance(payload.get("plan_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("plan_digest"))):
        raise _error("verification receipt must bind the approved plan digest")
    target = _operation_dir(root) / (operation_id + ".result.json")
    return _safe_write_operation_target(target, payload)


def _safe_write_operation_target(target: Path, value: Mapping[str, Any]) -> Path:
    """Write an internal operation record to a single safe filename."""

    if target.name != target.name.replace("/", "") or not target.name.endswith(".result.json"):
        raise _error("unsafe operation result filename")
    _safe_write(target, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    return target
def _init_workspace(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(os.path.abspath(str(args.root)))
    if not root.exists():
        root = _resolve_creation_path(root)
    _reject_symlink_components(root)
    if root.exists() and root.is_symlink():
        raise _error("workspace root must not be a symlink")
    if not root.exists():
        _mkdir_no_symlink(root)
    root = _root_path(root)
    config_dir = root / ".workspace-organizer"
    config_path = config_dir / "config.yaml"
    if config_dir.is_symlink() or (config_dir.exists() and not config_dir.is_dir()):
        raise _error(".workspace-organizer must be a real directory")
    if config_path.exists() and not args.force:
        raise _error("workspace is already initialized; use --force only for an explicit replacement")
    timezone_name = args.timezone or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise _error(f"invalid timezone: {timezone_name}") from exc
    workspace_id = args.workspace_id or _slug(root.name)
    if not SLUG_RE.fullmatch(workspace_id):
        raise _error("workspace-id must be a lowercase slug")
    area_items = [{"key": "general", "label": "通用", "archive_folder": "通用"}]
    if args.area:
        area_items = []
        seen_keys = set()
        seen_folders = set()
        seen_labels = set()
        for raw in args.area:
            parts = raw.split("=", 2)
            if len(parts) != 3:
                raise _error("areas use KEY=LABEL=ARCHIVE_FOLDER")
            key, label, folder = parts
            if not SLUG_RE.fullmatch(key):
                raise _error(f"invalid area key: {key}")
            _safe_single_line(label, "area label", maximum=240)
            _safe_single_line(folder, "archive folder", maximum=240)
            if key.casefold() in seen_keys:
                raise _error(f"duplicate area key: {key}")
            if _nfc(folder).casefold() in seen_folders:
                raise _error(f"duplicate normalized archive folder: {folder}")
            if _nfc(label).casefold() in seen_labels:
                raise _error(f"duplicate normalized area label: {label}")
            seen_keys.add(key.casefold())
            seen_folders.add(_nfc(folder).casefold())
            seen_labels.add(_nfc(label).casefold())
            _validate_rel(folder, "archive folder")
            if "/" in folder:
                raise _error("archive folder must be one directory name")
            area_items.append({"key": key, "label": label, "archive_folder": folder})
        if "general" not in seen_keys:
            if "通用".casefold() in seen_folders:
                raise _error("archive folder 通用 is reserved for the general area")
            if "通用".casefold() in seen_labels:
                raise _error("area label 通用 is reserved for the general area")
            area_items.append({"key": "general", "label": "通用", "archive_folder": "通用"})
    config = {
        "kind": "workspace-config",
        "schema_version": 2,
        "workspace_id": workspace_id,
        "timezone": timezone_name,
        "default_sensitivity": args.default_sensitivity,
        "areas": area_items,
    }
    config_text = "\n".join(
        [
            "kind: workspace-config",
            "schema_version: 2",
            f"workspace_id: {workspace_id}",
            f"timezone: {json.dumps(timezone_name, ensure_ascii=False)}",
            f"default_sensitivity: {args.default_sensitivity}",
            "areas: " + json.dumps(area_items, ensure_ascii=False, separators=(",", ":")),
            "",
        ]
    ).encode("utf-8")
    if not args.yes:
        return {
            "status": "preview",
            "operation": "init",
            "root": str(root),
            "workspace_id": workspace_id,
            "paths": [*ROOT_DIRS, ".workspace-organizer/config.yaml", "01_导航/HOME.md", "01_导航/FOCUS.md"],
            "message": "rerun with --yes to create the workspace",
        }
    config_dir.mkdir(parents=True, exist_ok=True)
    if config_path.exists() and args.force:
        _safe_write(config_path, config_text, replace=True)
    else:
        _safe_write(config_path, config_text)
    created: List[str] = []
    for relative in ROOT_DIRS:
        target = root / relative
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise _error(f"{relative}: must be a real directory")
            continue
        _prepare_directory(root, relative)
        created.append(relative)
    templates_root = Path(__file__).resolve().parent.parent / "assets"
    for name, default_text in (
        ("01_导航/HOME.md", "# 工作区首页\n\n打开 [[00_总览/TODAY|TODAY]] 开始今天。\n"),
        ("01_导航/FOCUS.md", "# 手动焦点\n\n在这里写入 1–3 个 task wikilink。\n"),
    ):
        target = root / name
        if not target.exists():
            source = templates_root / Path(name).name
            payload = source.read_bytes() if source.is_file() else default_text.encode("utf-8")
            _safe_write(target, payload)
            created.append(name)
    return {
        "status": "initialized",
        "operation": "init",
        "workspace_id": workspace_id,
        "timezone": timezone_name,
        "created": created,
        "config": ".workspace-organizer/config.yaml",
    }


def _task_fields(args: argparse.Namespace, config: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
    now = _now(config)
    title = args.title.strip()
    outcome = (args.outcome or title).strip()
    next_action = args.next_action if args.next_action is not None else "Define the first next action"
    configured_areas = {str(item.get("key")) for item in config.get("areas", []) if isinstance(item, Mapping)}
    if args.area not in configured_areas:
        raise _error(f"unknown area key: {args.area}")
    fields: Dict[str, Any] = {
        "kind": "task",
        "schema_version": 2,
        "id": task_id,
        "title": title,
        "outcome": outcome,
        "status": args.status,
        "storage_state": "active",
        "area": args.area,
        "type": args.type,
        "priority": args.priority,
        "scheduled_on": None if args.scheduled_on in {None, "null"} else args.scheduled_on,
        "due_on": None if args.due_on in {None, "null"} else args.due_on,
        "next_action": next_action,
        "waiting_on": None,
        "follow_up_on": None,
        "sensitivity": args.sensitivity or config["default_sensitivity"],
        "agent_access": args.agent_access or "metadata",
        "created_at": now,
        "updated_at": now,
        "started_at": now if args.status == "active" else None,
        "closed_at": None,
        "archived_at": None,
        "closure_summary": None,
        "tags": list(args.tags or []),
        "aliases": list(args.aliases or []),
    }
    validate_task(fields)
    return fields


def _create_task(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    title = args.title.strip()
    supplied = args.id
    base_id = supplied or (datetime.now(_zone(config)).strftime("%Y%m%dT%H%M%S") + "-" + _slug(title))
    task_id = base_id
    if not ID_RE.fullmatch(task_id):
        raise _error("task id contains unsafe characters")
    active_dir = root / "20_任务" / task_id
    if active_dir.exists():
        if supplied:
            raise _error(f"task id already exists: {task_id}")
        suffix = 2
        while (root / "20_任务" / f"{task_id}-{suffix}").exists():
            suffix += 1
        task_id = f"{task_id}-{suffix}"
        active_dir = root / "20_任务" / task_id
    fields = _task_fields(args, config, task_id)
    body = args.body or f"# {title}\n\n## 结果\n\n## 工作记录\n\n## 复盘\n"
    note = active_dir / f"{task_id}.md"
    if not args.yes:
        return {
            "status": "preview",
            "operation": "task.create",
            "task_id": task_id,
            "record": note.relative_to(root).as_posix(),
            "message": "rerun with --yes to create the canonical task",
        }
    _prepare_directory(root, f"20_任务/{task_id}")
    payload = render_frontmatter(fields, body).encode("utf-8")
    _safe_write(note, payload)
    from clean_slate_model import EventReceipt
    receipt = EventReceipt.create(
        event_type="task.created",
        entity_kind="task",
        entity_id=task_id,
        actor=args.actor,
        before_sha256=None,
        after_sha256=sha256_bytes(payload),
        changed_fields=tuple(sorted(fields.keys())),
        metadata={"record": note.relative_to(root).as_posix()},
        occurred_at=fields["created_at"],
    )
    _event(root, receipt)
    return {
        "status": "created",
        "operation": "task.create",
        "task_id": task_id,
        "record": note.relative_to(root).as_posix(),
        "sha256": sha256_bytes(payload),
        "receipt": receipt.to_dict(),
    }


def _record_agent_access(record: Mapping[str, Any], kind: Optional[str] = None) -> str:
    try:
        value = effective_agent_access(record, kind=kind)
    except ModelError as exc:
        raise _error(str(exc)) from exc
    if value not in AGENT_ACCESS_RANK:
        raise _error("record.agent_access: unknown access policy")
    return value


def _require_agent_access(record: Mapping[str, Any], required: str, *, kind: Optional[str] = None) -> str:
    actual = _record_agent_access(record, kind)
    if AGENT_ACCESS_RANK[actual] < AGENT_ACCESS_RANK[required]:
        raise _error(f"agent access {required!r} is required; record grants {actual!r}")
    return actual


def _task_projection(path: Path, task: Task, root: Path, *, include_body: bool = False) -> Dict[str, Any]:
    access = _record_agent_access(task.fields, "task")
    item = dict(task.fields)
    item["agent_access"] = access
    if access == "none":
        keep = {"kind", "schema_version", "id", "status", "storage_state", "area", "type", "priority", "scheduled_on", "due_on", "follow_up_on", "sensitivity", "agent_access"}
        item = {key: item[key] for key in keep if key in item}
        item["title"] = "[restricted]"
    else:
        item.update(record=path.relative_to(root).as_posix(), sha256=_sha256_file(path))
    if include_body:
        _require_agent_access(task.fields, "content", kind="task")
        item["body"] = task.body
    return item


def _task_list(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    records = _all_tasks(root)
    visible = records
    if args.status:
        visible = [(path, task) for path, task in visible if task.fields["status"] == args.status]
    if args.area:
        visible = [(path, task) for path, task in visible if task.fields["area"] == args.area]
    if args.open_only:
        visible = [(path, task) for path, task in visible if task.fields["status"] in {"planned", "active", "waiting", "blocked"} and task.fields["storage_state"] == "active"]
    items = []
    for path, task in sorted(visible, key=lambda item: (str(item[1].fields.get("due_on") or "9999-12-31"), str(item[1].fields.get("priority")), item[1].fields["id"])):
        item = _task_projection(path, task, root)
        items.append(item)
    return {"status": "ok", "operation": "task.list", "count": len(items), "items": items}


def _task_show(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    path, task = _find_task(root, args.task_id, metadata_only=not args.include_body)
    item = _task_projection(path, task, root, include_body=args.include_body)
    return {"status": "ok", "operation": "task.show", "task": item}


def _parse_optional_field(value: Optional[str]) -> Optional[str]:
    if value is None or value == "null":
        return None
    return value


def _task_update(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    path, task = _find_task(root, args.task_id)
    updates: Dict[str, Any] = {}
    for field in ("title", "outcome", "area", "type", "priority", "sensitivity", "agent_access", "next_action", "waiting_on"):
        value = getattr(args, field, None)
        if value is not None:
            updates[field] = _parse_optional_field(value)
    for field in ("scheduled_on", "due_on", "follow_up_on"):
        value = getattr(args, field, None)
        if value is not None:
            updates[field] = _parse_optional_field(value)
    if args.tags is not None:
        updates["tags"] = [item for item in args.tags.split(",") if item]
    if args.aliases is not None:
        updates["aliases"] = [item for item in args.aliases.split(",") if item]
    if any(field != "agent_access" for field in updates):
        _require_agent_access(task.fields, "metadata", kind="task")
    if "area" in updates:
        configured_areas = {
            str(item.get("key"))
            for item in config.get("areas", [])
            if isinstance(item, Mapping)
        }
        if updates["area"] not in configured_areas:
            raise _error(f"unknown area key: {updates['area']}")
    if "sensitivity" in updates:
        old_rank = SENSITIVITY_RANK.get(str(task.fields.get("sensitivity")), 99)
        new_rank = SENSITIVITY_RANK.get(str(updates["sensitivity"]), 99)
        if new_rank < old_rank:
            raise _error("lowering sensitivity requires an approved structural plan")
    if "agent_access" in updates:
        old_access = _record_agent_access(task.fields, "task")
        new_access = str(updates["agent_access"])
        if AGENT_ACCESS_RANK[new_access] > AGENT_ACCESS_RANK[old_access] and not (args.authorize_access and args.actor == "human"):
            raise _error("raising agent_access requires --authorize-access with --actor human")
    if not updates:
        raise _error("task update requires at least one field")
    expected = args.expected_sha or task.digest
    result = cas_update_path(
        path,
        expected,
        updates,
        actor=args.actor,
        event_type="task.updated",
        metadata={"record": path.relative_to(root).as_posix()},
        now=_now_after(config, task.fields.get("updated_at")),
    )
    _event(root, result.receipt)
    return {
        "status": "updated" if result.changed_fields else "unchanged",
        "operation": "task.update",
        "task_id": args.task_id,
        "record": path.relative_to(root).as_posix(),
        "changed_fields": list(result.changed_fields),
        "sha256": result.after_sha256,
        "receipt": result.receipt.to_dict(),
    }


def _transition(args: argparse.Namespace, target: str) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    path, task = _find_task(root, args.task_id)
    _require_agent_access(task.fields, "metadata", kind="task")
    kwargs: Dict[str, Any] = {"now": _now_after(config, task.fields.get("updated_at"))}
    if target in {"completed", "cancelled"}:
        if not args.summary:
            raise _error("closing a task requires --summary")
        kwargs["closure_summary"] = args.summary
    if target == "active" and task.fields["status"] in {"completed", "cancelled"}:
        kwargs["next_action"] = args.next_action or "Define the next action"
    if target == "waiting":
        kwargs["waiting_on"] = args.waiting_on
        kwargs["follow_up_on"] = _parse_optional_field(args.follow_up_on)
    updates = transition_task(task.fields, target, **kwargs)
    changed = {key: value for key, value in updates.items() if task.fields.get(key) != value}
    result = cas_update_path(
        path,
        args.expected_sha or task.digest,
        changed,
        actor=args.actor,
        event_type=f"task.status.{target}",
        metadata={"record": path.relative_to(root).as_posix(), "from": task.fields["status"], "to": target},
        now=kwargs["now"],
        touch_updated_at=False,
    )
    _event(root, result.receipt)
    return {
        "status": "updated",
        "operation": f"task.{target}",
        "task_id": args.task_id,
        "record": path.relative_to(root).as_posix(),
        "from": task.fields["status"],
        "to": target,
        "sha256": result.after_sha256,
        "receipt": result.receipt.to_dict(),
    }

def _copy_verified(source: Path, destination: Path) -> Tuple[str, int]:
    if source.is_symlink() or not source.is_file():
        raise _error(f"{source}: source must be a no-follow regular file")
    _reject_symlink_components(source)
    before = source.stat()
    _reject_symlink_components(destination.parent)
    _mkdir_no_symlink(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise _error(f"{destination}: destination already exists")
    temporary = destination.with_name("." + destination.name + ".copy-" + secrets.token_hex(8))
    digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(chunk)
                total += len(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = source.stat()
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise _error(f"{source}: source changed while copying")
        if total != before.st_size:
            raise _error(f"{source}: copied byte count changed")
        if destination.parent.is_symlink():
            raise _error(f"{destination.parent}: destination parent changed to symlink")
        if destination.exists() or destination.is_symlink():
            raise _error(f"{destination}: destination appeared during copy")
        # The destination is create-only.  A hard-link publish is the
        # no-overwrite primitive on POSIX; unlike os.replace it cannot clobber
        # a file that appeared after the preflight check.
        os.link(str(temporary), str(destination), follow_symlinks=False)
        temporary.unlink()
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return digest.hexdigest(), total


def _capture_create(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    if not args.text and not args.file:
        raise _error("capture create requires --text or --file")
    title = (args.title or (Path(args.file).name if args.file else args.text[:80])).strip()
    capture_id = args.id or (datetime.now(_zone(config)).strftime("%Y%m%dT%H%M%S") + "-" + _slug(title))
    if not ID_RE.fullmatch(capture_id):
        raise _error("capture id contains unsafe characters")
    _prepare_directory(root, "10_收件箱")
    note_path = root / "10_收件箱" / f"{capture_id}.md"
    if note_path.exists():
        raise _error(f"capture id already exists: {capture_id}")
    payload_path: Optional[str] = None
    source_value: str = args.source or "manual"
    external: Optional[Path] = None
    if args.file:
        external_input = Path(args.file)
        _reject_symlink_components(external_input)
        if external_input.is_symlink():
            raise _error("capture source symlinks are not trusted")
        try:
            external = external_input.resolve(strict=True)
        except OSError as exc:
            raise _error(f"capture source cannot be resolved: {exc}") from exc
        if external.is_symlink() or not external.is_file():
            raise _error("capture source must be a regular file")
        safe_name = _nfc(external.name)
        if safe_name in {"", ".", ".."} or "/" in safe_name or "\\" in safe_name:
            raise _error("capture source filename is unsafe")
        payload_path = f"10_收件箱/{capture_id}/{safe_name}"
        source_value = args.source or ("file:" + safe_name)
    timestamp = _now(config)
    fields: Dict[str, Any] = {
        "kind": "capture",
        "schema_version": 2,
        "capture_id": capture_id,
        "title": title,
        "status": "inbox",
        "capture_type": "file" if external else args.capture_type,
        "payload_path": payload_path,
        "source": source_value,
        "sensitivity": args.sensitivity or config["default_sensitivity"],
        "agent_access": args.agent_access or "metadata",
        "captured_at": timestamp,
        "updated_at": timestamp,
        "triaged_at": None,
        "disposition": None,
        "target_task": None,
        "tags": list(args.tags or ["capture"]),
    }
    validate_capture(fields)
    preview = {
        "status": "preview",
        "operation": "capture.create",
        "capture_id": capture_id,
        "record": note_path.relative_to(root).as_posix(),
        "payload": payload_path,
        "source_preserved": True,
    }
    if not args.yes:
        preview["message"] = "rerun with --yes to write the capture and copy its payload"
        return preview
    payload_receipt: Optional[Dict[str, Any]] = None
    payload_destination: Optional[Path] = None
    try:
        if external and payload_path:
            _prepare_directory(root, f"10_收件箱/{capture_id}")
            payload_destination = root / payload_path
            digest, size = _copy_verified(external, payload_destination)
            payload_receipt = {"path": payload_path, "sha256": digest, "bytes": size}
        body = (args.text or "").rstrip() + "\n"
        note_bytes = render_frontmatter(fields, body).encode("utf-8")
        _safe_write(note_path, note_bytes)
    except Exception:
        if payload_destination is not None:
            try:
                payload_destination.unlink(missing_ok=True)
                parent = payload_destination.parent
                if parent == root / "10_收件箱" / capture_id and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
        raise
    from clean_slate_model import EventReceipt
    receipt = EventReceipt.create(
        event_type="capture.created",
        entity_kind="capture",
        entity_id=capture_id,
        actor=args.actor,
        after_sha256=sha256_bytes(note_bytes),
        changed_fields=tuple(sorted(fields.keys())),
        metadata={"record": note_path.relative_to(root).as_posix(), "payload": payload_receipt},
        occurred_at=timestamp,
    )
    _event(root, receipt)
    return {
        "status": "created",
        "operation": "capture.create",
        "capture_id": capture_id,
        "record": note_path.relative_to(root).as_posix(),
        "sha256": sha256_bytes(note_bytes),
        "payload": payload_receipt,
        "source_preserved": True,
        "receipt": receipt.to_dict(),
    }


def _capture_list(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    records = collect_records(root)["captures"]
    items = []
    for raw in records:
        access = _record_agent_access(raw, "capture")
        if args.status and raw.get("status", raw.get("triage_state")) != args.status:
            continue
        item = dict(raw)
        item["agent_access"] = access
        if access == "none":
            allowed = {"kind", "schema_version", "capture_id", "id", "status", "capture_type", "sensitivity", "agent_access", "captured_at", "updated_at"}
            item = {key: item[key] for key in allowed if key in item}
            item["title"] = "[restricted]"
        items.append(item)
    items.sort(key=lambda item: (str(item.get("captured_at") or ""), str(item.get("id") or item.get("capture_id") or "")))
    return {"status": "ok", "operation": "capture.list", "count": len(items), "items": items}


def _load_json_file(path: Path) -> Tuple[Dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise _error(f"{path}: expected a regular JSON file")
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _error(f"{path}: invalid JSON") from exc
    if not isinstance(value, dict):
        raise _error(f"{path}: JSON value must be an object")
    return value, payload


def _write_plan_file(path: Path, plan: Mapping[str, Any]) -> Path:
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    _safe_write(path, payload)
    return path


def _make_plan(root: Path, operation: str, details: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "schema_version": 2,
        "operation": operation,
        "operation_id": datetime.now(_zone(config)).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(8),
        "workspace_id": config["workspace_id"],
        "created_at": _now(config),
    }
    plan.update(copy.deepcopy(dict(details)))
    plan["plan_digest"] = _plan_digest(plan)
    return plan


def _approve(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.yes:
        raise _error("approval requires --yes")
    plan, plan_bytes = _load_json_file(Path(args.plan))
    if plan.get("schema_version") != 2 or plan.get("plan_digest") != _plan_digest(plan):
        raise _error("plan digest is invalid")
    approval = {
        "schema_version": 2,
        "operation_id": plan.get("operation_id"),
        "operation": plan.get("operation"),
        "plan_digest": plan.get("plan_digest"),
        "plan_file_sha256": sha256_bytes(plan_bytes),
        "approved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "approved": True,
    }
    output = Path(args.output)
    _safe_write(output, json.dumps(approval, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    return {"status": "approved", "operation": plan.get("operation"), "operation_id": plan.get("operation_id"), "approval": str(output), "plan_file_sha256": approval["plan_file_sha256"]}


def _verify_approval(plan: Mapping[str, Any], plan_bytes: bytes, approval: Mapping[str, Any]) -> None:
    if not isinstance(plan, Mapping) or plan.get("schema_version") != 2:
        raise _error("plan schema_version must be 2")
    _safe_operation_id(plan.get("operation_id"), "plan.operation_id")
    if not isinstance(plan.get("operation"), str) or not ID_RE.fullmatch(str(plan.get("operation"))):
        raise _error("plan.operation is invalid")
    if not isinstance(plan.get("workspace_id"), str) or not SLUG_RE.fullmatch(str(plan.get("workspace_id"))):
        raise _error("plan.workspace_id is invalid")
    if not isinstance(plan.get("created_at"), str):
        raise _error("plan.created_at is required")
    try:
        parse_timestamp(plan["created_at"], "plan.created_at")
    except ModelError as exc:
        raise _error(str(exc)) from exc
    if approval.get("approved") is not True:
        raise _error("approval is not affirmative")
    if approval.get("schema_version") != 2:
        raise _error("approval schema_version must be 2")
    _safe_operation_id(approval.get("operation_id"), "approval.operation_id")
    if not isinstance(approval.get("approved_at"), str):
        raise _error("approval.approved_at is required")
    try:
        parse_timestamp(approval["approved_at"], "approval.approved_at")
    except ModelError as exc:
        raise _error(str(exc)) from exc
    for key in ("operation_id", "operation", "plan_digest"):
        if approval.get(key) != plan.get(key):
            raise _error(f"approval does not match plan field {key}")
    if not isinstance(plan.get("plan_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("plan_digest"))):
        raise _error("plan_digest is invalid")
    if approval.get("plan_file_sha256") != sha256_bytes(plan_bytes):
        raise _error("approval does not match exact plan bytes")
    if plan.get("plan_digest") != _plan_digest(plan):
        raise _error("plan digest is invalid")


def _triage_plan(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    capture_path, capture = _find_capture(root, args.capture_id)
    _require_agent_access(capture.fields, "metadata", kind="capture")
    if capture.fields["status"] != "inbox":
        raise _error("only inbox captures can be triaged")
    disposition = args.disposition
    timestamp = _now_after(config, capture.fields.get("updated_at"))
    details: Dict[str, Any] = {
        "capture_id": args.capture_id,
        "capture_record": capture_path.relative_to(root).as_posix(),
        "capture_sha256": capture.digest,
        "disposition": disposition,
        "target_task": args.task_id,
        "source_preserved": True,
        "timestamp": timestamp,
    }
    if disposition == "task" and not args.task_id:
        title = args.title or capture.fields["title"]
        task_id = datetime.now(_zone(config)).strftime("%Y%m%dT%H%M%S") + "-" + _slug(title)
        if (root / "20_任务" / task_id).exists():
            task_id += "-" + secrets.token_hex(2)
        area_key = args.area or "general"
        if area_key not in {str(item.get("key")) for item in config.get("areas", []) if isinstance(item, Mapping)}:
            raise _error(f"unknown area key: {area_key}")
        fields = {
            "kind": "task",
            "schema_version": 2,
            "id": task_id,
            "title": title,
            "outcome": args.outcome or title,
            "status": "planned",
            "storage_state": "active",
            "area": area_key,
            "type": args.type or "general",
            "priority": args.priority or "normal",
            "scheduled_on": None,
            "due_on": args.due_on,
            "next_action": args.next_action or "Review the captured input",
            "waiting_on": None,
            "follow_up_on": None,
            "sensitivity": capture.fields["sensitivity"],
            "agent_access": "metadata",
            "created_at": timestamp,
            "updated_at": timestamp,
            "started_at": None,
            "closed_at": None,
            "archived_at": None,
            "closure_summary": None,
            "tags": ["task/" + area_key],
            "aliases": [title],
        }
        validate_task(fields)
        details["new_task"] = fields
        details["target_task"] = task_id
    elif disposition in {"artifact", "task"}:
        if not details["target_task"]:
            raise _error("attaching a capture requires --task-id, or omit it with disposition=task to create a task")
        target_path, target_task = _find_task(root, str(details["target_task"]))
        _require_agent_access(target_task.fields, "metadata", kind="task")
        if target_task.fields["storage_state"] != "active":
            raise _error("cannot attach to an archived task")
        details["target_task_record"] = target_path.relative_to(root).as_posix()
        details["target_task_sha256"] = target_task.digest
    elif disposition == "library":
        details["target_task"] = None
    elif disposition == "defer":
        details["target_task"] = None
    else:
        raise _error("unsupported triage disposition")
    plan = _make_plan(root, "triage", details, config)
    output = Path(args.output) if args.output else _operation_dir(root) / (plan["operation_id"] + ".plan.json")
    _write_plan_file(output, plan)
    return {"status": "planned", "operation": "triage", "operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"], "plan": str(output), "source_preserved": True}


def _artifact_fields(
    *,
    artifact_id: str,
    payload_relative: str,
    owner_task: Optional[str],
    role: str,
    sensitivity: str,
    provenance: Mapping[str, Any],
    payload_path: Path,
    timestamp: str,
    agent_access: str = "none",
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "kind": "artifact",
        "schema_version": 2,
        "artifact_id": artifact_id,
        "payload_path": payload_relative,
        "owner_task": owner_task,
        "role": role,
        "sensitivity": sensitivity,
        "agent_access": agent_access,
        "provenance": dict(provenance),
        "sha256": _sha256_file(payload_path),
        "derived_from": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    validate_artifact(fields)
    return fields


def _triage_apply(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    plan, plan_bytes = _load_json_file(Path(args.plan))
    approval, _ = _load_json_file(Path(args.approval))
    _verify_approval(plan, plan_bytes, approval)
    if plan.get("operation") != "triage" or plan.get("workspace_id") != config["workspace_id"]:
        raise _error("triage plan targets another workspace")
    capture_path, capture = _find_capture(root, str(plan["capture_id"]))
    _require_agent_access(capture.fields, "metadata", kind="capture")
    if capture.digest != plan.get("capture_sha256"):
        raise _error("capture changed after the plan was approved")
    disposition = str(plan["disposition"])
    timestamp = str(plan["timestamp"])
    target_task_id = plan.get("target_task")
    created_paths: List[Path] = []
    artifact_receipt: Optional[Dict[str, Any]] = None
    try:
        if "new_task" in plan:
            fields = dict(plan["new_task"])
            validate_task(fields)
            target_task_id = fields["id"]
            bundle = root / "20_任务" / target_task_id
            if bundle.exists():
                raise _error("planned task destination now exists")
            _prepare_directory(root, f"20_任务/{target_task_id}")
            created_paths.append(bundle)
            note = bundle / f"{target_task_id}.md"
            body = f"# {fields['title']}\n\n## 结果\n\n## 工作记录\n\n## 复盘\n"
            _safe_write(note, render_frontmatter(fields, body).encode("utf-8"))
        if disposition in {"task", "artifact", "library"}:
            source_rel = capture.fields.get("payload_path") or capture_path.relative_to(root).as_posix()
            source = _safe_path(root, str(source_rel), kind="file")
            artifact_id = datetime.fromisoformat(timestamp).strftime("%Y%m%dT%H%M%S") + "-" + _slug(capture.fields["title"])
            safe_name = source.name
            if disposition == "library":
                payload_rel = f"30_资料库/{artifact_id}/{safe_name}"
                record_rel = f"30_资料库/{artifact_id}/{artifact_id}.artifact.md"
                owner = None
                role = "library"
            else:
                if not target_task_id:
                    raise _error("triage plan has no target task")
                target_path, target_task = _find_task(root, str(target_task_id))
                if target_task.fields["storage_state"] != "active":
                    raise _error("target task is not active storage")
                expected_target_sha = plan.get("target_task_sha256")
                if expected_target_sha and target_task.digest != expected_target_sha:
                    raise _error("target task changed after triage approval")
                payload_rel = f"20_任务/{target_task_id}/01_输入/{safe_name}"
                record_rel = f"20_任务/{target_task_id}/04_记录/{artifact_id}.artifact.md"
                owner = str(target_task_id)
                role = "input"
            payload_destination = root / payload_rel
            _prepare_directory(root, payload_destination.relative_to(root).parent.as_posix())
            digest, size = _copy_verified(source, payload_destination)
            created_paths.append(payload_destination)
            fields = _artifact_fields(
                artifact_id=artifact_id,
                payload_relative=payload_rel,
                owner_task=owner,
                role=role,
                sensitivity=capture.fields["sensitivity"],
                agent_access="none",
                provenance={"kind": "capture", "capture_id": capture.fields["capture_id"], "source_sha256": capture.digest},
                payload_path=payload_destination,
                timestamp=timestamp,
            )
            artifact_note = root / record_rel
            _prepare_directory(root, artifact_note.relative_to(root).parent.as_posix())
            _safe_write(artifact_note, render_frontmatter(fields, f"# {capture.fields['title']}\n").encode("utf-8"))
            created_paths.append(artifact_note)
            artifact_receipt = {"artifact_id": artifact_id, "record": record_rel, "payload": payload_rel, "sha256": digest, "bytes": size}
        updates = {
            "status": "deferred" if disposition == "defer" else "triaged",
            "updated_at": timestamp,
            "triaged_at": None if disposition == "defer" else timestamp,
            "disposition": "defer" if disposition == "defer" else disposition,
            "target_task": target_task_id,
        }
        # Deferred captures remain in the Inbox flow with an explicit defer
        # disposition, but do not have a completed-triage timestamp.
        result = cas_update_path(
            capture_path,
            capture.digest,
            updates,
            actor=args.actor,
            event_type="capture.triaged" if disposition != "defer" else "capture.deferred",
            metadata={"operation_id": plan["operation_id"], "artifact": artifact_receipt, "source_preserved": True},
            now=timestamp,
            touch_updated_at=False,
        )
        _event(root, result.receipt)
    except Exception:
        # Only paths created by this apply attempt are eligible for rollback.
        for path in reversed(created_paths):
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError:
                pass
        raise
    _write_operation_result(
        root,
        str(plan["operation_id"]),
        {"operation": "triage", "plan_digest": plan["plan_digest"], "capture_id": capture.fields["capture_id"], "disposition": disposition, "target_task": target_task_id, "artifact": artifact_receipt},
    )
    return {
        "status": "verified",
        "operation": "triage",
        "operation_id": plan["operation_id"],
        "capture_id": capture.fields["capture_id"],
        "disposition": disposition,
        "target_task": target_task_id,
        "artifact": artifact_receipt,
        "source_preserved": True,
    }


def _archive_plan(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    path, task = _find_task(root, args.task_id)
    _require_agent_access(task.fields, "metadata", kind="task")
    if task.fields["storage_state"] != "active" or task.fields["status"] not in {"completed", "cancelled"}:
        raise _error("only active completed/cancelled tasks can be archived")
    unresolved: List[str] = []
    bundle = path.parent
    canonical_note, canonical_task = _canonical_task_note(bundle, args.task_id)
    if canonical_note != path:
        raise _error("task lookup did not resolve the canonical task note")
    _validate_bundle_artifacts(root, bundle, args.task_id)
    artifact_updates = _archive_artifact_plan(root, bundle, args.task_id)
    for child in bundle.rglob("*"):
        if child.is_symlink():
            raise _error(f"{child}: symlink in task bundle")
        if child.is_file():
            relative = child.relative_to(bundle).as_posix()
            if relative == path.name:
                continue
            first = PurePosixPath(relative).parts[0]
            if first not in set(ROLE_DIRS.values()):
                unresolved.append(child.relative_to(root).as_posix())
    if unresolved:
        raise _error("task bundle has unassigned content: " + ", ".join(sorted(unresolved)))
    area_folder = _area_folder(config, task.fields["area"])
    year = str(task.fields["closed_at"])[:4]
    destination_rel = f"90_归档/{area_folder}/{year}/{args.task_id}"
    destination = root / destination_rel
    if destination.exists() or destination.is_symlink():
        raise _error(f"archive destination already exists: {destination_rel}")
    snapshot = _snapshot_tree(root, bundle.relative_to(root).as_posix())
    plan = _make_plan(
        root,
        "archive",
        {
            "task_id": args.task_id,
            "source": bundle.relative_to(root).as_posix(),
            "destination": destination_rel,
            "source_snapshot": snapshot,
            "task_sha256": task.digest,
            "artifact_updates": artifact_updates,
            "archive_at": _now_after(config, task.fields.get("updated_at")),
        },
        config,
    )
    output = Path(args.output) if args.output else _operation_dir(root) / (plan["operation_id"] + ".plan.json")
    _write_plan_file(output, plan)
    return {"status": "planned", "operation": "archive", "operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"], "plan": str(output), "destination": destination_rel}


def _archive_apply(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    plan, plan_bytes = _load_json_file(Path(args.plan))
    approval, _ = _load_json_file(Path(args.approval))
    _verify_approval(plan, plan_bytes, approval)
    if plan.get("operation") != "archive" or plan.get("workspace_id") != config["workspace_id"]:
        raise _error("archive plan targets another workspace")
    source_rel = _validate_rel(str(plan.get("source")), "archive.source")
    destination_rel = _validate_rel(str(plan.get("destination")), "archive.destination")
    task_id = str(plan.get("task_id"))
    if source_rel != f"20_任务/{task_id}":
        raise _error("archive plan source is not the canonical active task bundle")
    source = _safe_path(root, source_rel, kind="directory")
    destination = _safe_path(root, destination_rel, allow_missing=True)
    if destination.exists() or destination.is_symlink():
        raise _error("archive destination exists after approval")
    _, task = _canonical_task_note(source, task_id)
    _require_agent_access(task.fields, "metadata", kind="task")
    if task.digest != plan.get("task_sha256"):
        raise _error("canonical task note changed after archive approval")
    expected_destination = f"90_归档/{_area_folder(config, task.fields['area'])}/{str(task.fields['closed_at'])[:4]}/{task_id}"
    if destination_rel != expected_destination:
        raise _error("archive plan destination disagrees with task area/year policy")
    _validate_bundle_artifacts(root, source, task_id)
    expected_artifact_updates = _archive_artifact_plan(root, source, task_id)
    if plan.get("artifact_updates") != expected_artifact_updates:
        raise _error("archive plan artifact custody changed or is incomplete")
    current_snapshot = _snapshot_tree(root, source_rel)
    if current_snapshot != plan.get("source_snapshot"):
        raise _error("task bundle changed after archive approval")

    parent = destination.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise _error("archive destination parent is not a real directory")
    _prepare_directory(root, parent.relative_to(root).as_posix())
    stage = parent / ("." + destination.name + ".stage-" + secrets.token_hex(8))
    if stage.exists() or stage.is_symlink():
        raise _error("archive staging collision")
    published = False
    try:
        shutil.copytree(source, stage, symlinks=False)
        staged_note, staged_task = _canonical_task_note(stage, task_id)
        archived = archive_task(staged_task, now=str(plan["archive_at"]))
        archived_bytes = render_frontmatter(archived, staged_task.body).encode("utf-8")
        _safe_write(staged_note, archived_bytes, replace=True)

        # Rewrite Artifact sidecars so their workspace-relative payload paths
        # remain truthful after the bundle move, while retaining all bytes and
        # unknown frontmatter/body content.
        rewritten: Dict[str, bytes] = {str(staged_note.relative_to(stage).as_posix()): archived_bytes}
        for item in plan.get("artifact_updates", []):
            record_rel, artifact_bytes = _rewrite_staged_artifact(stage, source_rel, destination_rel, item)
            rewritten[record_rel[len(source_rel) + 1 :]] = artifact_bytes

        if _snapshot_tree(root, source_rel) != current_snapshot:
            raise _error("source changed while staging archive")
        # Publish the fully verified stage.  Do not mkdir destination first:
        # os.replace is the atomic no-existing-destination boundary.
        if destination.exists() or destination.is_symlink():
            raise _error("archive destination appeared before publish")
        os.replace(stage, destination)
        published = True

        expected = _expected_moved_snapshot(
            plan["source_snapshot"],
            source_rel,
            destination_rel,
            rewritten_files={
                source_rel + "/" + key: value
                for key, value in rewritten.items()
            },
        )
        actual = _snapshot_at(destination, destination_rel)
        if actual != expected:
            raise _error("archived destination failed exact snapshot verification")
        final_note = destination / f"{plan['task_id']}.md"
        final_record = parse_record(final_note)
        if not isinstance(final_record, Task) or final_record.fields["storage_state"] != "archived":
            raise _error("archived task metadata verification failed")
        _validate_bundle_artifacts(root, destination, task_id)
    except Exception:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    cleanup = _remove_published_source(root, source, source_rel, current_snapshot)
    receipt = __import__("clean_slate_model").EventReceipt.create(
        event_type="task.archived",
        entity_kind="task",
        entity_id=task_id,
        actor=args.actor,
        result=str(cleanup["status"]),
        before_sha256=plan["task_sha256"],
        after_sha256=_sha256_file(destination / (task_id + ".md")),
        metadata={"source": source_rel, "destination": destination_rel, "operation_id": plan["operation_id"], "cleanup": cleanup},
        occurred_at=plan["archive_at"],
    )
    _write_operation_result(
        root,
        str(plan["operation_id"]),
        {"status": cleanup["status"], "operation": "archive", "plan_digest": plan["plan_digest"], "task_id": task_id, "source": source_rel, "destination": destination_rel, "receipt": receipt.to_dict(), "cleanup": cleanup},
    )
    _event(root, receipt)
    return {"status": cleanup["status"], "operation": "archive", "operation_id": plan["operation_id"], "task_id": plan["task_id"], "source": source_rel, "destination": destination_rel, "receipt": receipt.to_dict(), "cleanup": cleanup}


def _restore_plan(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    path, task = _find_task(root, args.task_id)
    _require_agent_access(task.fields, "metadata", kind="task")
    if task.fields["storage_state"] != "archived" or task.fields["status"] not in {"completed", "cancelled"}:
        raise _error("only archived completed/cancelled tasks can be restored")
    source = path.parent
    source_rel = source.relative_to(root).as_posix()
    if not source_rel.startswith("90_归档/"):
        raise _error("restore source must be under 90_归档")
    _canonical_task_note(source, args.task_id)
    _validate_bundle_artifacts(root, source, args.task_id)
    artifact_updates = _archive_artifact_plan(root, source, args.task_id)
    destination_rel = f"20_任务/{args.task_id}"
    destination = root / destination_rel
    if destination.exists() or destination.is_symlink():
        raise _error(f"restore destination already exists: {destination_rel}")
    snapshot = _snapshot_tree(root, source_rel)
    plan = _make_plan(
        root,
        "restore",
        {
            "task_id": args.task_id,
            "source": source_rel,
            "destination": destination_rel,
            "source_snapshot": snapshot,
            "task_sha256": task.digest,
            "artifact_updates": artifact_updates,
            "restore_at": _now_after(config, task.fields.get("updated_at")),
        },
        config,
    )
    output = Path(args.output) if args.output else _operation_dir(root) / (plan["operation_id"] + ".plan.json")
    _write_plan_file(output, plan)
    return {"status": "planned", "operation": "restore", "operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"], "plan": str(output), "destination": destination_rel}


def _restore_apply(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    plan, plan_bytes = _load_json_file(Path(args.plan))
    approval, _ = _load_json_file(Path(args.approval))
    _verify_approval(plan, plan_bytes, approval)
    if plan.get("operation") != "restore" or plan.get("workspace_id") != config["workspace_id"]:
        raise _error("restore plan targets another workspace")
    source_rel = _validate_rel(str(plan.get("source")), "restore.source")
    destination_rel = _validate_rel(str(plan.get("destination")), "restore.destination")
    task_id = str(plan.get("task_id"))
    if destination_rel != f"20_任务/{task_id}" or not source_rel.startswith("90_归档/") or not source_rel.endswith("/" + task_id):
        raise _error("restore plan paths do not match canonical task locations")
    source = _safe_path(root, source_rel, kind="directory")
    destination = _safe_path(root, destination_rel, allow_missing=True)
    if destination.exists() or destination.is_symlink():
        raise _error("restore destination exists after approval")
    _, task = _canonical_task_note(source, task_id)
    _require_agent_access(task.fields, "metadata", kind="task")
    if task.fields["storage_state"] != "archived" or task.digest != plan.get("task_sha256"):
        raise _error("archived task changed after restore approval")
    _validate_bundle_artifacts(root, source, task_id)
    expected_artifact_updates = _archive_artifact_plan(root, source, task_id)
    if plan.get("artifact_updates") != expected_artifact_updates:
        raise _error("restore plan artifact custody changed or is incomplete")
    current_snapshot = _snapshot_tree(root, source_rel)
    if current_snapshot != plan.get("source_snapshot"):
        raise _error("archived bundle changed after restore approval")
    parent = destination.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise _error("restore destination parent is not a real directory")
    _prepare_directory(root, parent.relative_to(root).as_posix())
    stage = parent / ("." + destination.name + ".stage-" + secrets.token_hex(8))
    if stage.exists() or stage.is_symlink():
        raise _error("restore staging collision")
    published = False
    try:
        shutil.copytree(source, stage, symlinks=False)
        staged_note, staged_task = _canonical_task_note(stage, task_id)
        restored = restore_task(staged_task, now=str(plan["restore_at"]))
        restored_bytes = render_frontmatter(restored, staged_task.body).encode("utf-8")
        _safe_write(staged_note, restored_bytes, replace=True)
        rewritten: Dict[str, bytes] = {staged_note.relative_to(stage).as_posix(): restored_bytes}
        for item in plan.get("artifact_updates", []):
            record_rel, artifact_bytes = _rewrite_staged_artifact(stage, source_rel, destination_rel, item)
            rewritten[record_rel[len(source_rel) + 1 :]] = artifact_bytes
        if _snapshot_tree(root, source_rel) != current_snapshot:
            raise _error("archive source changed while staging restore")
        if destination.exists() or destination.is_symlink():
            raise _error("restore destination appeared before publish")
        os.replace(stage, destination)
        published = True
        expected = _expected_moved_snapshot(
            plan["source_snapshot"],
            source_rel,
            destination_rel,
            rewritten_files={
                source_rel + "/" + key: value
                for key, value in rewritten.items()
            },
        )
        actual = _snapshot_at(destination, destination_rel)
        if actual != expected:
            raise _error("restored destination failed exact snapshot verification")
        final_record = parse_record(destination / f"{task_id}.md")
        if not isinstance(final_record, Task) or final_record.fields["storage_state"] != "active":
            raise _error("restored task metadata verification failed")
        _validate_bundle_artifacts(root, destination, task_id)
    except Exception:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    cleanup = _remove_published_source(root, source, source_rel, current_snapshot)
    receipt = __import__("clean_slate_model").EventReceipt.create(
        event_type="task.restored",
        entity_kind="task",
        entity_id=task_id,
        actor=args.actor,
        result=str(cleanup["status"]),
        before_sha256=plan["task_sha256"],
        after_sha256=_sha256_file(destination / f"{task_id}.md"),
        metadata={"source": source_rel, "destination": destination_rel, "operation_id": plan["operation_id"], "cleanup": cleanup},
        occurred_at=plan["restore_at"],
    )
    _write_operation_result(
        root,
        str(plan["operation_id"]),
        {"status": cleanup["status"], "operation": "restore", "plan_digest": plan["plan_digest"], "task_id": task_id, "source": source_rel, "destination": destination_rel, "receipt": receipt.to_dict(), "cleanup": cleanup},
    )
    _event(root, receipt)
    return {"status": cleanup["status"], "operation": "restore", "operation_id": plan["operation_id"], "task_id": task_id, "source": source_rel, "destination": destination_rel, "receipt": receipt.to_dict(), "cleanup": cleanup}


def _generate(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    focus = args.focus or []
    return dict(generate_views(root, now=args.now or _today(config), profile=None, focus_ids=focus), operation="views.generate")


def _export_views(args: argparse.Namespace) -> Dict[str, Any]:
    root = _root_path(args.root)
    config = _read_config(root)
    output = Path(args.output).resolve()
    if output == root or root in output.parents:
        raise _error("export output must be outside the canonical workspace")
    records = collect_records(root)
    bundle = build_views(records["tasks"], records["captures"], now=args.now or _today(config), profile=args.profile, area_labels=_area_labels(config))
    receipt = write_views(output, bundle)
    receipt.update(operation="views.export", profile=bundle["profile"], output=str(output), source_sha256=bundle["source_sha256"])
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("root")
    init.add_argument("--workspace-id")
    init.add_argument("--timezone", default="Asia/Shanghai")
    init.add_argument("--default-sensitivity", choices=tuple(SENSITIVITY_RANK), default="internal")
    init.add_argument("--area", action="append", help="KEY=LABEL=ARCHIVE_FOLDER")
    init.add_argument("--force", action="store_true")
    init.add_argument("--yes", action="store_true")

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    create = task_sub.add_parser("create")
    create.add_argument("root")
    create.add_argument("--title", required=True)
    create.add_argument("--outcome")
    create.add_argument("--next-action")
    create.add_argument("--id")
    create.add_argument("--status", choices=("planned", "active"), default="planned")
    create.add_argument("--area", default="general")
    create.add_argument("--type", default="general")
    create.add_argument("--priority", choices=("urgent", "high", "normal", "low"), default="normal")
    create.add_argument("--scheduled-on")
    create.add_argument("--due-on")
    create.add_argument("--sensitivity", choices=tuple(SENSITIVITY_RANK))
    create.add_argument("--agent-access", choices=tuple(AGENT_ACCESS_RANK), default="metadata")
    create.add_argument("--tags", action="append")
    create.add_argument("--aliases", action="append")
    create.add_argument("--body")
    create.add_argument("--actor", default="agent")
    create.add_argument("--yes", action="store_true")

    list_command = task_sub.add_parser("list")
    list_command.add_argument("root")
    list_command.add_argument("--status")
    list_command.add_argument("--area")
    list_command.add_argument("--open-only", action="store_true")

    show = task_sub.add_parser("show")
    show.add_argument("root")
    show.add_argument("--task-id", required=True)
    show.add_argument("--include-body", action="store_true")

    update = task_sub.add_parser("update")
    update.add_argument("root")
    update.add_argument("--task-id", required=True)
    for field in ("title", "outcome", "area", "type", "priority", "sensitivity", "agent-access", "next-action", "waiting-on", "scheduled-on", "due-on", "follow-up-on", "tags", "aliases"):
        update.add_argument("--" + field)
    update.add_argument("--expected-sha")
    update.add_argument("--actor", default="agent")
    update.add_argument("--authorize-access", action="store_true")

    for command_name, target in (("start", "active"), ("wait", "waiting"), ("block", "blocked"), ("complete", "completed"), ("cancel", "cancelled"), ("reopen", "active")):
        transition = task_sub.add_parser(command_name)
        transition.add_argument("root")
        transition.add_argument("--task-id", required=True)
        transition.add_argument("--summary")
        transition.add_argument("--next-action")
        transition.add_argument("--waiting-on")
        transition.add_argument("--follow-up-on")
        transition.add_argument("--expected-sha")
        transition.add_argument("--actor", default="agent")

    capture = sub.add_parser("capture")
    capture_sub = capture.add_subparsers(dest="capture_command", required=True)
    create_capture = capture_sub.add_parser("create")
    create_capture.add_argument("root")
    create_capture.add_argument("--text")
    create_capture.add_argument("--file")
    create_capture.add_argument("--title")
    create_capture.add_argument("--id")
    create_capture.add_argument("--source")
    create_capture.add_argument("--capture-type", choices=("text", "file", "link", "email", "transcript", "other"), default="text")
    create_capture.add_argument("--sensitivity", choices=tuple(SENSITIVITY_RANK))
    create_capture.add_argument("--agent-access", choices=tuple(AGENT_ACCESS_RANK), default="metadata")
    create_capture.add_argument("--tags", action="append")
    create_capture.add_argument("--actor", default="agent")
    create_capture.add_argument("--yes", action="store_true")

    list_capture = capture_sub.add_parser("list")
    list_capture.add_argument("root")
    list_capture.add_argument("--status")
    triage = capture_sub.add_parser("triage")
    triage.add_argument("root")
    triage.add_argument("--capture-id", required=True)
    triage.add_argument("--disposition", choices=("task", "artifact", "library", "defer"), required=True)
    triage.add_argument("--task-id")
    triage.add_argument("--title")
    triage.add_argument("--outcome")
    triage.add_argument("--area")
    triage.add_argument("--type")
    triage.add_argument("--priority", choices=("urgent", "high", "normal", "low"))
    triage.add_argument("--due-on")
    triage.add_argument("--next-action")
    triage.add_argument("--output")
    triage.add_argument("--actor", default="agent")
    triage_apply = capture_sub.add_parser("triage-apply")
    triage_apply.add_argument("root")
    triage_apply.add_argument("--plan", required=True)
    triage_apply.add_argument("--approval", required=True)
    triage_apply.add_argument("--actor", default="agent")

    views = sub.add_parser("views")
    views_sub = views.add_subparsers(dest="views_command", required=True)
    generate = views_sub.add_parser("generate")
    generate.add_argument("root")
    generate.add_argument("--now")
    generate.add_argument("--focus", action="append")
    export = views_sub.add_parser("export")
    export.add_argument("root")
    export.add_argument("--profile", choices=tuple(SENSITIVITY_RANK), required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--now")

    artifact = sub.add_parser("artifact")
    artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    list_artifact = artifact_sub.add_parser("list")
    list_artifact.add_argument("root")
    show_artifact = artifact_sub.add_parser("show")
    show_artifact.add_argument("root")
    show_artifact.add_argument("--artifact-id", required=True)
    show_artifact.add_argument("--include-payload", action="store_true")
    update_artifact = artifact_sub.add_parser("update-access")
    update_artifact.add_argument("root")
    update_artifact.add_argument("--artifact-id", required=True)
    update_artifact.add_argument("--agent-access", choices=tuple(AGENT_ACCESS_RANK), required=True)
    update_artifact.add_argument("--expected-sha")
    update_artifact.add_argument("--authorize-access", action="store_true")
    update_artifact.add_argument("--actor", default="agent")

    archive = sub.add_parser("archive")
    archive_sub = archive.add_subparsers(dest="archive_command", required=True)
    archive_plan = archive_sub.add_parser("plan")
    archive_plan.add_argument("root")
    archive_plan.add_argument("--task-id", required=True)
    archive_plan.add_argument("--output")
    archive_plan.add_argument("--actor", default="agent")
    archive_apply = archive_sub.add_parser("apply")
    archive_apply.add_argument("root")
    archive_apply.add_argument("--plan", required=True)
    archive_apply.add_argument("--approval", required=True)
    archive_apply.add_argument("--actor", default="agent")

    restore = sub.add_parser("restore")
    restore_sub = restore.add_subparsers(dest="restore_command", required=True)
    restore_plan = restore_sub.add_parser("plan")
    restore_plan.add_argument("root")
    restore_plan.add_argument("--task-id", required=True)
    restore_plan.add_argument("--output")
    restore_plan.add_argument("--actor", default="agent")
    restore_apply = restore_sub.add_parser("apply")
    restore_apply.add_argument("root")
    restore_apply.add_argument("--plan", required=True)
    restore_apply.add_argument("--approval", required=True)
    restore_apply.add_argument("--actor", default="agent")

    approve = sub.add_parser("approve")
    approve.add_argument("--plan", required=True)
    approve.add_argument("--output", required=True)
    approve.add_argument("--yes", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = _init_workspace(args)
        elif args.command == "task":
            if args.task_command == "create":
                result = _create_task(args)
            elif args.task_command == "list":
                result = _task_list(args)
            elif args.task_command == "show":
                result = _task_show(args)
            elif args.task_command == "update":
                result = _task_update(args)
            else:
                target = {"start": "active", "wait": "waiting", "block": "blocked", "complete": "completed", "cancel": "cancelled", "reopen": "active"}[args.task_command]
                result = _transition(args, target)
        elif args.command == "capture":
            if args.capture_command == "create":
                result = _capture_create(args)
            elif args.capture_command == "list":
                result = _capture_list(args)
            elif args.capture_command == "triage":
                result = _triage_plan(args)
            else:
                result = _triage_apply(args)
        elif args.command == "artifact":
            if args.artifact_command == "list":
                result = _artifact_list(args)
            elif args.artifact_command == "show":
                result = _artifact_show(args)
            else:
                result = _artifact_update_access(args)
        elif args.command == "views":
            result = _generate(args) if args.views_command == "generate" else _export_views(args)
        elif args.command == "archive":
            if args.archive_command == "plan":
                result = _archive_plan(args)
            else:
                result = _archive_apply(args)
        elif args.command == "restore":
            if args.restore_command == "plan":
                result = _restore_plan(args)
            else:
                result = _restore_apply(args)
        else:
            result = _approve(args)
        _pretty(result)
        return 0
    except (CLIError, ModelError, StateTransitionError, OSError, ValueError) as exc:
        sys.stderr.write("workspace-organizer: " + str(exc) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
