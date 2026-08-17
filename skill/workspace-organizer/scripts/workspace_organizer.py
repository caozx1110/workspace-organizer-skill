#!/usr/bin/env python3
"""Deterministic, fail-closed workspace-organizer operations (schema v1).

The module is intentionally standard-library only and can be invoked directly
from an installed skill package.  Read-only commands write JSON to stdout.
Every structural mutation consumes an immutable plan plus a separate approval
bound to the exact plan bytes.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import quote


SCHEMA_VERSION = 1
MANAGED_DIRECTORIES = (
    "00_总览",
    "10_收件箱",
    "20_任务",
    "30_资料库",
    "90_归档",
    "99_待整理",
    ".workspace-organizer",
)
TASK_ROLES = ("inputs", "work", "deliverables", "records", "history")
OPEN_STATUSES = {"planned", "active", "waiting", "blocked"}
CLOSED_STATUSES = {"completed", "cancelled"}
STATUSES = OPEN_STATUSES | CLOSED_STATUSES | {"archived"}
PRIORITY_ORDER = ("urgent", "high", "normal", "low")
PRIORITY_RANK = {value: index for index, value in enumerate(PRIORITY_ORDER)}
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")
SENSITIVITY_RANK = {value: index for index, value in enumerate(SENSITIVITY_ORDER)}
VISIBLE_SENSITIVITIES = {"public", "internal"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
ARCHIVE_SUFFIXES = {
    ".7z", ".bz2", ".gz", ".rar", ".tar", ".tbz", ".tbz2", ".tgz", ".txz", ".xz", ".zip"
}
TASK_REQUIRED = {
    "schema_version", "id", "title", "status", "area", "type", "priority",
    "due", "sensitivity", "next_action", "updated", "closed_at", "archived_at",
}
TASK_OPTIONAL = {"tags"}
CONFIG_REQUIRED = {
    "schema_version", "workspace_id", "default_sensitivity",
    "adopted_task_paths", "adopted_material_roots", "exclude_paths",
}
GENERATED_PATHS = {
    "todo": (".workspace-organizer/catalog/todo.json", "00_总览/TODO.md"),
    "timeline": (".workspace-organizer/catalog/timeline.json", "00_总览/TIMELINE.md"),
    "materials": (".workspace-organizer/catalog/materials.json", "00_总览/MATERIALS.md"),
}
DEFAULT_SCAN_HASH_LIMIT = 8 * 1024 * 1024
DEFAULT_COMPRESSED_SOURCE_LIMIT = 256 * 1024 * 1024
DEFAULT_COMPRESSED_METADATA_LIMIT = 16 * 1024 * 1024
INITIALIZATION_DIRECTORIES = MANAGED_DIRECTORIES + (".workspace-organizer/verification",)
ADOPTION_FORBIDDEN_ROOTS = MANAGED_DIRECTORIES
MATERIAL_INDEX_FORBIDDEN_ROOTS = ("00_总览", "10_收件箱", "90_归档", "99_待整理", ".workspace-organizer")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_WRITE_NEW_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class WorkspaceError(ValueError):
    """A requested operation cannot be proven safe under the v1 contract."""


def _error(context: str, message: str) -> WorkspaceError:
    return WorkspaceError(f"{context}: {message}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _collision_key(value: str) -> Tuple[str, ...]:
    return tuple(_nfc(part).casefold() for part in value.split("/"))


def _path_contains(root: str, candidate: str) -> bool:
    root_parts = _collision_key(root)
    candidate_parts = _collision_key(candidate)
    return len(root_parts) <= len(candidate_parts) and candidate_parts[: len(root_parts)] == root_parts


def _paths_overlap(left: str, right: str) -> bool:
    return _path_contains(left, right) or _path_contains(right, left)


def validate_relative_path(value: Any, context: str = "path") -> str:
    if not isinstance(value, str) or not value:
        raise _error(context, "must be a non-empty string")
    if value != _nfc(value):
        raise _error(context, "must use Unicode NFC")
    if (
        value.startswith("/") or value.endswith("/") or "//" in value
        or "\\" in value or "\x00" in value
    ):
        raise _error(context, "must be a normalized workspace-relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(value).is_absolute():
        raise _error(context, "must not be absolute or contain empty, . or .. segments")
    return value


def _validate_slug(value: Any, context: str, minimum: int = 1, maximum: int = 48) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or not ID_RE.fullmatch(value):
        raise _error(context, f"must be a {minimum}-{maximum} character lowercase ASCII slug")
    return value


def _parse_timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise _error(context, "must be RFC 3339 with seconds and an explicit offset")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _error(context, "must be an actual RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise _error(context, "must have an explicit offset")
    return parsed


def _parse_date(value: Any, context: str) -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise _error(context, "must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _error(context, "must be an actual Gregorian date") from exc


def _validate_human(value: Any, context: str, maximum: int) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > maximum
        or value != value.strip() or "\r" in value or "\n" in value or value != _nfc(value)
    ):
        raise _error(context, f"must be trimmed NFC single-line text of at most {maximum} characters")
    return value


def _parse_scalar(raw: str, context: str) -> Any:
    if raw == "null":
        return None
    if raw == "1":
        return 1
    if raw.startswith('"') or raw.startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _error(context, f"invalid JSON scalar: {exc.msg}") from exc
    if ID_RE.fullmatch(raw):
        return raw
    raise _error(context, "value must be null, integer 1, a bare slug, or a JSON string/array")


def parse_task_bytes(content: bytes, context: str = "TASK.md") -> Tuple[Dict[str, Any], str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error(context, "must be UTF-8") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise _error(context, "first line must be ---")
    data: Dict[str, Any] = {}
    closing: Optional[int] = None
    for index, raw_line in enumerate(lines[1:], start=1):
        line = raw_line.rstrip("\r\n")
        if line == "---":
            closing = index
            break
        if not line:
            continue
        if line != line.strip():
            raise _error(context, f"line {index + 1} has surrounding whitespace")
        key, separator, raw = line.partition(": ")
        if not separator or not KEY_RE.fullmatch(key):
            raise _error(context, f"line {index + 1} must be one key: value pair")
        if key in data:
            raise _error(context, f"duplicate front matter key {key!r}")
        data[key] = _parse_scalar(raw, f"{context}:{index + 1}")
    if closing is None:
        raise _error(context, "front matter has no closing ---")
    body = "".join(lines[closing + 1 :])
    return data, body


def validate_task(data: Mapping[str, Any], context: str = "TASK.md") -> None:
    missing = TASK_REQUIRED - set(data)
    unknown = set(data) - TASK_REQUIRED - TASK_OPTIONAL
    if missing or unknown:
        raise _error(context, f"schema keys mismatch; missing={sorted(missing)} unknown={sorted(unknown)}")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise _error(context, "schema_version must be integer 1")
    _validate_slug(data["id"], f"{context}.id", 3, 64)
    _validate_human(data["title"], f"{context}.title", 200)
    _validate_slug(data["area"], f"{context}.area")
    _validate_slug(data["type"], f"{context}.type")
    if data["status"] not in STATUSES:
        raise _error(context, "unknown status")
    if data["priority"] not in PRIORITY_ORDER:
        raise _error(context, "unknown priority")
    if data["sensitivity"] not in SENSITIVITY_ORDER:
        raise _error(context, "unknown sensitivity")
    if data["due"] is not None:
        _parse_date(data["due"], f"{context}.due")
    updated = _parse_timestamp(data["updated"], f"{context}.updated")
    closed = _parse_timestamp(data["closed_at"], f"{context}.closed_at") if data["closed_at"] is not None else None
    archived = _parse_timestamp(data["archived_at"], f"{context}.archived_at") if data["archived_at"] is not None else None
    if data["status"] in OPEN_STATUSES:
        _validate_human(data["next_action"], f"{context}.next_action", 500)
        if closed is not None or archived is not None:
            raise _error(context, "open state requires null transition timestamps")
    elif data["status"] in CLOSED_STATUSES:
        if data["next_action"] is not None or closed is None or archived is not None or updated < closed:
            raise _error(context, "closed state requires next_action null, closed_at, and updated >= closed_at")
    else:
        if data["next_action"] is not None or closed is None or archived is None or archived < closed or updated < archived:
            raise _error(context, "archived state has inconsistent timestamps")
    tags = data.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 32 or len(tags) != len(set(tags)):
        raise _error(context, "tags must be at most 32 unique slugs")
    for index, tag in enumerate(tags):
        _validate_slug(tag, f"{context}.tags[{index}]")


def serialize_task(data: Mapping[str, Any], body: str) -> bytes:
    order = (
        "schema_version", "id", "title", "status", "area", "type", "priority",
        "due", "sensitivity", "next_action", "updated", "closed_at", "archived_at", "tags",
    )
    bare = {"status", "area", "type", "priority", "sensitivity"}
    lines = ["---"]
    for key in order:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            rendered = "null"
        elif key == "schema_version":
            rendered = "1"
        elif key in bare:
            rendered = str(value)
        else:
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ": "))
        lines.append(f"{key}: {rendered}")
    lines.extend(["---", ""])
    prefix = "\n".join(lines)
    return (prefix + body.lstrip("\r\n")).encode("utf-8")


def _serialize_task_preserving_body(data: Mapping[str, Any], body: str) -> bytes:
    sentinel = "workspace-organizer-body-sentinel"
    rendered = serialize_task(data, sentinel).decode("utf-8")
    prefix, marker, _ = rendered.partition(sentinel)
    if not marker:
        raise _error("TASK.md", "cannot serialize front matter")
    return prefix.encode("utf-8") + body.encode("utf-8")


def validate_config(data: Mapping[str, Any], context: str = "config.json") -> None:
    missing = CONFIG_REQUIRED - set(data)
    unknown = set(data) - CONFIG_REQUIRED
    if missing or unknown:
        raise _error(context, f"schema keys mismatch; missing={sorted(missing)} unknown={sorted(unknown)}")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise _error(context, "schema_version must be integer 1")
    _validate_slug(data["workspace_id"], f"{context}.workspace_id", 3, 64)
    if data["default_sensitivity"] not in SENSITIVITY_ORDER:
        raise _error(context, "unknown default_sensitivity")
    if not isinstance(data["adopted_task_paths"], list) or not isinstance(data["adopted_material_roots"], list) or not isinstance(data["exclude_paths"], list):
        raise _error(context, "registered paths must be arrays")
    tasks = [validate_relative_path(path, f"{context}.adopted_task_paths") for path in data["adopted_task_paths"]]
    materials: List[str] = []
    for item in data["adopted_material_roots"]:
        if not isinstance(item, dict) or set(item) != {"path", "sensitivity"} or item.get("sensitivity") not in SENSITIVITY_ORDER:
            raise _error(context, "each material root must contain valid path and sensitivity")
        materials.append(validate_relative_path(item["path"], f"{context}.adopted_material_roots"))
    exclusions = [validate_relative_path(path, f"{context}.exclude_paths") for path in data["exclude_paths"]]
    for values, label, allow_nested in (
        (tasks, "adopted tasks", False), (materials, "material roots", True), (exclusions, "exclusions", False)
    ):
        seen: Dict[Tuple[str, ...], str] = {}
        for value in values:
            key = _collision_key(value)
            if key in seen:
                raise _error(context, f"normalized {label} collision between {seen[key]!r} and {value!r}")
            seen[key] = value
        if not allow_nested:
            for index, left in enumerate(values):
                for right in values[index + 1 :]:
                    if _paths_overlap(left, right):
                        raise _error(context, f"overlapping {label}: {left!r} and {right!r}")
    for task in tasks:
        for material in materials:
            if _paths_overlap(task, material):
                raise _error(context, f"task/material overlap: {task!r} and {material!r}")
    for registered in tasks + materials:
        for managed in ADOPTION_FORBIDDEN_ROOTS:
            if _paths_overlap(managed, registered):
                raise _error(
                    context,
                    f"adopted roots must not overlap managed role {managed!r}: {registered!r}",
                )
    for registered in tasks + materials:
        for excluded in exclusions:
            if _path_contains(excluded, registered):
                raise _error(context, f"registered root {registered!r} is inside exclusion {excluded!r}")


def _workspace_root(root: Path, *, must_exist: bool = True) -> Path:
    lexical = root.absolute()
    if lexical.is_symlink():
        raise _error(str(root), "workspace root must not be a symlink")
    if must_exist:
        try:
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise _error(str(root), f"cannot resolve workspace root: {exc}") from exc
        if not resolved.is_dir():
            raise _error(str(root), "workspace root must be a directory")
        return resolved
    return lexical


def _relative(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return validate_relative_path(relative, str(path))


def _has_nested_git(root: Path, directory: Path) -> bool:
    current = directory
    while current != root:
        marker = current / ".git"
        if marker.is_symlink() or marker.exists():
            return True
        if current.parent == current:
            raise _error(str(directory), "escapes workspace")
        current = current.parent
    return False


def _is_config_excluded(relative: str, config: Optional[Mapping[str, Any]]) -> bool:
    return bool(config) and any(_path_contains(path, relative) for path in config["exclude_paths"])


def _safe_existing(root: Path, relative: str, *, config: Optional[Mapping[str, Any]] = None, kind: Optional[str] = None) -> Path:
    relative = validate_relative_path(relative, relative)
    if _path_contains(".workspace-organizer/cache", relative):
        raise _error(relative, "cache paths are excluded")
    if any(part.casefold() == ".git" for part in relative.split("/")):
        raise _error(relative, "VCS paths are excluded")
    if _is_config_excluded(relative, config):
        raise _error(relative, "path is excluded by configuration")
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise _error(relative, f"cannot inspect component: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise _error(relative, "symlink components and targets are not allowed")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _error(relative, "resolved path escapes or is missing") from exc
    check_dir = current if current.is_dir() else current.parent
    if _has_nested_git(root, check_dir):
        raise _error(relative, "path crosses a nested Git repository")
    if kind == "file" and not stat.S_ISREG(current.stat().st_mode):
        raise _error(relative, "must be a regular file")
    if kind == "directory" and not stat.S_ISDIR(current.stat().st_mode):
        raise _error(relative, "must be a directory")
    return current


def _safe_destination(root: Path, relative: str, *, config: Optional[Mapping[str, Any]] = None) -> Path:
    relative = validate_relative_path(relative, relative)
    if _path_contains(".workspace-organizer/cache", relative) or any(part.casefold() == ".git" for part in relative.split("/")) or _is_config_excluded(relative, config):
        raise _error(relative, "destination is excluded")
    current = root
    parts = PurePosixPath(relative).parts
    existing_prefix = True
    for index, part in enumerate(parts):
        is_final = index == len(parts) - 1
        if existing_prefix:
            matches = [entry for entry in current.iterdir() if _nfc(entry.name).casefold() == _nfc(part).casefold()]
            if len(matches) > 1:
                raise _error(relative, f"normalized collision in destination parent {current}")
            if matches:
                match = matches[0]
                if match.name != part or is_final:
                    raise _error(relative, f"destination collides with existing {match.name!r}")
                mode = match.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise _error(relative, "destination parent has a symlink or non-directory component")
                current = match
                if _has_nested_git(root, current):
                    raise _error(relative, "destination crosses a nested Git repository")
                continue
            existing_prefix = False
        current = current / part
    return root.joinpath(*parts)


def load_config(root: Path) -> Dict[str, Any]:
    root = _workspace_root(root)
    config_path = _safe_existing(root, ".workspace-organizer/config.json", kind="file")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(".workspace-organizer/config.json", f"cannot read JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise _error(".workspace-organizer/config.json", "top-level value must be an object")
    validate_config(config, ".workspace-organizer/config.json")
    return config


def _iter_tree(root: Path, start: Path, config: Optional[Mapping[str, Any]], *, include_control: bool = False) -> Iterator[Tuple[str, Path, os.stat_result]]:
    stack = [start]
    while stack:
        directory = stack.pop()
        relative_dir = "" if directory == root else _relative(root, directory)
        if relative_dir and (_is_config_excluded(relative_dir, config) or _path_contains(".workspace-organizer/cache", relative_dir)):
            continue
        if directory != root and _has_nested_git(root, directory):
            continue
        try:
            entries = sorted(directory.iterdir(), key=lambda item: (_nfc(item.name).casefold(), _nfc(item.name)))
        except OSError as exc:
            raise _error(relative_dir or ".", f"cannot list directory: {exc}") from exc
        collision_seen: Dict[str, str] = {}
        for entry in entries:
            if entry.name.casefold() == ".git" or (entry.name == ".workspace-organizer" and not include_control and directory == root):
                continue
            key = _nfc(entry.name).casefold()
            if key in collision_seen:
                raise _error(relative_dir or ".", f"normalized collision between {collision_seen[key]!r} and {entry.name!r}")
            collision_seen[key] = entry.name
            relative = _relative(root, entry)
            if _is_config_excluded(relative, config) or _path_contains(".workspace-organizer/cache", relative):
                continue
            try:
                info = entry.lstat()
            except OSError as exc:
                raise _error(relative, f"cannot inspect: {exc}") from exc
            yield relative, entry, info
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                if entry.name.casefold() != ".git" and not _has_nested_git(root, entry):
                    stack.append(entry)


def _is_compressed(relative: str) -> bool:
    lower = relative.casefold()
    return any(lower.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def inventory_workspace(root: Path, *, hash_limit: int = DEFAULT_SCAN_HASH_LIMIT) -> Dict[str, Any]:
    root = _workspace_root(root)
    config: Optional[Dict[str, Any]] = None
    config_path = root / ".workspace-organizer" / "config.json"
    if config_path.exists() and not config_path.is_symlink():
        config = load_config(root)
    entries: List[Dict[str, Any]] = []
    boundaries: List[Dict[str, str]] = [
        {"path": path, "reason": "configured_exclusion"}
        for path in (config["exclude_paths"] if config else [])
    ]
    for relative, path, info in _iter_tree(root, root, config, include_control=config is None):
        if stat.S_ISLNK(info.st_mode):
            boundaries.append({"path": relative, "reason": "symlink"})
            continue
        if stat.S_ISDIR(info.st_mode):
            if (path / ".git").exists() or (path / ".git").is_symlink():
                boundaries.append({"path": relative, "reason": "nested_git"})
            entries.append({"path": relative, "kind": "directory"})
            continue
        if not stat.S_ISREG(info.st_mode):
            boundaries.append({"path": relative, "reason": "non_regular"})
            continue
        restricted_unknown = _path_contains("10_收件箱", relative) or _path_contains("99_待整理", relative)
        item: Dict[str, Any] = {
            "path": relative,
            "kind": "compressed_original" if _is_compressed(relative) else "file",
            "bytes": info.st_size,
            "sensitivity": "restricted" if restricted_unknown else "unknown",
        }
        if _is_compressed(relative):
            item["sha256"] = None
            item["hash_state"] = "metadata_only"
        elif info.st_size > hash_limit:
            item["sha256"] = None
            item["hash_state"] = "deferred_large_file"
        else:
            item["sha256"] = _sha256_file(path)
            item["hash_state"] = "recorded"
        entries.append(item)
    return {
        "schema_version": 1,
        "operation": "inventory",
        "workspace": ".",
        "entries": sorted(entries, key=lambda item: _collision_key(item["path"])),
        "boundaries": sorted(boundaries, key=lambda item: _collision_key(item["path"])),
    }


def scan_workspace(root: Path, *, hash_limit: int = DEFAULT_SCAN_HASH_LIMIT) -> Dict[str, Any]:
    inventory = inventory_workspace(root, hash_limit=hash_limit)
    proposals: List[Dict[str, Any]] = []
    hashes: Dict[Tuple[int, str], List[str]] = {}
    for item in inventory["entries"]:
        if item["kind"] not in {"file", "compressed_original"}:
            continue
        relative = item["path"]
        if relative.startswith("10_收件箱/") or relative.startswith("99_待整理/"):
            proposals.append({
                "source": relative,
                "classification": "confirmation_required",
                "destination": None,
                "sensitivity": "restricted",
                "reason": "ownership and role are never inferred",
            })
        if item.get("sha256"):
            hashes.setdefault((item["bytes"], item["sha256"]), []).append(relative)
    duplicates = [
        {"bytes": key[0], "sha256": key[1], "paths": sorted(paths, key=_collision_key), "action": "confirmation_required"}
        for key, paths in hashes.items() if len(paths) > 1
    ]
    inventory["operation"] = "scan"
    inventory["proposals"] = sorted(proposals, key=lambda item: _collision_key(item["source"]))
    inventory["duplicate_candidates"] = sorted(duplicates, key=lambda item: (item["sha256"], item["paths"]))
    inventory["snapshot_sha256"] = _sha256_bytes(_canonical_json({
        "entries": inventory["entries"], "boundaries": inventory["boundaries"]
    }))
    return inventory


def _snapshot_file(path: Path) -> Dict[str, Any]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise _error(str(path), "must be a regular file")
    return {"kind": "file", "bytes": info.st_size, "sha256": _sha256_file(path)}


def _snapshot_directory(root: Path, relative: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    directory = _safe_existing(root, relative, config=config, kind="directory")
    files: List[Dict[str, Any]] = []
    directories: List[str] = [""]
    for child_relative, child, info in _iter_tree(root, directory, config, include_control=False):
        local = PurePosixPath(child_relative).relative_to(PurePosixPath(relative)).as_posix()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise _error(child_relative, "bundle contains a symlink or non-regular entry")
        if stat.S_ISDIR(info.st_mode):
            if (child / ".git").exists() or (child / ".git").is_symlink():
                raise _error(child_relative, "bundle contains a nested Git repository")
            directories.append(local)
        else:
            files.append({"path": local, **_snapshot_file(child)})
    files.sort(key=lambda item: _collision_key(item["path"]))
    directories.sort(key=lambda value: _collision_key(value) if value else ())
    digest = _sha256_bytes(_canonical_json({"directories": directories, "files": files}))
    return {"kind": "directory", "tree_sha256": digest, "directories": directories, "files": files}


def _same_snapshot(current: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return _canonical_json(current) == _canonical_json(expected)


def _plan_with_id(plan: MutableMapping[str, Any]) -> Dict[str, Any]:
    core = dict(plan)
    core.pop("plan_id", None)
    plan_id = _sha256_bytes(_canonical_json(core))
    result = {"schema_version": 1, "plan_id": plan_id}
    for key, value in core.items():
        if key != "schema_version":
            result[key] = value
    return result


def _require_keys(value: Mapping[str, Any], expected: Iterable[str], context: str) -> None:
    expected_set = set(expected)
    if set(value) != expected_set:
        raise _error(
            context,
            f"keys mismatch; missing={sorted(expected_set - set(value))} unknown={sorted(set(value) - expected_set)}",
        )


def _validate_file_snapshot(snapshot: Any, context: str) -> None:
    if not isinstance(snapshot, dict):
        raise _error(context, "must be an object")
    _require_keys(snapshot, {"kind", "bytes", "sha256"}, context)
    if snapshot["kind"] != "file" or type(snapshot["bytes"]) is not int or snapshot["bytes"] < 0:
        raise _error(context, "invalid regular-file evidence")
    if not isinstance(snapshot["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot["sha256"]):
        raise _error(context, "invalid file SHA-256")


def _validate_directory_snapshot(snapshot: Any, context: str) -> None:
    if not isinstance(snapshot, dict):
        raise _error(context, "must be an object")
    _require_keys(snapshot, {"kind", "tree_sha256", "directories", "files"}, context)
    if snapshot["kind"] != "directory" or not isinstance(snapshot["tree_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot["tree_sha256"]):
        raise _error(context, "invalid directory evidence")
    if not isinstance(snapshot["directories"], list) or not isinstance(snapshot["files"], list) or snapshot["directories"][:1] != [""]:
        raise _error(context, "invalid directory/file inventory")
    for index, relative in enumerate(snapshot["directories"][1:], start=1):
        validate_relative_path(relative, f"{context}.directories[{index}]")
    seen: set = set()
    for index, item in enumerate(snapshot["files"]):
        if not isinstance(item, dict):
            raise _error(context, "file entries must be objects")
        _require_keys(item, {"path", "kind", "bytes", "sha256"}, f"{context}.files[{index}]")
        validate_relative_path(item["path"], f"{context}.files[{index}].path")
        _validate_file_snapshot({key: item[key] for key in ("kind", "bytes", "sha256")}, f"{context}.files[{index}]")
        key = _collision_key(item["path"])
        if key in seen:
            raise _error(context, "file inventory has a normalized collision")
        seen.add(key)
    expected_digest = _sha256_bytes(_canonical_json({
        "directories": snapshot["directories"], "files": snapshot["files"]
    }))
    if expected_digest != snapshot["tree_sha256"]:
        raise _error(context, "tree_sha256 does not match inventory")


def _validate_plan_shape(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != 1 or plan.get("operation") not in {"initialize", "organize", "archive"}:
        raise _error("plan", "unknown schema version or operation")
    plan_id = plan.get("plan_id")
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise _error("plan", "invalid plan_id")
    core = dict(plan)
    del core["plan_id"]
    if _sha256_bytes(_canonical_json(core)) != plan_id:
        raise _error("plan", "plan_id does not match canonical plan content")
    operation = plan["operation"]
    if operation == "initialize":
        _require_keys(
            plan,
            {"schema_version", "plan_id", "operation", "workspace_id", "root_snapshot_sha256", "config", "operations"},
            "initialize plan",
        )
        _validate_slug(plan["workspace_id"], "plan.workspace_id", 3, 64)
        if not isinstance(plan["root_snapshot_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["root_snapshot_sha256"]):
            raise _error("initialize plan", "invalid root snapshot digest")
        if not isinstance(plan["config"], dict):
            raise _error("initialize plan", "config must be an object")
        validate_config(plan["config"], "plan.config")
        if plan["config"]["workspace_id"] != plan["workspace_id"]:
            raise _error("initialize plan", "workspace identity mismatch")
        if not isinstance(plan["operations"], list) or len(plan["operations"]) != len(INITIALIZATION_DIRECTORIES):
            raise _error("initialize plan", "must cover each managed directory exactly once")
        paths: List[str] = []
        for index, item in enumerate(plan["operations"]):
            if not isinstance(item, dict):
                raise _error("initialize plan", "operations must be objects")
            _require_keys(item, {"action", "path"}, f"plan.operations[{index}]")
            if item["action"] not in {"create_directory", "accept_existing_directory"} or item["path"] not in INITIALIZATION_DIRECTORIES:
                raise _error("initialize plan", "invalid managed-directory operation")
            paths.append(item["path"])
        if set(paths) != set(INITIALIZATION_DIRECTORIES) or len(paths) != len(set(paths)):
            raise _error("initialize plan", "managed-directory operations are incomplete or duplicated")
        return
    if operation == "organize":
        _require_keys(
            plan,
            {"schema_version", "plan_id", "operation", "workspace_id", "config_sha256", "operations"},
            "organize plan",
        )
        _validate_slug(plan["workspace_id"], "plan.workspace_id", 3, 64)
        if not isinstance(plan["config_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["config_sha256"]):
            raise _error("organize plan", "invalid configuration digest")
        if not isinstance(plan["operations"], list) or not plan["operations"]:
            raise _error("organize plan", "at least one operation is required")
        for index, item in enumerate(plan["operations"]):
            if not isinstance(item, dict):
                raise _error("organize plan", "operations must be objects")
            _require_keys(item, {"action", "source", "destination", "source_snapshot"}, f"plan.operations[{index}]")
            if item["action"] != "move_file":
                raise _error("organize plan", "only explicit move_file operations are supported")
            source = validate_relative_path(item["source"], f"plan.operations[{index}].source")
            destination = validate_relative_path(item["destination"], f"plan.operations[{index}].destination")
            if source.startswith(".workspace-organizer/") or source.startswith("00_总览/") or source.startswith("90_归档/") or _paths_overlap(source, destination):
                raise _error("organize plan", "protected or overlapping move path")
            _validate_file_snapshot(item["source_snapshot"], f"plan.operations[{index}].source_snapshot")
        return
    _require_keys(
        plan,
        {
            "schema_version", "plan_id", "operation", "workspace_id", "config_sha256", "task_id",
            "source", "destination", "source_snapshot", "task_record_before_sha256",
            "task_record_before_base64", "task_record_after_sha256", "task_record_after_base64", "adopted_source",
        },
        "archive plan",
    )
    _validate_slug(plan["workspace_id"], "plan.workspace_id", 3, 64)
    _validate_slug(plan["task_id"], "plan.task_id", 3, 64)
    source = validate_relative_path(plan["source"], "plan.source")
    destination = validate_relative_path(plan["destination"], "plan.destination")
    if source.startswith("90_归档/") or not destination.startswith("90_归档/") or _paths_overlap(source, destination):
        raise _error("archive plan", "invalid archive source or destination")
    if type(plan["adopted_source"]) is not bool:
        raise _error("archive plan", "adopted_source must be boolean")
    if not isinstance(plan["config_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["config_sha256"]):
        raise _error("archive plan", "invalid configuration digest")
    _validate_directory_snapshot(plan["source_snapshot"], "plan.source_snapshot")
    decoded: Dict[str, bytes] = {}
    for state in ("before", "after"):
        digest = plan[f"task_record_{state}_sha256"]
        encoded = plan[f"task_record_{state}_base64"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(encoded, str):
            raise _error("archive plan", f"invalid TASK.md {state} evidence")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise _error("archive plan", f"invalid TASK.md {state} base64") from exc
        if _sha256_bytes(content) != digest:
            raise _error("archive plan", f"TASK.md {state} digest mismatch")
        decoded[state] = content
    before, before_body = parse_task_bytes(decoded["before"], "plan TASK.md before")
    after, after_body = parse_task_bytes(decoded["after"], "plan TASK.md after")
    validate_task(before, "plan TASK.md before")
    validate_task(after, "plan TASK.md after")
    expected_destination = f"90_归档/{before['closed_at'][:4]}/{before['area']}/{before['id']}" if before.get("closed_at") else ""
    expected_after = dict(before)
    expected_after.update({
        "status": "archived",
        "updated": after.get("archived_at"),
        "archived_at": after.get("archived_at"),
    })
    if (
        before["id"] != plan["task_id"] or before["status"] not in CLOSED_STATUSES
        or after != expected_after or before_body.encode("utf-8") != after_body.encode("utf-8")
        or destination != expected_destination
    ):
        raise _error("archive plan", "TASK.md transition or canonical destination is invalid")


def write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(_pretty_json(value))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise _error(str(path), "immutable output already exists") from exc


def load_plan(path: Path) -> Tuple[Dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        plan = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(str(path), f"cannot read plan: {exc}") from exc
    if not isinstance(plan, dict):
        raise _error(str(path), "plan must be a JSON object")
    _validate_plan_shape(plan)
    return plan, raw


def approve_plan(plan_path: Path, approval_path: Path, *, confirmed: bool) -> Dict[str, Any]:
    if not confirmed:
        raise _error("approval", "explicit confirmation is required")
    plan, raw = load_plan(plan_path)
    approval = {
        "schema_version": 1,
        "approved": True,
        "plan_id": plan["plan_id"],
        "plan_sha256": _sha256_bytes(raw),
    }
    write_immutable_json(approval_path, approval)
    return approval


def _load_approval(path: Path, plan: Mapping[str, Any], raw_plan: bytes) -> Tuple[Dict[str, Any], bytes]:
    try:
        raw_approval = path.read_bytes()
        approval = json.loads(raw_approval.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(str(path), f"cannot read approval: {exc}") from exc
    expected = {
        "schema_version": 1, "approved": True, "plan_id": plan["plan_id"],
        "plan_sha256": _sha256_bytes(raw_plan),
    }
    if approval != expected:
        raise _error(str(path), "approval does not bind the exact plan bytes")
    return approval, raw_approval


def build_initialization_plan(
    root: Path,
    workspace_id: str,
    *,
    default_sensitivity: str = "internal",
    adopted_task_paths: Sequence[str] = (),
    adopted_material_roots: Sequence[Mapping[str, str]] = (),
    exclude_paths: Sequence[str] = (),
    accepted_existing_managed: Sequence[str] = (),
) -> Dict[str, Any]:
    root = _workspace_root(root)
    config = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "default_sensitivity": default_sensitivity,
        "adopted_task_paths": list(adopted_task_paths),
        "adopted_material_roots": [dict(item) for item in adopted_material_roots],
        "exclude_paths": list(exclude_paths),
    }
    validate_config(config)
    if (root / ".workspace-organizer" / "config.json").exists() or (root / ".workspace-organizer" / "config.json").is_symlink():
        raise _error("initialize", "workspace already has a configuration")
    accepted = set(accepted_existing_managed)
    if not accepted <= set(MANAGED_DIRECTORIES):
        raise _error("initialize", "accepted managed paths contain an unknown role")
    managed_by_key = {_nfc(value).casefold(): value for value in MANAGED_DIRECTORIES}
    for entry in root.iterdir():
        expected = managed_by_key.get(_nfc(entry.name).casefold())
        if expected is not None and entry.name != expected:
            raise _error(entry.name, f"normalized managed-name collision with {expected!r}")
    operations: List[Dict[str, Any]] = []
    for relative in MANAGED_DIRECTORIES:
        path = root / relative
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise _error(relative, "managed-name collision is not a real directory")
            if relative not in accepted:
                raise _error(relative, "existing managed directory requires explicit role acceptance")
            operations.append({"action": "accept_existing_directory", "path": relative})
        else:
            operations.append({"action": "create_directory", "path": relative})
    verification_relative = ".workspace-organizer/verification"
    verification_path = root / ".workspace-organizer" / "verification"
    if verification_path.exists() or verification_path.is_symlink():
        if verification_path.is_symlink() or not verification_path.is_dir():
            raise _error(verification_relative, "control-plane collision is not a real directory")
        operations.append({"action": "accept_existing_directory", "path": verification_relative})
    else:
        operations.append({"action": "create_directory", "path": verification_relative})
    for relative in config["adopted_task_paths"]:
        task_path = _safe_existing(root, f"{relative}/TASK.md", config=config, kind="file")
        task, _ = parse_task_bytes(task_path.read_bytes(), f"{relative}/TASK.md")
        validate_task(task, f"{relative}/TASK.md")
        if task["status"] == "archived":
            raise _error(relative, "cannot adopt an archived task")
    for item in config["adopted_material_roots"]:
        _safe_existing(root, item["path"], config=config)
    for relative in config["exclude_paths"]:
        _safe_existing(root, relative)
    _registered_tasks(root, config, include_archived=True)
    inventory = inventory_workspace(root)
    return _plan_with_id({
        "schema_version": 1,
        "operation": "initialize",
        "workspace_id": workspace_id,
        "root_snapshot_sha256": _sha256_bytes(_canonical_json(inventory)),
        "config": config,
        "operations": operations,
    })


def _registered_tasks(root: Path, config: Mapping[str, Any], *, include_archived: bool = True) -> List[Dict[str, Any]]:
    candidates: List[Tuple[str, str]] = []
    active = root / "20_任务"
    if active.is_symlink():
        raise _error("20_任务", "managed task root must not be a symlink")
    if active.exists():
        for relative, path, info in _iter_tree(root, active, config):
            if stat.S_ISREG(info.st_mode) and path.name == "TASK.md":
                parts = PurePosixPath(relative).parts
                if len(parts) != 3:
                    raise _error(relative, "canonical TASK.md must be one level below 20_任务")
        for child in sorted(active.iterdir(), key=lambda path: (_nfc(path.name).casefold(), _nfc(path.name))):
            if child.is_dir() and not child.is_symlink() and not _has_nested_git(root, child):
                candidate = f"20_任务/{child.name}/TASK.md"
                if not _is_config_excluded(candidate, config) and (child / "TASK.md").exists():
                    candidates.append((candidate, "active"))
    for adopted in config["adopted_task_paths"]:
        candidates.append((f"{adopted}/TASK.md", "adopted"))
    if include_archived:
        archive = root / "90_归档"
        if archive.exists() and not archive.is_symlink():
            for path in archive.glob("*/*/*/TASK.md"):
                if not path.is_symlink():
                    candidates.append((_relative(root, path), "archived"))
    records: List[Dict[str, Any]] = []
    seen_ids: Dict[str, str] = {}
    seen_paths: Dict[Tuple[str, ...], str] = {}
    for relative, location in sorted(candidates, key=lambda item: _collision_key(item[0])):
        key = _collision_key(relative)
        if key in seen_paths:
            raise _error(relative, f"normalized collision with {seen_paths[key]!r}")
        seen_paths[key] = relative
        path = _safe_existing(root, relative, config=config, kind="file")
        data, body = parse_task_bytes(path.read_bytes(), relative)
        validate_task(data, relative)
        if data["id"] in seen_ids:
            raise _error(relative, f"duplicate task ID also at {seen_ids[data['id']]!r}")
        seen_ids[data["id"]] = relative
        bundle = PurePosixPath(relative).parent.as_posix()
        if location == "active":
            if PurePosixPath(bundle).name != data["id"] or data["status"] == "archived":
                raise _error(relative, "canonical active location disagrees with task ID/status")
        elif location == "adopted" and data["status"] == "archived":
            raise _error(relative, "adopted task cannot be archived in place")
        elif location == "archived":
            parts = PurePosixPath(bundle).parts
            if data["status"] != "archived" or parts[-3:] != (data["closed_at"][:4], data["area"], data["id"]):
                raise _error(relative, "archive location disagrees with task metadata")
        records.append({"record": relative, "bundle": bundle, "location": location, "data": data, "body": body})
    for record in records:
        if record["location"] == "archived":
            continue
        for declaration in config["adopted_material_roots"]:
            if _paths_overlap(record["bundle"], declaration["path"]):
                raise _error(
                    ".workspace-organizer/config.json",
                    f"task/material overlap: {record['bundle']!r} and {declaration['path']!r}",
                )
    return records


def _validate_organize_destination(destination: str, tasks: Sequence[Mapping[str, Any]]) -> None:
    if _path_contains("99_待整理", destination) or _path_contains("30_资料库", destination):
        return
    for task in tasks:
        for role in TASK_ROLES + ("pending",):
            if _path_contains(f"{task['bundle']}/{role}", destination):
                return
    raise _error(destination, "destination must be a confirmed task role, task pending, library, or staging path")


def build_organize_plan(root: Path, moves: Sequence[Mapping[str, str]], *, allow_compressed_source: bool = False) -> Dict[str, Any]:
    root = _workspace_root(root)
    config = load_config(root)
    tasks = _registered_tasks(root, config, include_archived=False)
    if not moves:
        raise _error("organize", "at least one explicit move is required")
    operations: List[Dict[str, Any]] = []
    sources: List[str] = []
    destinations: List[str] = []
    for index, move in enumerate(moves):
        if not isinstance(move, Mapping) or set(move) != {"source", "destination"}:
            raise _error(f"moves[{index}]", "must contain only source and destination")
        source = validate_relative_path(move["source"], f"moves[{index}].source")
        destination = validate_relative_path(move["destination"], f"moves[{index}].destination")
        if _paths_overlap(source, destination):
            raise _error(f"moves[{index}]", "source and destination must not overlap")
        if source.startswith(".workspace-organizer/") or source.startswith("00_总览/") or source.startswith("90_归档/"):
            raise _error(source, "protected content cannot be organized")
        if _is_compressed(source) and not allow_compressed_source:
            raise _error(source, "compressed originals require explicit --allow-compressed-source")
        source_path = _safe_existing(root, source, config=config, kind="file")
        _validate_organize_destination(destination, tasks)
        _safe_destination(root, destination, config=config)
        sources.append(source)
        destinations.append(destination)
        operations.append({
            "action": "move_file",
            "source": source,
            "destination": destination,
            "source_snapshot": _snapshot_file(source_path),
        })
    for values, label in ((sources, "sources"), (destinations, "destinations")):
        seen: Dict[Tuple[str, ...], str] = {}
        for value in values:
            key = _collision_key(value)
            if key in seen:
                raise _error("organize", f"normalized {label} collision between {seen[key]!r} and {value!r}")
            seen[key] = value
    for source in sources:
        for destination in destinations:
            if _paths_overlap(source, destination):
                raise _error("organize", f"a destination overlaps planned source {source!r}: {destination!r}")
    component_spellings: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    for destination in destinations:
        spelling = PurePosixPath(destination).parts
        normalized: List[str] = []
        actual: List[str] = []
        for part in spelling:
            normalized.append(_nfc(part).casefold())
            actual.append(part)
            key = tuple(normalized)
            value = tuple(actual)
            if key in component_spellings and component_spellings[key] != value:
                raise _error("organize", f"planned destinations have a normalized component collision at {destination!r}")
            component_spellings[key] = value
    return _plan_with_id({
        "schema_version": 1,
        "operation": "organize",
        "workspace_id": config["workspace_id"],
        "config_sha256": _sha256_bytes(_canonical_json(config)),
        "operations": operations,
    })


def _pending_or_unassigned(root: Path, task: Mapping[str, Any], config: Mapping[str, Any]) -> List[str]:
    bundle = task["bundle"]
    directory = _safe_existing(root, bundle, config=config, kind="directory")
    unresolved: List[str] = []
    for relative, path, info in _iter_tree(root, directory, config):
        local_parts = PurePosixPath(relative).relative_to(PurePosixPath(bundle)).parts
        if not local_parts:
            continue
        if local_parts[0] == "pending":
            if len(local_parts) > 1:
                unresolved.append(relative)
        elif local_parts[0] in TASK_ROLES or local_parts[0] == "TASK.md":
            continue
        else:
            unresolved.append(relative)
    return unresolved


def _has_unverified_reference(root: Path, bundle: str) -> bool:
    control = root / ".workspace-organizer"
    if not control.exists():
        return False
    if control.is_symlink() or not control.is_dir():
        raise _error(".workspace-organizer", "control plane must be a real directory")
    directories = [control]
    verification = control / "verification"
    if verification.is_symlink():
        raise _error(".workspace-organizer/verification", "verification directory must not be a symlink")
    if verification.is_dir():
        directories.insert(0, verification)
    records: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            name = path.name
            match = re.fullmatch(r"([0-9a-f]{64})\.(intent|result)\.json", name)
            legacy = re.fullmatch(r"([0-9a-f]{64})\.json", name)
            if match is None and legacy is None:
                continue
            if path.is_symlink() or not path.is_file():
                raise _error(_relative(root, path), "durable record must be a no-follow regular file")
            record = _load_json_record(path, "durable operation record")
            plan_id = (match or legacy).group(1)
            kind = match.group(2) if match is not None else "legacy"
            records.setdefault(plan_id, {})[kind] = record
    for group in records.values():
        result = group.get("result") or group.get("legacy")
        if result is not None and result.get("status") == "verified":
            continue
        record = result or group.get("intent")
        if record is None:
            continue
        referenced = record.get("referenced_paths", [])
        if any(isinstance(value, str) and _paths_overlap(bundle, value) for value in referenced):
            return True
    return False


def build_archive_plan(root: Path, task_id: str, archived_at: str) -> Dict[str, Any]:
    root = _workspace_root(root)
    config = load_config(root)
    _validate_slug(task_id, "task_id", 3, 64)
    archive_time = _parse_timestamp(archived_at, "archived_at")
    tasks = _registered_tasks(root, config, include_archived=True)
    matches = [task for task in tasks if task["data"]["id"] == task_id]
    if len(matches) != 1:
        raise _error(task_id, "must identify exactly one registered task")
    task = matches[0]
    data = task["data"]
    if task["location"] == "archived" or data["status"] not in CLOSED_STATUSES or data["next_action"] is not None or data["closed_at"] is None:
        raise _error(task_id, "task is not in an eligible closed pre-archive state")
    if archive_time < _parse_timestamp(data["closed_at"], "closed_at") or archive_time < _parse_timestamp(data["updated"], "updated"):
        raise _error(task_id, "archived_at must be at or after closed_at and updated")
    unresolved = _pending_or_unassigned(root, task, config)
    if unresolved:
        raise _error(task_id, f"bundle has pending or unassigned entries: {unresolved}")
    if _has_unverified_reference(root, task["bundle"]):
        raise _error(task_id, "an unverified operation refers to this bundle")
    destination = f"90_归档/{data['closed_at'][:4]}/{data['area']}/{task_id}"
    _safe_destination(root, destination, config=config)
    source_path = _safe_existing(root, task["bundle"], config=config, kind="directory")
    before_bytes = (source_path / "TASK.md").read_bytes()
    after_data = dict(data)
    after_data.update({"status": "archived", "archived_at": archived_at, "updated": archived_at})
    after_bytes = _serialize_task_preserving_body(after_data, task["body"])
    parsed_after, _ = parse_task_bytes(after_bytes, "archived TASK.md")
    validate_task(parsed_after, "archived TASK.md")
    return _plan_with_id({
        "schema_version": 1,
        "operation": "archive",
        "workspace_id": config["workspace_id"],
        "config_sha256": _sha256_bytes(_canonical_json(config)),
        "task_id": task_id,
        "source": task["bundle"],
        "destination": destination,
        "source_snapshot": _snapshot_directory(root, task["bundle"], config),
        "task_record_before_sha256": _sha256_bytes(before_bytes),
        "task_record_before_base64": base64.b64encode(before_bytes).decode("ascii"),
        "task_record_after_sha256": _sha256_bytes(after_bytes),
        "task_record_after_base64": base64.b64encode(after_bytes).decode("ascii"),
        "adopted_source": task["location"] == "adopted",
    })


def _verify_config_binding(root: Path, plan: Mapping[str, Any]) -> Dict[str, Any]:
    config = load_config(root)
    if config["workspace_id"] != plan["workspace_id"] or _sha256_bytes(_canonical_json(config)) != plan["config_sha256"]:
        raise _error("plan", "workspace configuration changed after plan generation")
    return config


def _current_snapshot_for_operation(root: Path, operation: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    source = _safe_existing(root, operation["source"], config=config, kind="file")
    return _snapshot_file(source)


def _verification_path(root: Path, plan_id: str) -> Path:
    directory = _control_subdirectory(root, "verification", create=False)
    return directory / f"{plan_id}.json"


def _control_subdirectory(root: Path, name: str, *, create: bool) -> Path:
    if name not in {"verification", "cache", "catalog", "plans"}:
        raise _error("control plane", "unknown subdirectory role")
    control = _safe_existing(root, ".workspace-organizer", kind="directory")
    matches = [entry for entry in control.iterdir() if _nfc(entry.name).casefold() == name.casefold()]
    if len(matches) > 1 or (matches and matches[0].name != name):
        raise _error(f".workspace-organizer/{name}", "normalized control-plane collision")
    target = matches[0] if matches else control / name
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise _error(f".workspace-organizer/{name}", "must be a real directory")
    if create and not target.exists():
        target.mkdir(exist_ok=False)
    return target


def _load_json_record(path: Path, context: str) -> Dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(str(path), f"cannot read {context}: {exc}") from exc
    if not isinstance(record, dict):
        raise _error(str(path), f"{context} must be a JSON object")
    return record


def _journal_paths(
    root: Path,
    plan: Mapping[str, Any],
    *,
    plan_path: Optional[Path] = None,
) -> List[Path]:
    if plan["operation"] == "initialize":
        return [plan_path.parent.resolve(strict=True)] if plan_path is not None else []
    control = root / ".workspace-organizer"
    if control.is_symlink():
        raise _error(".workspace-organizer", "control plane must not be a symlink")
    if not control.is_dir():
        return []
    paths: List[Path] = []
    verification = control / "verification"
    if verification.is_symlink():
        raise _error(".workspace-organizer/verification", "verification directory must not be a symlink")
    if verification.is_dir():
        paths.append(verification)
    paths.append(control)
    return paths


def _load_existing_verification(
    root: Path,
    plan: Mapping[str, Any],
    *,
    plan_path: Optional[Path] = None,
    raw_plan: Optional[bytes] = None,
) -> Optional[Dict[str, Any]]:
    expected_plan_sha = _sha256_bytes(raw_plan) if raw_plan is not None else None
    for directory in _journal_paths(root, plan, plan_path=plan_path):
        result_path = directory / f"{plan['plan_id']}.result.json"
        intent_path = directory / f"{plan['plan_id']}.intent.json"
        if result_path.exists() or result_path.is_symlink():
            if result_path.is_symlink() or not result_path.is_file():
                raise _error(str(result_path), "result record must be a no-follow regular file")
            record = _load_json_record(result_path, "result record")
            if record.get("plan_id") != plan["plan_id"]:
                raise _error(str(result_path), "result identity mismatch")
            if expected_plan_sha is not None and record.get("plan_sha256") != expected_plan_sha:
                raise _error(str(result_path), "result does not bind the exact plan bytes")
            return record
        if intent_path.exists() or intent_path.is_symlink():
            if intent_path.is_symlink() or not intent_path.is_file():
                raise _error(str(intent_path), "intent record must be a no-follow regular file")
            intent = _load_json_record(intent_path, "intent record")
            if intent.get("plan_id") != plan["plan_id"]:
                raise _error(str(intent_path), "intent identity mismatch")
            if expected_plan_sha is not None and intent.get("plan_sha256") != expected_plan_sha:
                raise _error(str(intent_path), "intent does not bind the exact plan bytes")
            return {
                "schema_version": 1,
                "record_type": "unfinished-intent",
                "plan_id": plan["plan_id"],
                "plan_sha256": intent.get("plan_sha256"),
                "operation": plan["operation"],
                "status": "partial",
                "referenced_paths": intent.get("referenced_paths", []),
                "intent": intent,
            }
    control = root / ".workspace-organizer"
    if control.is_symlink():
        raise _error(".workspace-organizer", "control plane must not be a symlink")
    if not control.exists():
        return None
    path = _verification_path(root, plan["plan_id"])
    if not path.exists():
        return None
    record = _load_json_record(path, "legacy verification")
    if record.get("plan_id") != plan["plan_id"]:
        raise _error(str(path), "verification identity mismatch")
    return record


def _verify_completed(root: Path, plan: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    if record.get("status") != "verified":
        raise _error("verification", "record is not verified")
    if plan["operation"] == "initialize":
        if load_config(root) != plan["config"]:
            raise _error("verification", "initialized configuration no longer matches")
        for operation in plan["operations"]:
            path = root / operation["path"]
            if path.is_symlink() or not path.is_dir():
                raise _error("verification", f"managed directory changed: {operation['path']}")
        return
    config = load_config(root)
    if plan["operation"] == "organize":
        for operation in plan["operations"]:
            source_path = root.joinpath(*PurePosixPath(operation["source"]).parts)
            if source_path.exists() or source_path.is_symlink():
                raise _error("verification", f"move source returned: {operation['source']}")
            destination = _safe_existing(root, operation["destination"], config=config, kind="file")
            if not _same_snapshot(_snapshot_file(destination), operation["source_snapshot"]):
                raise _error("verification", f"move destination changed: {operation['destination']}")
        return
    source_path = root.joinpath(*PurePosixPath(plan["source"]).parts)
    if source_path.exists() or source_path.is_symlink():
        raise _error("verification", "archive source returned")
    destination = _safe_existing(root, plan["destination"], config=config, kind="directory")
    if _sha256_file(destination / "TASK.md") != plan["task_record_after_sha256"]:
        raise _error("verification", "archived TASK.md changed")
    results = record.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise _error("verification", "archive evidence is incomplete")
    expected_snapshot = results[0].get("destination_snapshot")
    if not isinstance(expected_snapshot, dict) or not _same_snapshot(
        _snapshot_directory(root, plan["destination"], config), expected_snapshot
    ):
        raise _error("verification", "archived bundle content changed")


def validate_plan_preconditions(
    root: Path,
    plan: Mapping[str, Any],
    *,
    plan_path: Optional[Path] = None,
    raw_plan: Optional[bytes] = None,
) -> Dict[str, Any]:
    root = _workspace_root(root)
    existing = _load_existing_verification(
        root, plan, plan_path=plan_path, raw_plan=raw_plan
    )
    if existing is not None:
        if existing.get("status") == "verified":
            _verify_completed(root, plan, existing)
            return {"status": "already_applied", "verification": existing}
        raise _error("apply", "an unfinished or failed durable operation record already exists")
    if plan["operation"] == "initialize":
        if (root / ".workspace-organizer" / "config.json").exists():
            raise _error("initialize", "workspace was initialized after plan generation")
        current = inventory_workspace(root)
        if _sha256_bytes(_canonical_json(current)) != plan["root_snapshot_sha256"]:
            raise _error("initialize", "workspace changed after plan generation")
        for operation in plan["operations"]:
            relative = operation["path"]
            exists = (root / relative).exists() or (root / relative).is_symlink()
            if operation["action"] == "create_directory" and exists:
                raise _error(relative, "planned directory now exists")
            if operation["action"] == "accept_existing_directory":
                if not exists or (root / relative).is_symlink() or not (root / relative).is_dir():
                    raise _error(relative, "accepted managed directory changed")
        return {"status": "ready", "operations": plan["operations"]}
    config = _verify_config_binding(root, plan)
    if plan["operation"] == "organize":
        tasks = _registered_tasks(root, config, include_archived=False)
        seen_sources: set = set()
        seen_destinations: set = set()
        for operation in plan["operations"]:
            source_key = _collision_key(operation["source"])
            destination_key = _collision_key(operation["destination"])
            if source_key in seen_sources or destination_key in seen_destinations:
                raise _error("organize", "planned sources and destinations must be unique")
            seen_sources.add(source_key)
            seen_destinations.add(destination_key)
            _validate_organize_destination(operation["destination"], tasks)
            current = _current_snapshot_for_operation(root, operation, config)
            if not _same_snapshot(current, operation["source_snapshot"]):
                raise _error(operation["source"], "source changed after plan generation")
            _safe_destination(root, operation["destination"], config=config)
        return {"status": "ready", "operations": plan["operations"]}
    source = _safe_existing(root, plan["source"], config=config, kind="directory")
    tasks = [task for task in _registered_tasks(root, config, include_archived=True) if task["data"]["id"] == plan["task_id"]]
    if (
        len(tasks) != 1 or tasks[0]["bundle"] != plan["source"]
        or (tasks[0]["location"] == "adopted") != plan["adopted_source"]
    ):
        raise _error(plan["task_id"], "archive plan no longer names the registered stable task path")
    current = _snapshot_directory(root, plan["source"], config)
    if not _same_snapshot(current, plan["source_snapshot"]):
        raise _error(plan["source"], "archive source changed after plan generation")
    if _sha256_bytes((source / "TASK.md").read_bytes()) != plan["task_record_before_sha256"]:
        raise _error(plan["source"], "TASK.md changed after plan generation")
    _safe_destination(root, plan["destination"], config=config)
    return {"status": "ready", "operations": [{"action": "archive_bundle", "source": plan["source"], "destination": plan["destination"]}]}


def dry_run(root: Path, plan_path: Path) -> Dict[str, Any]:
    plan, raw = load_plan(plan_path)
    if plan["operation"] == "initialize":
        intended = plan["operations"]
        excluded = plan["config"]["exclude_paths"]
    elif plan["operation"] == "organize":
        intended = plan["operations"]
        excluded = load_config(_workspace_root(root))["exclude_paths"]
    else:
        after_bytes = base64.b64decode(plan["task_record_after_base64"], validate=True)
        after_task, _ = parse_task_bytes(after_bytes, "plan TASK.md after")
        intended = [
            {
                "action": "update_task_record",
                "path": f"{plan['destination']}/TASK.md",
                "changes": {
                    "status": after_task["status"],
                    "updated": after_task["updated"],
                    "archived_at": after_task["archived_at"],
                },
            },
            {"action": "archive_bundle", "source": plan["source"], "destination": plan["destination"]},
        ]
        excluded = load_config(_workspace_root(root))["exclude_paths"]
    errors: List[str] = []
    collisions: List[str] = []
    try:
        result = validate_plan_preconditions(root, plan, plan_path=plan_path, raw_plan=raw)
        if result["status"] == "already_applied" and result["verification"].get("plan_sha256") != _sha256_bytes(raw):
            raise _error("dry-run", "existing verification binds different plan bytes")
        status = result["status"]
    except WorkspaceError as exc:
        status = "blocked"
        errors.append(str(exc))
        if "collid" in str(exc).casefold() or "destination" in str(exc).casefold() and "exist" in str(exc).casefold():
            collisions.append(str(exc))
    return {
        "schema_version": 1,
        "operation": "dry-run",
        "plan_id": plan["plan_id"],
        "plan_sha256": _sha256_bytes(raw),
        "status": status,
        "mutated": False,
        "intended_mutations": intended,
        "collisions": collisions,
        "excluded_paths": excluded,
        "errors": errors,
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_all(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: List[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _snapshot_file_descriptor(descriptor: int, context: str) -> Dict[str, Any]:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise _error(context, "must remain a regular file")
    return {"kind": "file", "bytes": info.st_size, "sha256": _sha256_bytes(_read_all(descriptor))}


def _normalized_entry_at(directory_fd: int, name: str, context: str) -> Optional[str]:
    matches = [entry for entry in os.listdir(directory_fd) if _nfc(entry).casefold() == _nfc(name).casefold()]
    if len(matches) > 1 or (matches and matches[0] != name):
        raise _error(context, f"normalized collision for component {name!r}")
    return matches[0] if matches else None


def _open_child_directory_at(directory_fd: int, name: str, context: str, *, create: bool = False) -> int:
    existing = _normalized_entry_at(directory_fd, name, context)
    if existing is None:
        if not create:
            raise _error(context, f"missing directory component {name!r}")
        os.mkdir(name, mode=0o700, dir_fd=directory_fd)
        os.fsync(directory_fd)
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise _error(context, f"directory component {name!r} is not a no-follow directory: {exc}") from exc
    try:
        os.stat(".git", dir_fd=child_fd, follow_symlinks=False)
    except FileNotFoundError:
        return child_fd
    except OSError as exc:
        os.close(child_fd)
        raise _error(context, f"cannot inspect nested Git boundary: {exc}") from exc
    os.close(child_fd)
    raise _error(context, "path crosses a nested Git repository")


def _check_apply_path(relative: str, config: Optional[Mapping[str, Any]], *, internal_control: bool = False) -> str:
    relative = validate_relative_path(relative, relative)
    if any(part.casefold() == ".git" for part in relative.split("/")):
        raise _error(relative, "VCS paths are excluded")
    if not internal_control and _path_contains(".workspace-organizer", relative):
        raise _error(relative, "control-plane paths are not user operations")
    if _path_contains(".workspace-organizer/cache", relative) or _is_config_excluded(relative, config):
        raise _error(relative, "path is excluded")
    return relative


def _open_parent_descriptor(
    root: Path,
    relative: str,
    *,
    config: Optional[Mapping[str, Any]],
    create: bool,
    internal_control: bool = False,
) -> Tuple[int, str]:
    relative = _check_apply_path(relative, config, internal_control=internal_control)
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    current = root_fd
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            next_fd = _open_child_directory_at(current, part, relative, create=create)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _open_directory_descriptor(
    root: Path,
    relative: str,
    config: Optional[Mapping[str, Any]],
    *,
    internal_control: bool = False,
) -> Tuple[int, int, str]:
    parent_fd, name = _open_parent_descriptor(
        root, relative, config=config, create=False, internal_control=internal_control
    )
    try:
        directory_fd = _open_child_directory_at(parent_fd, name, relative, create=False)
        return parent_fd, directory_fd, name
    except BaseException:
        os.close(parent_fd)
        raise


def _open_file_descriptor(
    root: Path,
    relative: str,
    config: Optional[Mapping[str, Any]],
    *,
    internal_control: bool = False,
) -> Tuple[int, int, str, os.stat_result]:
    parent_fd, name = _open_parent_descriptor(
        root, relative, config=config, create=False, internal_control=internal_control
    )
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _error(relative, "must be a no-follow regular file")
        return parent_fd, descriptor, name, info
    except BaseException:
        os.close(parent_fd)
        raise


def _open_relative_parent_from(directory_fd: int, relative: str, *, create: bool) -> Tuple[int, str]:
    relative = validate_relative_path(relative, relative)
    current = os.dup(directory_fd)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            next_fd = _open_child_directory_at(current, part, relative, create=create)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _snapshot_tree_descriptor(directory_fd: int, context: str) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    directories: List[str] = [""]

    def walk(current_fd: int, prefix: str) -> None:
        names = sorted(os.listdir(current_fd), key=lambda value: (_nfc(value).casefold(), _nfc(value)))
        seen: Dict[str, str] = {}
        for name in names:
            key = _nfc(name).casefold()
            if key in seen:
                raise _error(context, f"normalized collision between {seen[key]!r} and {name!r}")
            seen[key] = name
            if name.casefold() == ".git":
                raise _error(context, "bundle contains a nested Git repository")
            local = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise _error(context, f"bundle entry {local!r} is a symlink or non-regular object")
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=current_fd)
                try:
                    directories.append(local)
                    walk(child_fd, local)
                finally:
                    os.close(child_fd)
            else:
                file_fd = os.open(name, _READ_FLAGS, dir_fd=current_fd)
                try:
                    files.append({"path": local, **_snapshot_file_descriptor(file_fd, local)})
                finally:
                    os.close(file_fd)

    walk(directory_fd, "")
    files.sort(key=lambda item: _collision_key(item["path"]))
    directories.sort(key=lambda value: _collision_key(value) if value else ())
    digest = _sha256_bytes(_canonical_json({"directories": directories, "files": files}))
    return {"kind": "directory", "tree_sha256": digest, "directories": directories, "files": files}


def _copy_fd_data(source_fd: int, destination_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        _write_all(destination_fd, chunk)
    os.lseek(source_fd, 0, os.SEEK_SET)


def _remove_entry_tree_at(parent_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        for child in os.listdir(directory_fd):
            _remove_entry_tree_at(directory_fd, child)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _stage_file_at(source_fd: int, destination_parent_fd: int, temporary_name: str, expected: Mapping[str, Any]) -> Dict[str, Any]:
    descriptor: Optional[int] = None
    created = False
    try:
        descriptor = os.open(temporary_name, _WRITE_NEW_FLAGS, 0o600, dir_fd=destination_parent_fd)
        created = True
        _copy_fd_data(source_fd, descriptor)
        os.fsync(descriptor)
        snapshot = _snapshot_file_descriptor(descriptor, temporary_name)
        if not _same_snapshot(snapshot, expected):
            raise _error(temporary_name, "temporary copy does not match approved source")
        os.close(descriptor)
        descriptor = None
        os.fsync(destination_parent_fd)
        return snapshot
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(temporary_name, dir_fd=destination_parent_fd)
                os.fsync(destination_parent_fd)
            except FileNotFoundError:
                pass
        raise


def _install_file_noreplace_at(parent_fd: int, temporary_name: str, final_name: str) -> None:
    if _normalized_entry_at(parent_fd, final_name, final_name) is not None:
        raise _error(final_name, "destination already exists")
    os.link(
        temporary_name,
        final_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
        follow_symlinks=False,
    )
    os.fsync(parent_fd)
    os.unlink(temporary_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _rename_noreplace_at(parent_fd: int, temporary_name: str, final_name: str) -> None:
    if _normalized_entry_at(parent_fd, final_name, final_name) is not None:
        raise _error(final_name, "destination already exists")
    library = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(temporary_name)
    destination = os.fsencode(final_name)
    result = -1
    if sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        function = library.renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(parent_fd, source, parent_fd, destination, 0x00000004 | 0x00000010)
    elif hasattr(library, "renameat2"):
        function = library.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(parent_fd, source, parent_fd, destination, 0x00000001)
    else:
        raise _error(final_name, "this POSIX platform lacks atomic no-replace directory installation")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), final_name)
    os.fsync(parent_fd)


def _write_bytes_new_at(parent_fd: int, name: str, payload: bytes, mode: int = 0o600) -> Dict[str, Any]:
    descriptor: Optional[int] = None
    created = False
    try:
        descriptor = os.open(name, _WRITE_NEW_FLAGS, mode, dir_fd=parent_fd)
        created = True
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        snapshot = _snapshot_file_descriptor(descriptor, name)
        os.close(descriptor)
        descriptor = None
        os.fsync(parent_fd)
        return snapshot
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        raise


class _Journal:
    def __init__(self, directory_fd: int, display_directory: Path, plan_id: str, plan_sha256: str):
        self.directory_fd = directory_fd
        self.display_directory = display_directory
        self.plan_id = plan_id
        self.plan_sha256 = plan_sha256
        self.events: List[str] = []

    def close(self) -> None:
        os.close(self.directory_fd)

    def name(self, suffix: str) -> str:
        return f"{self.plan_id}.{suffix}.json"

    def read(self, suffix: str) -> Optional[Dict[str, Any]]:
        name = self.name(suffix)
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=self.directory_fd)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(_read_all(descriptor).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _error(str(self.display_directory / name), f"invalid durable journal record: {exc}") from exc
        finally:
            os.close(descriptor)
        if not isinstance(value, dict) or value.get("plan_id") != self.plan_id or value.get("plan_sha256") != self.plan_sha256:
            raise _error(str(self.display_directory / name), "journal identity mismatch")
        return value

    def write(self, suffix: str, record: Mapping[str, Any]) -> str:
        name = self.name(suffix)
        _write_bytes_new_at(self.directory_fd, name, _pretty_json(record))
        self.events.append(name)
        return name


def _open_journal(root: Path, plan_path: Path, plan: Mapping[str, Any], raw_plan: bytes) -> _Journal:
    if plan["operation"] == "initialize":
        directory = plan_path.parent.resolve(strict=True)
        directory_fd = os.open(directory, _DIRECTORY_FLAGS)
        return _Journal(directory_fd, directory, plan["plan_id"], _sha256_bytes(raw_plan))
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        control_fd = _open_child_directory_at(root_fd, ".workspace-organizer", ".workspace-organizer", create=False)
    finally:
        os.close(root_fd)
    display = root / ".workspace-organizer"
    try:
        existing = _normalized_entry_at(control_fd, "verification", ".workspace-organizer/verification")
        if existing is not None:
            verification_fd = _open_child_directory_at(control_fd, "verification", ".workspace-organizer/verification", create=False)
            os.close(control_fd)
            control_fd = verification_fd
            display = display / "verification"
        return _Journal(control_fd, display, plan["plan_id"], _sha256_bytes(raw_plan))
    except BaseException:
        os.close(control_fd)
        raise


def _write_intent(journal: _Journal, record: Mapping[str, Any]) -> None:
    journal.write("intent", record)


def _write_wal_stage(journal: _Journal, sequence: int, stage: str, evidence: Mapping[str, Any]) -> None:
    journal.write(
        f"wal-{sequence:03d}-{stage}",
        {
            "schema_version": 1,
            "record_type": "wal-stage",
            "plan_id": journal.plan_id,
            "plan_sha256": journal.plan_sha256,
            "sequence": sequence,
            "stage": stage,
            "evidence": dict(evidence),
        },
    )


def _write_result(journal: _Journal, record: Mapping[str, Any]) -> None:
    journal.write("result", record)


def _referenced_paths(plan: Mapping[str, Any]) -> List[str]:
    if plan["operation"] == "initialize":
        return [operation["path"] for operation in plan["operations"]]
    if plan["operation"] == "organize":
        return [path for operation in plan["operations"] for path in (operation["source"], operation["destination"])]
    return [plan["source"], plan["destination"]]


class _WalCursor:
    def __init__(self, journal: _Journal):
        self.journal = journal
        self.sequence = 0

    def write(self, stage: str, evidence: Mapping[str, Any]) -> None:
        self.sequence += 1
        _write_wal_stage(self.journal, self.sequence, stage, evidence)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev, right.st_ino, stat.S_IFMT(right.st_mode)
    )


def _cleanup_temporary_at(parent_fd: int, name: str) -> None:
    try:
        _remove_entry_tree_at(parent_fd, name)
        os.fsync(parent_fd)
    except FileNotFoundError:
        pass


def _open_parent_descriptor_journaled(
    root: Path,
    relative: str,
    *,
    config: Optional[Mapping[str, Any]],
    wal: _WalCursor,
) -> Tuple[int, str]:
    relative = _check_apply_path(relative, config)
    current = os.open(root, _DIRECTORY_FLAGS)
    try:
        parts = PurePosixPath(relative).parts
        prefix: List[str] = []
        for part in parts[:-1]:
            prefix.append(part)
            current_relative = "/".join(prefix)
            existing = _normalized_entry_at(current, part, current_relative)
            if existing is None:
                wal.write("create-destination-parent", {"path": current_relative, "rollback": "remove-if-empty"})
                os.mkdir(part, mode=0o700, dir_fd=current)
                os.fsync(current)
            next_fd = _open_child_directory_at(current, part, current_relative, create=False)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _make_intent(
    plan: Mapping[str, Any],
    raw_plan: bytes,
    approval_sha256: str,
    config_before: Optional[Mapping[str, Any]],
    config_after: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    rollback: Dict[str, Any] = {
        "mode": "manual-verified-recovery",
        "referenced_paths": _referenced_paths(plan),
    }
    if plan["operation"] == "organize":
        rollback["moves"] = [
            {
                "source": item["source"],
                "destination": item["destination"],
                "required_sha256": item["source_snapshot"]["sha256"],
            }
            for item in plan["operations"]
        ]
    elif plan["operation"] == "archive":
        rollback.update({
            "restore_path": plan["source"],
            "archived_path": plan["destination"],
            "source_snapshot": plan["source_snapshot"],
            "task_record_before_base64": plan["task_record_before_base64"],
        })
    return {
        "schema_version": 1,
        "record_type": "immutable-intent",
        "plan_id": plan["plan_id"],
        "plan_sha256": _sha256_bytes(raw_plan),
        "approval_sha256": approval_sha256,
        "operation": plan["operation"],
        "workspace_id": plan["workspace_id"],
        "referenced_paths": _referenced_paths(plan),
        "plan": dict(plan),
        "config_before": dict(config_before) if config_before is not None else None,
        "config_after": dict(config_after) if config_after is not None else None,
        "rollback": rollback,
    }


def _apply_initialize(
    root: Path,
    plan: Mapping[str, Any],
    results: List[Dict[str, Any]],
    wal: _WalCursor,
) -> None:
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        for operation in plan["operations"]:
            relative = operation["path"]
            parts = PurePosixPath(relative).parts
            if len(parts) == 1:
                parent_fd = os.dup(root_fd)
                name = parts[0]
            else:
                parent_fd = os.dup(root_fd)
                try:
                    for part in parts[:-1]:
                        next_fd = _open_child_directory_at(parent_fd, part, relative, create=False)
                        os.close(parent_fd)
                        parent_fd = next_fd
                    name = parts[-1]
                except BaseException:
                    os.close(parent_fd)
                    raise
            try:
                existing = _normalized_entry_at(parent_fd, name, relative)
                if operation["action"] == "create_directory":
                    if existing is not None:
                        raise _error(relative, "planned directory now exists")
                    wal.write("create-managed-directory", {"path": relative, "rollback": "remove-if-empty"})
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                elif existing is None:
                    raise _error(relative, "accepted managed directory disappeared")
                check_fd = _open_child_directory_at(parent_fd, name, relative, create=False)
                os.close(check_fd)
                results.append({**operation, "verified": True})
            finally:
                os.close(parent_fd)

        control_fd = _open_child_directory_at(root_fd, ".workspace-organizer", ".workspace-organizer", create=False)
        temporary_name = f".config.{plan['plan_id']}.tmp"
        temporary_created = False
        config_bytes = _pretty_json(plan["config"])
        try:
            wal.write("stage-initial-config", {
                "path": ".workspace-organizer/config.json",
                "sha256": _sha256_bytes(config_bytes),
                "temporary_name": temporary_name,
            })
            _write_bytes_new_at(control_fd, temporary_name, config_bytes)
            temporary_created = True
            wal.write("install-initial-config", {
                "path": ".workspace-organizer/config.json",
                "sha256": _sha256_bytes(config_bytes),
                "rollback": "remove-only-if-sha256-matches",
            })
            _install_file_noreplace_at(control_fd, temporary_name, "config.json")
            temporary_created = False
        except BaseException:
            if temporary_created:
                _cleanup_temporary_at(control_fd, temporary_name)
            raise
        finally:
            os.close(control_fd)
    finally:
        os.close(root_fd)
    parent_fd, descriptor, _, _ = _open_file_descriptor(
        root, ".workspace-organizer/config.json", None, internal_control=True
    )
    try:
        written = _read_all(descriptor)
        if json.loads(written.decode("utf-8")) != plan["config"]:
            raise _error("initialize", "written configuration failed verification")
    finally:
        os.close(descriptor)
        os.close(parent_fd)
    results.append({
        "action": "write_config",
        "path": ".workspace-organizer/config.json",
        "sha256": _sha256_bytes(written),
        "verified": True,
    })


def _apply_organize(
    root: Path,
    plan: Mapping[str, Any],
    config: Mapping[str, Any],
    results: List[Dict[str, Any]],
    wal: _WalCursor,
) -> None:
    for index, operation in enumerate(plan["operations"]):
        source_parent_fd, source_fd, source_name, source_info = _open_file_descriptor(
            root, operation["source"], config
        )
        destination_parent_fd: Optional[int] = None
        temporary_name = f".workspace-organizer-{plan['plan_id'][:16]}-{index:03d}.tmp"
        temporary_created = False
        try:
            source_snapshot = _snapshot_file_descriptor(source_fd, operation["source"])
            if not _same_snapshot(source_snapshot, operation["source_snapshot"]):
                raise _error(operation["source"], "source changed after plan approval")
            destination_parent_fd, destination_name = _open_parent_descriptor_journaled(
                root, operation["destination"], config=config, wal=wal
            )
            if _normalized_entry_at(destination_parent_fd, destination_name, operation["destination"]) is not None:
                raise _error(operation["destination"], "destination already exists")
            wal.write("stage-file-copy", {
                "source": operation["source"],
                "destination": operation["destination"],
                "temporary_name": temporary_name,
                "source_snapshot": source_snapshot,
            })
            temporary_snapshot = _stage_file_at(
                source_fd, destination_parent_fd, temporary_name, operation["source_snapshot"]
            )
            temporary_created = True
            wal.write("install-file", {
                "source": operation["source"],
                "destination": operation["destination"],
                "temporary_snapshot": temporary_snapshot,
                "rollback": "retain-source-until-fresh-destination-verification",
            })
            _install_file_noreplace_at(destination_parent_fd, temporary_name, destination_name)
            temporary_created = False

            fresh_parent_fd, fresh_fd, _, fresh_info = _open_file_descriptor(
                root, operation["destination"], config
            )
            try:
                destination_snapshot = _snapshot_file_descriptor(fresh_fd, operation["destination"])
                installed_info = os.stat(destination_name, dir_fd=destination_parent_fd, follow_symlinks=False)
                if not _same_identity(installed_info, fresh_info) or not _same_snapshot(
                    destination_snapshot, operation["source_snapshot"]
                ):
                    raise _error(operation["destination"], "fresh destination verification failed")
            finally:
                os.close(fresh_fd)
                os.close(fresh_parent_fd)

            current_source = _snapshot_file_descriptor(source_fd, operation["source"])
            path_source_info = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
            if not _same_identity(path_source_info, source_info) or not _same_snapshot(
                current_source, operation["source_snapshot"]
            ):
                raise _error(operation["source"], "source identity changed before removal")
            wal.write("remove-source-file", {
                "source": operation["source"],
                "destination": operation["destination"],
                "source_snapshot": current_source,
                "rollback": {
                    "copy_from": operation["destination"],
                    "restore_to": operation["source"],
                    "required_sha256": current_source["sha256"],
                },
            })
            delete_parent_fd, delete_destination_fd, _, delete_destination_info = _open_file_descriptor(
                root, operation["destination"], config
            )
            try:
                if not _same_identity(installed_info, delete_destination_info) or not _same_snapshot(
                    _snapshot_file_descriptor(delete_destination_fd, operation["destination"]),
                    operation["source_snapshot"],
                ):
                    raise _error(operation["destination"], "destination binding changed before source removal")
            finally:
                os.close(delete_destination_fd)
                os.close(delete_parent_fd)
            delete_source_info = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
            if not _same_identity(delete_source_info, source_info) or not _same_snapshot(
                _snapshot_file_descriptor(source_fd, operation["source"]),
                operation["source_snapshot"],
            ):
                raise _error(operation["source"], "source binding changed at removal boundary")
            os.unlink(source_name, dir_fd=source_parent_fd)
            os.fsync(source_parent_fd)
            try:
                os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _error(operation["source"], "source removal did not persist")
            final_parent_fd, final_fd, _, final_info = _open_file_descriptor(
                root, operation["destination"], config
            )
            try:
                if not _same_identity(installed_info, final_info) or not _same_snapshot(
                    _snapshot_file_descriptor(final_fd, operation["destination"]),
                    operation["source_snapshot"],
                ):
                    raise _error(operation["destination"], "destination changed after source removal")
            finally:
                os.close(final_fd)
                os.close(final_parent_fd)
            results.append({
                **operation,
                "post_apply": {
                    "source_absent": True,
                    "destination_snapshot": destination_snapshot,
                    "rollback": {
                        "copy_from": operation["destination"],
                        "restore_to": operation["source"],
                        "required_sha256": destination_snapshot["sha256"],
                    },
                    "verified": True,
                },
            })
        finally:
            if destination_parent_fd is not None:
                if temporary_created:
                    _cleanup_temporary_at(destination_parent_fd, temporary_name)
                os.close(destination_parent_fd)
            os.close(source_fd)
            os.close(source_parent_fd)


def _expected_archive_snapshot(plan: Mapping[str, Any]) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    after_bytes = base64.b64decode(plan["task_record_after_base64"], validate=True)
    for item in plan["source_snapshot"]["files"]:
        if item["path"] == "TASK.md":
            files.append({
                "path": "TASK.md",
                "kind": "file",
                "bytes": len(after_bytes),
                "sha256": plan["task_record_after_sha256"],
            })
        else:
            files.append(dict(item))
    directories = list(plan["source_snapshot"]["directories"])
    tree_sha256 = _sha256_bytes(_canonical_json({"directories": directories, "files": files}))
    return {"kind": "directory", "tree_sha256": tree_sha256, "directories": directories, "files": files}


def _copy_archive_tree_to_temporary(
    source_fd: int,
    temporary_fd: int,
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    expected = _expected_archive_snapshot(plan)
    directories = [value for value in expected["directories"] if value]
    directories.sort(key=lambda value: (len(PurePosixPath(value).parts), _collision_key(value)))
    for relative in directories:
        parent_fd, name = _open_relative_parent_from(temporary_fd, relative, create=False)
        try:
            if _normalized_entry_at(parent_fd, name, relative) is not None:
                raise _error(relative, "temporary archive directory collision")
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    source_files = {item["path"]: item for item in plan["source_snapshot"]["files"]}
    expected_files = {item["path"]: item for item in expected["files"]}
    after_bytes = base64.b64decode(plan["task_record_after_base64"], validate=True)
    for relative in sorted(source_files, key=_collision_key):
        source_parent_fd, source_name = _open_relative_parent_from(source_fd, relative, create=False)
        destination_parent_fd, destination_name = _open_relative_parent_from(temporary_fd, relative, create=False)
        source_file_fd: Optional[int] = None
        try:
            source_file_fd = os.open(source_name, _READ_FLAGS, dir_fd=source_parent_fd)
            if not _same_snapshot(
                _snapshot_file_descriptor(source_file_fd, relative),
                {key: source_files[relative][key] for key in ("kind", "bytes", "sha256")},
            ):
                raise _error(relative, "archive source file changed during copy")
            if relative == "TASK.md":
                _write_bytes_new_at(destination_parent_fd, destination_name, after_bytes)
            else:
                _stage_file_at(
                    source_file_fd,
                    destination_parent_fd,
                    destination_name,
                    {key: expected_files[relative][key] for key in ("kind", "bytes", "sha256")},
                )
        finally:
            if source_file_fd is not None:
                os.close(source_file_fd)
            os.close(source_parent_fd)
            os.close(destination_parent_fd)
    actual = _snapshot_tree_descriptor(temporary_fd, "temporary archive tree")
    if not _same_snapshot(actual, expected):
        raise _error(plan["destination"], "temporary archive tree failed exact verification")
    return actual


def _remove_approved_tree_at(
    source_parent_fd: int,
    source_name: str,
    source_fd: int,
    expected: Mapping[str, Any],
) -> None:
    files = {item["path"]: item for item in expected["files"]}
    for relative in sorted(files, key=_collision_key):
        parent_fd, name = _open_relative_parent_from(source_fd, relative, create=False)
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
            descriptor_info = os.fstat(descriptor)
            path_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_identity(descriptor_info, path_info) or not _same_snapshot(
                _snapshot_file_descriptor(descriptor, relative),
                {key: files[relative][key] for key in ("kind", "bytes", "sha256")},
            ):
                raise _error(relative, "archive source changed before verified removal")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)
    directories = [value for value in expected["directories"] if value]
    directories.sort(key=lambda value: (-len(PurePosixPath(value).parts), _collision_key(value)))
    for relative in directories:
        parent_fd, name = _open_relative_parent_from(source_fd, relative, create=False)
        directory_fd: Optional[int] = None
        try:
            directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            if not _same_identity(
                os.fstat(directory_fd), os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            ):
                raise _error(relative, "archive source directory identity changed")
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            os.close(parent_fd)
    if not _same_identity(
        os.fstat(source_fd), os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
    ):
        raise _error(source_name, "archive source root identity changed")
    os.rmdir(source_name, dir_fd=source_parent_fd)
    os.fsync(source_parent_fd)


def _replace_adopted_config(
    root: Path,
    plan: Mapping[str, Any],
    config_before: Mapping[str, Any],
    config_after: Mapping[str, Any],
    wal: _WalCursor,
) -> None:
    parent_fd, descriptor, name, original_info = _open_file_descriptor(
        root, ".workspace-organizer/config.json", None, internal_control=True
    )
    temporary_name = f".config.{plan['plan_id']}.tmp"
    temporary_created = False
    try:
        try:
            loaded = json.loads(_read_all(descriptor).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _error("archive", f"configuration became unreadable: {exc}") from exc
        validate_config(loaded)
        if loaded != config_before or _sha256_bytes(_canonical_json(loaded)) != plan["config_sha256"]:
            raise _error("archive", "configuration changed before adopted-task update")
        payload = _pretty_json(config_after)
        wal.write("stage-adopted-config", {
            "path": ".workspace-organizer/config.json",
            "before": config_before,
            "after": config_after,
            "temporary_name": temporary_name,
            "after_sha256": _sha256_bytes(payload),
        })
        _write_bytes_new_at(parent_fd, temporary_name, payload)
        temporary_created = True
        current_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(current_info, original_info):
            raise _error("archive", "configuration identity changed before replacement")
        wal.write("install-adopted-config", {
            "path": ".workspace-organizer/config.json",
            "before": config_before,
            "after": config_after,
            "rollback": "restore exact config_before before re-registering source",
        })
        os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_created = False
        os.fsync(parent_fd)
    except BaseException:
        if temporary_created:
            _cleanup_temporary_at(parent_fd, temporary_name)
        raise
    finally:
        os.close(descriptor)
        os.close(parent_fd)
    fresh_parent_fd, fresh_fd, _, _ = _open_file_descriptor(
        root, ".workspace-organizer/config.json", None, internal_control=True
    )
    try:
        fresh = json.loads(_read_all(fresh_fd).decode("utf-8"))
        if fresh != config_after:
            raise _error("archive", "adopted-task configuration update failed verification")
    finally:
        os.close(fresh_fd)
        os.close(fresh_parent_fd)
    wal.write("adopted-config-verified", {
        "path": ".workspace-organizer/config.json",
        "after": config_after,
    })


def _apply_archive(
    root: Path,
    plan: Mapping[str, Any],
    config: Dict[str, Any],
    results: List[Dict[str, Any]],
    wal: _WalCursor,
) -> None:
    source_parent_fd, source_fd, source_name = _open_directory_descriptor(root, plan["source"], config)
    destination_parent_fd: Optional[int] = None
    temporary_fd: Optional[int] = None
    temporary_name = f".workspace-organizer-{plan['plan_id'][:16]}.tmp"
    installed = False
    temporary_created = False
    try:
        source_snapshot = _snapshot_tree_descriptor(source_fd, plan["source"])
        if not _same_snapshot(source_snapshot, plan["source_snapshot"]):
            raise _error(plan["source"], "archive source changed after plan approval")
        destination_parent_fd, destination_name = _open_parent_descriptor_journaled(
            root, plan["destination"], config=config, wal=wal
        )
        if _normalized_entry_at(destination_parent_fd, destination_name, plan["destination"]) is not None:
            raise _error(plan["destination"], "archive destination already exists")
        wal.write("stage-archive-tree", {
            "source": plan["source"],
            "destination": plan["destination"],
            "temporary_name": temporary_name,
            "source_snapshot": source_snapshot,
            "expected_destination_snapshot": _expected_archive_snapshot(plan),
        })
        if _normalized_entry_at(destination_parent_fd, temporary_name, plan["destination"]) is not None:
            raise _error(plan["destination"], "controlled archive temporary target already exists")
        os.mkdir(temporary_name, mode=0o700, dir_fd=destination_parent_fd)
        temporary_created = True
        os.fsync(destination_parent_fd)
        temporary_fd = os.open(temporary_name, _DIRECTORY_FLAGS, dir_fd=destination_parent_fd)
        destination_snapshot = _copy_archive_tree_to_temporary(source_fd, temporary_fd, plan)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        wal.write("install-archive-tree", {
            "source": plan["source"],
            "destination": plan["destination"],
            "destination_snapshot": destination_snapshot,
            "rollback": "retain-source-until-fresh-destination-verification",
        })
        _rename_noreplace_at(destination_parent_fd, temporary_name, destination_name)
        installed = True
        temporary_created = False

        fresh_parent_fd, fresh_fd, _ = _open_directory_descriptor(root, plan["destination"], config)
        try:
            fresh_snapshot = _snapshot_tree_descriptor(fresh_fd, plan["destination"])
            installed_info = os.stat(destination_name, dir_fd=destination_parent_fd, follow_symlinks=False)
            if not _same_identity(installed_info, os.fstat(fresh_fd)) or not _same_snapshot(
                fresh_snapshot, destination_snapshot
            ):
                raise _error(plan["destination"], "fresh archive destination verification failed")
        finally:
            os.close(fresh_fd)
            os.close(fresh_parent_fd)

        after_config = dict(config)
        if plan["adopted_source"]:
            after_config["adopted_task_paths"] = [
                path for path in config["adopted_task_paths"] if path != plan["source"]
            ]
            validate_config(after_config)
            _replace_adopted_config(root, plan, config, after_config, wal)

        if not _same_snapshot(
            _snapshot_tree_descriptor(source_fd, plan["source"]), plan["source_snapshot"]
        ):
            raise _error(plan["source"], "archive source changed before removal")
        wal.write("remove-archive-source", {
            "source": plan["source"],
            "destination": plan["destination"],
            "source_snapshot": plan["source_snapshot"],
            "destination_snapshot": destination_snapshot,
            "config_before": config if plan["adopted_source"] else None,
            "config_after": after_config if plan["adopted_source"] else None,
            "rollback": {
                "restore_from": plan["destination"],
                "restore_to": plan["source"],
                "task_record_before_base64": plan["task_record_before_base64"],
            },
        })
        delete_destination_parent_fd, delete_destination_fd, _ = _open_directory_descriptor(
            root, plan["destination"], after_config
        )
        try:
            if not _same_identity(installed_info, os.fstat(delete_destination_fd)) or not _same_snapshot(
                _snapshot_tree_descriptor(delete_destination_fd, plan["destination"]),
                destination_snapshot,
            ):
                raise _error(plan["destination"], "archive destination binding changed before source removal")
        finally:
            os.close(delete_destination_fd)
            os.close(delete_destination_parent_fd)
        if not _same_identity(
            os.fstat(source_fd),
            os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False),
        ) or not _same_snapshot(
            _snapshot_tree_descriptor(source_fd, plan["source"]), plan["source_snapshot"]
        ):
            raise _error(plan["source"], "archive source binding changed at removal boundary")
        _remove_approved_tree_at(
            source_parent_fd, source_name, source_fd, plan["source_snapshot"]
        )
        wal.write("archive-source-removed", {
            "source": plan["source"],
            "destination": plan["destination"],
            "destination_tree_sha256": destination_snapshot["tree_sha256"],
        })
        try:
            os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _error(plan["source"], "archive source removal did not persist")
        final_parent_fd, final_fd, _ = _open_directory_descriptor(root, plan["destination"], after_config)
        try:
            if not _same_snapshot(
                _snapshot_tree_descriptor(final_fd, plan["destination"]), destination_snapshot
            ):
                raise _error("archive", "post-archive destination verification failed")
        finally:
            os.close(final_fd)
            os.close(final_parent_fd)
        results.append({
            "action": "archive_bundle",
            "source": plan["source"],
            "destination": plan["destination"],
            "source_snapshot": plan["source_snapshot"],
            "destination_snapshot": destination_snapshot,
            "task_record_before_sha256": plan["task_record_before_sha256"],
            "task_record_after_sha256": plan["task_record_after_sha256"],
            "config_before": config if plan["adopted_source"] else None,
            "config_after": after_config if plan["adopted_source"] else None,
            "rollback": {
                "restore_path": plan["source"],
                "archived_path": plan["destination"],
                "task_record_before_base64": plan["task_record_before_base64"],
                "required_destination_tree_sha256": destination_snapshot["tree_sha256"],
                "mode": "verified_exact_archive_rollback_only",
            },
            "verified": True,
        })
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if destination_parent_fd is not None:
            if temporary_created and not installed:
                _cleanup_temporary_at(destination_parent_fd, temporary_name)
            os.close(destination_parent_fd)
        os.close(source_fd)
        os.close(source_parent_fd)


def apply_plan(root: Path, plan_path: Path, approval_path: Path) -> Dict[str, Any]:
    root = _workspace_root(root)
    plan, raw_plan = load_plan(plan_path)
    _, raw_approval = _load_approval(approval_path, plan, raw_plan)
    approval_sha256 = _sha256_bytes(raw_approval)
    preflight = validate_plan_preconditions(
        root, plan, plan_path=plan_path, raw_plan=raw_plan
    )
    if preflight["status"] == "already_applied":
        if preflight["verification"].get("plan_sha256") != _sha256_bytes(raw_plan):
            raise _error("apply", "existing verification binds different plan bytes")
        return preflight["verification"]

    config_before: Optional[Dict[str, Any]] = None
    config_after: Optional[Dict[str, Any]] = None
    if plan["operation"] == "initialize":
        config_after = dict(plan["config"])
    else:
        config_before = _verify_config_binding(root, plan)
        config_after = dict(config_before)
        if plan["operation"] == "archive" and plan["adopted_source"]:
            config_after["adopted_task_paths"] = [
                path for path in config_before["adopted_task_paths"] if path != plan["source"]
            ]
            validate_config(config_after)

    journal = _open_journal(root, plan_path, plan, raw_plan)
    try:
        if journal.read("result") is not None or journal.read("intent") is not None:
            raise _error("apply", "durable operation evidence already exists; blind retry is blocked")
        intent = _make_intent(
            plan, raw_plan, approval_sha256, config_before, config_after
        )
        _write_intent(journal, intent)
        wal = _WalCursor(journal)
        record: Dict[str, Any] = {
            "schema_version": 1,
            "record_type": "verification-result",
            "plan_id": plan["plan_id"],
            "plan_sha256": _sha256_bytes(raw_plan),
            "approval_sha256": approval_sha256,
            "operation": plan["operation"],
            "status": "failed",
            "referenced_paths": _referenced_paths(plan),
            "journal_events": journal.events,
            "results": [],
        }
        results: List[Dict[str, Any]] = record["results"]
        try:
            if plan["operation"] == "initialize":
                _apply_initialize(root, plan, results, wal)
            elif plan["operation"] == "organize":
                assert config_before is not None
                _apply_organize(root, plan, config_before, results, wal)
            else:
                assert config_before is not None
                _apply_archive(root, plan, config_before, results, wal)
            if not all(
                result.get("verified") or result.get("post_apply", {}).get("verified")
                for result in results
            ):
                raise _error("apply", "one or more operations did not verify")
            record.update({
                "status": "verified",
                "journal_events": list(journal.events),
                "results": results,
            })
            _write_result(journal, record)
            return record
        except BaseException as exc:
            record.update({
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "journal_events": list(journal.events),
                "results": results,
            })
            try:
                _write_result(journal, record)
            except BaseException:
                pass
            raise
    finally:
        journal.close()


def verify_plan(root: Path, plan_path: Path) -> Dict[str, Any]:
    root = _workspace_root(root)
    plan, raw = load_plan(plan_path)
    record = _load_existing_verification(
        root, plan, plan_path=plan_path, raw_plan=raw
    )
    if record is None:
        raise _error("verify", "no durable result record exists for this plan")
    if record.get("status") != "verified":
        raise _error("verify", "operation is unfinished or failed")
    if record.get("plan_sha256") != _sha256_bytes(raw):
        raise _error("verify", "verification does not match exact plan bytes")
    _verify_completed(root, plan, record)
    return {
        "schema_version": 1,
        "operation": "verify",
        "plan_id": plan["plan_id"],
        "status": "verified",
        "verification_sha256": _sha256_bytes(_pretty_json(record)),
    }


def _effective_material_sensitivity(relative: str, config: Mapping[str, Any]) -> str:
    if any(_path_contains(root, relative) for root in ("10_收件箱", "99_待整理")):
        return "restricted"
    values = [config["default_sensitivity"]]
    for item in config["adopted_material_roots"]:
        if _path_contains(item["path"], relative):
            values.append(item["sensitivity"])
    return max(values, key=lambda value: SENSITIVITY_RANK[value])


def _catalog(view: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "view": view,
        "profile": "default",
        "source_sha256": _sha256_bytes(_canonical_json(items)),
        "items": items,
    }


def _escape_table(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _link(relative: str) -> str:
    return "../" + quote(relative, safe="/-._~")


def _render_markdown(view: str, catalog: Mapping[str, Any]) -> bytes:
    marker = f"<!-- workspace-organizer:generated view={view} schema=1 source_sha256={catalog['source_sha256']} -->"
    if view == "todo":
        lines = [marker, "# TODO", "", "| Priority | Due | Status | Task | Next action |", "| --- | --- | --- | --- | --- |"]
        for item in catalog["items"]:
            lines.append(
                f"| {item['priority']} | {item['due'] or '—'} | {item['status']} | "
                f"[{item['id']}]({_link(item['record'])}) — {_escape_table(item['title'])} | {_escape_table(item['next_action'])} |"
            )
    elif view == "timeline":
        lines = [marker, "# Timeline", "", "| Date | Event | Priority | Status | Task |", "| --- | --- | --- | --- | --- |"]
        for item in catalog["items"]:
            lines.append(
                f"| {item['date']} | {item['event']} | {item['priority']} | {item['status']} | "
                f"[{item['id']}]({_link(item['record'])}) — {_escape_table(item['title'])} |"
            )
    else:
        lines = [marker, "# Materials", "", "| Role | Task | Material | Bytes | SHA-256 |", "| --- | --- | --- | --- | --- |"]
        for item in catalog["items"]:
            task = item["task_id"] or "—"
            lines.append(f"| {item['role']} | {task} | [{_escape_table(item['path'])}]({_link(item['path'])}) | {item['bytes']} | {item['sha256']} |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _material_item(root: Path, relative: str, role: str, task_id: Optional[str], sensitivity: str, config: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if any(_path_contains(forbidden, relative) for forbidden in MATERIAL_INDEX_FORBIDDEN_ROOTS):
        return None
    if sensitivity not in VISIBLE_SENSITIVITIES:
        return None
    path = _safe_existing(root, relative, config=config, kind="file")
    info = path.stat()
    return {"path": relative, "role": role, "task_id": task_id, "sensitivity": sensitivity, "bytes": info.st_size, "sha256": _sha256_file(path)}


def build_generated_outputs(root: Path) -> Dict[str, bytes]:
    root = _workspace_root(root)
    config = load_config(root)
    tasks = _registered_tasks(root, config, include_archived=True)
    todo: List[Dict[str, Any]] = []
    materials: List[Dict[str, Any]] = []
    for task in tasks:
        if task["location"] == "archived":
            continue
        data = task["data"]
        if data["sensitivity"] in VISIBLE_SENSITIVITIES and data["status"] in OPEN_STATUSES:
            todo.append({
                "id": data["id"], "title": data["title"], "status": data["status"],
                "priority": data["priority"], "due": data["due"], "sensitivity": data["sensitivity"],
                "next_action": data["next_action"], "record": task["record"],
            })
        bundle_path = _safe_existing(root, task["bundle"], config=config, kind="directory")
        for role in TASK_ROLES:
            role_path = bundle_path / role
            if not role_path.exists() or role_path.is_symlink() or not role_path.is_dir():
                continue
            for relative, path, info in _iter_tree(root, role_path, config):
                if stat.S_ISREG(info.st_mode):
                    item = _material_item(root, relative, role, data["id"], data["sensitivity"], config)
                    if item:
                        materials.append(item)
    library_roots = ["30_资料库"] + [item["path"] for item in config["adopted_material_roots"]]
    seen_material_paths = {item["path"] for item in materials}
    for material_root in library_roots:
        if any(_paths_overlap(forbidden, material_root) for forbidden in MATERIAL_INDEX_FORBIDDEN_ROOTS):
            raise _error(material_root, "material inventory must not enter a fixed or control-plane role")
        path = root.joinpath(*PurePosixPath(material_root).parts)
        if not path.exists():
            if material_root != "30_资料库":
                raise _error(material_root, "registered material root is missing")
            continue
        root_path = _safe_existing(root, material_root, config=config)
        candidates: List[Tuple[str, Path, os.stat_result]]
        if root_path.is_file():
            candidates = [(material_root, root_path, root_path.stat())]
        elif root_path.is_dir():
            candidates = list(_iter_tree(root, root_path, config))
        else:
            raise _error(material_root, "material root must be a regular file or directory")
        for relative, child, info in candidates:
            if relative in seen_material_paths or not stat.S_ISREG(info.st_mode):
                continue
            sensitivity = _effective_material_sensitivity(relative, config)
            item = _material_item(root, relative, "library", None, sensitivity, config)
            if item:
                materials.append(item)
                seen_material_paths.add(relative)
    todo.sort(key=lambda item: (
        PRIORITY_RANK[item["priority"]], item["due"] is None, item["due"] or "", item["id"]
    ))
    timeline = [
        {"date": item["due"], "event": "due", "id": item["id"], "title": item["title"],
         "status": item["status"], "priority": item["priority"], "sensitivity": item["sensitivity"], "record": item["record"]}
        for item in todo if item["due"] is not None
    ]
    timeline.sort(key=lambda item: (item["date"], PRIORITY_RANK[item["priority"]], item["id"]))
    materials.sort(key=lambda item: _nfc(item["path"]))
    catalogs = {"todo": _catalog("todo", todo), "timeline": _catalog("timeline", timeline), "materials": _catalog("materials", materials)}
    outputs: Dict[str, bytes] = {}
    for view, catalog in catalogs.items():
        json_path, markdown_path = GENERATED_PATHS[view]
        outputs[json_path] = _pretty_json(catalog)
        outputs[markdown_path] = _render_markdown(view, catalog)
    return outputs


def _valid_generated_marker(content: bytes, view: str) -> bool:
    try:
        first = content.decode("utf-8").splitlines()[0]
    except (UnicodeDecodeError, IndexError):
        return False
    return bool(re.fullmatch(
        rf"<!-- workspace-organizer:generated view={view} schema=1 source_sha256=[0-9a-f]{{64}} -->",
        first,
    ))


def _replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _validate_generated_target(root: Path, relative: str) -> None:
    parts = PurePosixPath(validate_relative_path(relative)).parts
    current = root
    for index, part in enumerate(parts):
        matches = [entry for entry in current.iterdir() if _nfc(entry.name).casefold() == _nfc(part).casefold()] if current.exists() else []
        if len(matches) > 1 or (matches and matches[0].name != part):
            raise _error(relative, "generated output has a normalized path collision")
        candidate = matches[0] if matches else current / part
        if candidate.is_symlink():
            raise _error(relative, "generated output path contains a symlink")
        if candidate.exists() and index < len(parts) - 1 and not candidate.is_dir():
            raise _error(relative, "generated output parent is not a directory")
        current = candidate


def generate_indexes(root: Path) -> Dict[str, Any]:
    root = _workspace_root(root)
    outputs = build_generated_outputs(root)
    for relative in outputs:
        _validate_generated_target(root, relative)
    for view, (_, markdown_relative) in GENERATED_PATHS.items():
        target = root.joinpath(*PurePosixPath(markdown_relative).parts)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or not _valid_generated_marker(target.read_bytes(), view):
                raise _error(markdown_relative, "existing overview is user-owned or has a different marker")
    if all(
        (root.joinpath(*PurePosixPath(relative).parts)).is_file()
        and not (root.joinpath(*PurePosixPath(relative).parts)).is_symlink()
        and (root.joinpath(*PurePosixPath(relative).parts)).read_bytes() == content
        for relative, content in outputs.items()
    ):
        return {
            "schema_version": 1,
            "operation": "index",
            "status": "already_current",
            "outputs": [{"path": relative, "sha256": _sha256_bytes(content)} for relative, content in sorted(outputs.items())],
        }
    cache = _control_subdirectory(root, "cache", create=True)
    transaction = Path(tempfile.mkdtemp(prefix="index-", dir=cache))
    prior: Dict[str, Optional[bytes]] = {}
    committed: List[str] = []
    try:
        for relative, content in outputs.items():
            target = root.joinpath(*PurePosixPath(relative).parts)
            prior[relative] = target.read_bytes() if target.exists() and target.is_file() and not target.is_symlink() else None
            stage = transaction.joinpath(*PurePosixPath(relative).parts)
            stage.parent.mkdir(parents=True, exist_ok=True)
            with stage.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for relative in sorted(outputs, key=_collision_key):
            target = root.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = transaction.joinpath(*PurePosixPath(relative).parts)
            _replace(stage, target)
            committed.append(relative)
        for relative, content in outputs.items():
            target = root.joinpath(*PurePosixPath(relative).parts)
            if not target.is_file() or target.is_symlink() or _sha256_file(target) != _sha256_bytes(content):
                raise _error(relative, "generated output failed post-commit verification")
    except Exception:
        for relative in reversed(committed):
            target = root.joinpath(*PurePosixPath(relative).parts)
            old = prior[relative]
            if old is None:
                if target.exists() and target.is_file() and not target.is_symlink():
                    target.unlink()
            else:
                restore = transaction / "restore" / PurePosixPath(relative)
                restore.parent.mkdir(parents=True, exist_ok=True)
                restore.write_bytes(old)
                os.replace(restore, target)
        raise
    finally:
        shutil.rmtree(transaction, ignore_errors=True)
    return {
        "schema_version": 1,
        "operation": "index",
        "status": "verified",
        "outputs": [{"path": relative, "sha256": _sha256_bytes(content)} for relative, content in sorted(outputs.items())],
    }


def inspect_compressed(
    root: Path,
    relative: str,
    *,
    confirmed: bool,
    max_entries: int = 1000,
    max_total_bytes: int = 512 * 1024 * 1024,
    max_source_bytes: int = DEFAULT_COMPRESSED_SOURCE_LIMIT,
    max_metadata_bytes: int = DEFAULT_COMPRESSED_METADATA_LIMIT,
) -> Dict[str, Any]:
    if not confirmed:
        raise _error("compressed inspection", "explicit confirmation is required")
    root = _workspace_root(root)
    config = load_config(root)
    relative = validate_relative_path(relative)
    if not (_path_contains("10_收件箱", relative) or _path_contains("99_待整理", relative)):
        raise _error(relative, "compressed originals may only be inspected from inbox or staging")
    if not _is_compressed(relative):
        raise _error(relative, "file extension is not a supported compressed-original type")
    if min(max_entries, max_total_bytes, max_source_bytes, max_metadata_bytes) <= 0:
        raise _error(relative, "compressed-inspection resource limits must be positive")
    entries: List[Dict[str, Any]] = []
    total = 0

    def accept(name: str, size: int, kind: str) -> None:
        nonlocal total
        name = name.rstrip("/")
        if not name:
            return
        normalized = _nfc(name)
        validate_relative_path(normalized, f"{relative} member")
        if normalized != name:
            raise _error(relative, "archive member path must already use NFC")
        if kind not in {"file", "directory"}:
            raise _error(relative, "archive contains a link, device, or unsupported member")
        total += size
        if len(entries) >= max_entries or total > max_total_bytes:
            raise _error(relative, "archive exceeds configured metadata resource limits")
        entries.append({"path": name, "kind": kind, "bytes": size})

    source_parent_fd, source_fd, _, source_before = _open_file_descriptor(root, relative, config)
    try:
        if source_before.st_size > max_source_bytes:
            raise _error(relative, "compressed source exceeds configured byte limit")
        with tempfile.TemporaryFile(
            prefix="workspace-organizer-compressed-", dir="/tmp"
        ) as scratch:
            os.lseek(source_fd, 0, os.SEEK_SET)
            copied = 0
            while True:
                chunk = os.read(source_fd, min(1024 * 1024, max_source_bytes - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_source_bytes:
                    raise _error(relative, "compressed source exceeds configured byte limit")
                scratch.write(chunk)
            scratch.flush()
            source_after = os.fstat(source_fd)
            if (
                not _same_identity(source_before, source_after)
                or source_before.st_size != copied
                or source_before.st_mtime_ns != source_after.st_mtime_ns
                or source_before.st_ctime_ns != source_after.st_ctime_ns
            ):
                raise _error(relative, "compressed source changed during descriptor-bound snapshot")
            scratch.seek(0)
            is_zip = zipfile.is_zipfile(scratch)
            scratch.seek(0)
            if is_zip:
                tail_size = min(copied, 65557)
                scratch.seek(copied - tail_size)
                tail = scratch.read(tail_size)
                eocd_index = tail.rfind(b"PK\x05\x06")
                if eocd_index < 0 or len(tail) - eocd_index < 22:
                    raise _error(relative, "ZIP end-of-central-directory record is missing")
                eocd_offset = copied - tail_size + eocd_index
                (
                    _, disk_number, central_disk, disk_entries, declared_entries,
                    central_bytes, central_offset, comment_bytes,
                ) = struct.unpack("<4s4H2LH", tail[eocd_index:eocd_index + 22])
                if (
                    disk_number != 0 or central_disk != 0 or disk_entries != declared_entries
                    or declared_entries == 0xFFFF or central_bytes == 0xFFFFFFFF
                    or central_offset == 0xFFFFFFFF
                ):
                    raise _error(relative, "split or ZIP64 archives are not supported for bounded inspection")
                if eocd_offset + 22 + comment_bytes != copied:
                    raise _error(relative, "ZIP has an abnormal trailing or comment declaration")
                if central_offset + central_bytes != eocd_offset:
                    raise _error(relative, "ZIP central-directory offsets are inconsistent")
                if declared_entries > max_entries or central_bytes > max_metadata_bytes:
                    raise _error(relative, "ZIP central-directory metadata exceeds configured limits")
                scratch.seek(0)
                with zipfile.ZipFile(scratch) as archive:
                    infos = archive.infolist()
                    if len(infos) != declared_entries:
                        raise _error(relative, "ZIP entry count disagrees with its bounded declaration")
                    for info in infos:
                        mode = (info.external_attr >> 16) & 0xFFFF
                        kind = "directory" if info.is_dir() else "file"
                        if stat.S_ISLNK(mode):
                            kind = "symlink"
                        accept(info.filename, info.file_size, kind)
            else:
                scratch.seek(0)
                try:
                    archive = tarfile.open(fileobj=scratch, mode="r:*")
                except tarfile.TarError as exc:
                    raise _error(relative, "unsupported or malformed compressed original") from exc
                with archive:
                    for info in archive:
                        kind = "directory" if info.isdir() else "file" if info.isfile() else "unsupported"
                        accept(info.name, info.size, kind)
    finally:
        os.close(source_fd)
        os.close(source_parent_fd)
    entries.sort(key=lambda item: _collision_key(item["path"]))
    seen: Dict[Tuple[str, ...], str] = {}
    for item in entries:
        key = _collision_key(item["path"])
        if key in seen:
            raise _error(relative, f"archive member collision between {seen[key]!r} and {item['path']!r}")
        seen[key] = item["path"]
    return {
        "schema_version": 1,
        "operation": "inspect-compressed",
        "source": relative,
        "sensitivity": "restricted",
        "entries": entries,
        "entry_count": len(entries),
        "total_uncompressed_bytes": total,
        "source_bytes": copied,
        "limits": {
            "max_entries": max_entries,
            "max_total_bytes": max_total_bytes,
            "max_source_bytes": max_source_bytes,
            "max_metadata_bytes": max_metadata_bytes,
        },
        "content_extracted": False,
        "source_mutated": False,
    }


def _parse_material(value: str) -> Dict[str, str]:
    path, separator, sensitivity = value.rpartition("=")
    if not separator:
        raise argparse.ArgumentTypeError("material roots use PATH=SENSITIVITY")
    return {"path": path, "sensitivity": sensitivity}


def _read_moves(path: Path) -> List[Dict[str, str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(str(path), f"cannot read moves JSON: {exc}") from exc
    if not isinstance(value, list):
        raise _error(str(path), "moves JSON must be an array")
    return value


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(_pretty_json(value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "scan"):
        command = subparsers.add_parser(name)
        command.add_argument("root", type=Path)
        command.add_argument("--hash-limit", type=int, default=DEFAULT_SCAN_HASH_LIMIT)
    initialize = subparsers.add_parser("plan-init")
    initialize.add_argument("root", type=Path)
    initialize.add_argument("--workspace-id", required=True)
    initialize.add_argument("--default-sensitivity", choices=SENSITIVITY_ORDER, default="internal")
    initialize.add_argument("--adopt-task", action="append", default=[])
    initialize.add_argument("--adopt-material", action="append", type=_parse_material, default=[])
    initialize.add_argument("--exclude", action="append", default=[])
    initialize.add_argument("--accept-existing-managed", action="append", default=[])
    initialize.add_argument("--output", type=Path, required=True)
    organize = subparsers.add_parser("plan-organize")
    organize.add_argument("root", type=Path)
    organize.add_argument("--moves", type=Path, required=True)
    organize.add_argument("--allow-compressed-source", action="store_true")
    organize.add_argument("--output", type=Path, required=True)
    archive = subparsers.add_parser("plan-archive")
    archive.add_argument("root", type=Path)
    archive.add_argument("--task-id", required=True)
    archive.add_argument("--archived-at", required=True)
    archive.add_argument("--output", type=Path, required=True)
    approval = subparsers.add_parser("approve")
    approval.add_argument("--plan", type=Path, required=True)
    approval.add_argument("--output", type=Path, required=True)
    approval.add_argument("--yes", action="store_true")
    dry = subparsers.add_parser("dry-run")
    dry.add_argument("root", type=Path)
    dry.add_argument("--plan", type=Path, required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("root", type=Path)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--approval", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("root", type=Path)
    verify.add_argument("--plan", type=Path, required=True)
    index = subparsers.add_parser("index")
    index.add_argument("root", type=Path)
    compressed = subparsers.add_parser("inspect-compressed")
    compressed.add_argument("root", type=Path)
    compressed.add_argument("path")
    compressed.add_argument("--yes", action="store_true")
    compressed.add_argument("--max-entries", type=int, default=1000)
    compressed.add_argument("--max-total-bytes", type=int, default=512 * 1024 * 1024)
    compressed.add_argument("--max-source-bytes", type=int, default=DEFAULT_COMPRESSED_SOURCE_LIMIT)
    compressed.add_argument("--max-metadata-bytes", type=int, default=DEFAULT_COMPRESSED_METADATA_LIMIT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "inventory":
            result = inventory_workspace(arguments.root, hash_limit=arguments.hash_limit)
        elif arguments.command == "scan":
            result = scan_workspace(arguments.root, hash_limit=arguments.hash_limit)
        elif arguments.command == "plan-init":
            result = build_initialization_plan(
                arguments.root, arguments.workspace_id,
                default_sensitivity=arguments.default_sensitivity,
                adopted_task_paths=arguments.adopt_task,
                adopted_material_roots=arguments.adopt_material,
                exclude_paths=arguments.exclude,
                accepted_existing_managed=arguments.accept_existing_managed,
            )
            write_immutable_json(arguments.output, result)
        elif arguments.command == "plan-organize":
            result = build_organize_plan(arguments.root, _read_moves(arguments.moves), allow_compressed_source=arguments.allow_compressed_source)
            write_immutable_json(arguments.output, result)
        elif arguments.command == "plan-archive":
            result = build_archive_plan(arguments.root, arguments.task_id, arguments.archived_at)
            write_immutable_json(arguments.output, result)
        elif arguments.command == "approve":
            result = approve_plan(arguments.plan, arguments.output, confirmed=arguments.yes)
        elif arguments.command == "dry-run":
            result = dry_run(arguments.root, arguments.plan)
        elif arguments.command == "apply":
            result = apply_plan(arguments.root, arguments.plan, arguments.approval)
        elif arguments.command == "verify":
            result = verify_plan(arguments.root, arguments.plan)
        elif arguments.command == "index":
            result = generate_indexes(arguments.root)
        else:
            result = inspect_compressed(
                arguments.root, arguments.path, confirmed=arguments.yes,
                max_entries=arguments.max_entries, max_total_bytes=arguments.max_total_bytes,
                max_source_bytes=arguments.max_source_bytes,
                max_metadata_bytes=arguments.max_metadata_bytes,
            )
        _emit(result)
        if arguments.command == "dry-run" and result.get("status") == "blocked":
            return 2
        return 0
    except (WorkspaceError, OSError) as exc:
        sys.stderr.write(f"workspace-organizer: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
