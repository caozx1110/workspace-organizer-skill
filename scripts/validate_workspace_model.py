#!/usr/bin/env python3
"""Read-only conformance checks for the workspace-model v1 examples.

This module intentionally validates only the design contract. It never creates,
moves, archives, overwrites, or deletes workspace content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


TASK_REQUIRED = {
    "schema_version",
    "id",
    "title",
    "status",
    "area",
    "type",
    "priority",
    "due",
    "sensitivity",
    "next_action",
    "updated",
    "closed_at",
    "archived_at",
}
TASK_OPTIONAL = {"tags"}
CONFIG_REQUIRED = {
    "schema_version",
    "workspace_id",
    "default_sensitivity",
    "adopted_task_paths",
    "adopted_material_roots",
    "exclude_paths",
}

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)

STATUSES = {
    "planned",
    "active",
    "waiting",
    "blocked",
    "completed",
    "cancelled",
    "archived",
}
OPEN_STATUSES = {"planned", "active", "waiting", "blocked"}
CLOSED_STATUSES = {"completed", "cancelled"}
PRIORITIES = {"urgent", "high", "normal", "low"}
PRIORITY_ORDER = ("urgent", "high", "normal", "low")
PRIORITY_RANK = {value: index for index, value in enumerate(PRIORITY_ORDER)}
SENSITIVITIES = {"public", "internal", "confidential", "restricted"}
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")
SENSITIVITY_RANK = {
    value: index for index, value in enumerate(SENSITIVITY_ORDER)
}
VISIBLE_SENSITIVITIES = {"public", "internal"}
MATERIAL_ROLES = {
    "inputs",
    "work",
    "deliverables",
    "records",
    "history",
    "library",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """A workspace-model input does not conform to the v1 contract."""


def _error(context: str, message: str) -> ContractError:
    return ContractError(f"{context}: {message}")


def _is_nfc(value: str) -> bool:
    return value == unicodedata.normalize("NFC", value)


def _require_nfc(value: str, context: str) -> None:
    if not _is_nfc(value):
        raise _error(context, "must use Unicode NFC")


def _parse_front_matter_scalar(raw: str, context: str) -> Any:
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
    raise _error(
        context,
        "values must be null, integer 1, a bare ASCII slug, or a JSON string/array",
    )


def parse_task(path: Path) -> Dict[str, Any]:
    """Parse the restricted YAML front matter required by TASK.md."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error(str(path), f"cannot read UTF-8 text: {exc}") from exc

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _error(str(path), "first line must be ---")

    data: Dict[str, Any] = {}
    closing_line: Optional[int] = None
    for index, line in enumerate(lines[1:], start=2):
        if line == "---":
            closing_line = index
            break
        if not line:
            continue
        if line != line.strip():
            raise _error(str(path), f"line {index} has surrounding indentation/space")
        key, separator, raw = line.partition(": ")
        if not separator or not KEY_RE.fullmatch(key):
            raise _error(str(path), f"line {index} must be one key: value pair")
        if key in data:
            raise _error(str(path), f"duplicate front matter key {key!r}")
        data[key] = _parse_front_matter_scalar(raw, f"{path}:{index}")

    if closing_line is None:
        raise _error(str(path), "front matter has no closing ---")
    return data


def _validate_slug(value: Any, context: str, *, minimum: int = 1, maximum: int = 48) -> None:
    if not isinstance(value, str) or not (minimum <= len(value) <= maximum):
        raise _error(context, f"must be a {minimum}-{maximum} character string")
    if not ID_RE.fullmatch(value):
        raise _error(context, "must be a lowercase ASCII slug")


def _validate_human_string(value: Any, context: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _error(context, f"must be a non-empty string of at most {maximum} characters")
    if value != value.strip() or "\n" in value or "\r" in value:
        raise _error(context, "must be trimmed and single-line")
    _require_nfc(value, context)


def _parse_date(value: Any, context: str) -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise _error(context, "must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _error(context, "must be an actual Gregorian date") from exc


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


def validate_task(data: Mapping[str, Any], context: str = "TASK.md") -> None:
    keys = set(data)
    missing = sorted(TASK_REQUIRED - keys)
    unknown = sorted(keys - TASK_REQUIRED - TASK_OPTIONAL)
    if missing:
        raise _error(context, f"missing required keys: {', '.join(missing)}")
    if unknown:
        raise _error(context, f"unknown schema-v1 keys: {', '.join(unknown)}")

    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise _error(context, "schema_version must be integer 1")
    _validate_slug(data["id"], f"{context}.id", minimum=3, maximum=64)
    _validate_human_string(data["title"], f"{context}.title", 200)
    _validate_slug(data["area"], f"{context}.area")
    _validate_slug(data["type"], f"{context}.type")

    status = data["status"]
    if not isinstance(status, str) or status not in STATUSES:
        raise _error(f"{context}.status", f"must be one of {sorted(STATUSES)}")
    if not isinstance(data["priority"], str) or data["priority"] not in PRIORITIES:
        raise _error(f"{context}.priority", f"must be one of {sorted(PRIORITIES)}")
    if (
        not isinstance(data["sensitivity"], str)
        or data["sensitivity"] not in SENSITIVITIES
    ):
        raise _error(
            f"{context}.sensitivity", f"must be one of {sorted(SENSITIVITIES)}"
        )

    due = data["due"]
    if due is not None:
        _parse_date(due, f"{context}.due")
    updated = _parse_timestamp(data["updated"], f"{context}.updated")

    closed_value = data["closed_at"]
    archived_value = data["archived_at"]
    closed_at = (
        _parse_timestamp(closed_value, f"{context}.closed_at")
        if closed_value is not None
        else None
    )
    archived_at = (
        _parse_timestamp(archived_value, f"{context}.archived_at")
        if archived_value is not None
        else None
    )

    if status in OPEN_STATUSES:
        _validate_human_string(data["next_action"], f"{context}.next_action", 500)
        if closed_at is not None or archived_at is not None:
            raise _error(context, "open states require null closed_at and archived_at")
    elif status in CLOSED_STATUSES:
        if data["next_action"] is not None:
            raise _error(context, "closed states require null next_action")
        if closed_at is None or archived_at is not None:
            raise _error(context, "closed states require closed_at and null archived_at")
        if updated < closed_at:
            raise _error(context, "updated must be at or after closed_at")
    else:
        if data["next_action"] is not None:
            raise _error(context, "archived state requires null next_action")
        if closed_at is None or archived_at is None:
            raise _error(context, "archived state requires closed_at and archived_at")
        if archived_at < closed_at:
            raise _error(context, "archived_at must be at or after closed_at")
        if updated < archived_at:
            raise _error(context, "updated must be at or after archived_at")

    tags = data.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 32:
        raise _error(f"{context}.tags", "must be an array of at most 32 slugs")
    for index, tag in enumerate(tags):
        _validate_slug(tag, f"{context}.tags[{index}]")
    if len(tags) != len(set(tags)):
        raise _error(f"{context}.tags", "must contain unique values")


def validate_relative_path(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(context, "must be a non-empty string")
    _require_nfc(value, context)
    if (
        value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise _error(context, "must be a normalized workspace-relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _error(context, "must not contain empty, . or .. segments")
    if PurePosixPath(value).is_absolute():
        raise _error(context, "must not be absolute")
    return value


def _reject_normalized_duplicates(values: Iterable[str], context: str) -> None:
    seen: Dict[str, str] = {}
    for value in values:
        key = unicodedata.normalize("NFC", value).casefold()
        if key in seen:
            raise _error(context, f"normalized collision between {seen[key]!r} and {value!r}")
        seen[key] = value


def _normalized_path_parts(value: str) -> Tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold() for part in value.split("/")
    )


def _path_contains(root: str, candidate: str) -> bool:
    root_parts = _normalized_path_parts(root)
    candidate_parts = _normalized_path_parts(candidate)
    return (
        len(root_parts) <= len(candidate_parts)
        and candidate_parts[: len(root_parts)] == root_parts
    )


def _paths_overlap(left: str, right: str) -> bool:
    return _path_contains(left, right) or _path_contains(right, left)


def _reject_overlaps(values: Sequence[str], context: str) -> None:
    for index, left in enumerate(values):
        for right in values[index + 1 :]:
            if _paths_overlap(left, right):
                raise _error(context, f"overlapping paths {left!r} and {right!r}")


def is_excluded_path(relative: str, config: Mapping[str, Any]) -> bool:
    validate_relative_path(relative, "candidate path")
    return any(_path_contains(excluded, relative) for excluded in config["exclude_paths"])


def effective_material_sensitivity(
    relative: str, config: Mapping[str, Any]
) -> str:
    """Return the most restrictive sensitivity that applies to a material path."""

    validate_relative_path(relative, "material path")
    applicable = [config["default_sensitivity"]]
    for declaration in config["adopted_material_roots"]:
        if _path_contains(declaration["path"], relative):
            applicable.append(declaration["sensitivity"])
    return max(applicable, key=lambda value: SENSITIVITY_RANK[value])


def validate_config(data: Mapping[str, Any], context: str = "config.json") -> None:
    keys = set(data)
    missing = sorted(CONFIG_REQUIRED - keys)
    unknown = sorted(keys - CONFIG_REQUIRED)
    if missing:
        raise _error(context, f"missing required keys: {', '.join(missing)}")
    if unknown:
        raise _error(context, f"unknown schema-v1 keys: {', '.join(unknown)}")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise _error(context, "schema_version must be integer 1")
    _validate_slug(data["workspace_id"], f"{context}.workspace_id", minimum=3, maximum=64)
    if (
        not isinstance(data["default_sensitivity"], str)
        or data["default_sensitivity"] not in SENSITIVITIES
    ):
        raise _error(f"{context}.default_sensitivity", "has an unknown value")

    adopted_tasks = data["adopted_task_paths"]
    material_roots = data["adopted_material_roots"]
    excluded = data["exclude_paths"]
    if not isinstance(adopted_tasks, list):
        raise _error(f"{context}.adopted_task_paths", "must be an array")
    if not isinstance(material_roots, list):
        raise _error(f"{context}.adopted_material_roots", "must be an array")
    if not isinstance(excluded, list):
        raise _error(f"{context}.exclude_paths", "must be an array")

    task_paths: List[str] = []
    material_paths: List[str] = []
    excluded_paths: List[str] = []
    for index, value in enumerate(adopted_tasks):
        task_paths.append(
            validate_relative_path(value, f"{context}.adopted_task_paths[{index}]")
        )
    for index, item in enumerate(material_roots):
        item_context = f"{context}.adopted_material_roots[{index}]"
        if not isinstance(item, dict) or set(item) != {"path", "sensitivity"}:
            raise _error(item_context, "must contain only path and sensitivity")
        material_paths.append(
            validate_relative_path(item["path"], f"{item_context}.path")
        )
        if (
            not isinstance(item["sensitivity"], str)
            or item["sensitivity"] not in SENSITIVITIES
        ):
            raise _error(f"{item_context}.sensitivity", "has an unknown value")
    for index, value in enumerate(excluded):
        excluded_paths.append(
            validate_relative_path(value, f"{context}.exclude_paths[{index}]")
        )

    _reject_normalized_duplicates(task_paths, f"{context}.adopted_task_paths")
    _reject_normalized_duplicates(
        material_paths, f"{context}.adopted_material_roots"
    )
    _reject_normalized_duplicates(excluded_paths, f"{context}.exclude_paths")
    _reject_overlaps(task_paths, f"{context}.adopted_task_paths")
    _reject_overlaps(excluded_paths, f"{context}.exclude_paths")

    for task_path in task_paths:
        for material_path in material_paths:
            if _paths_overlap(task_path, material_path):
                raise _error(
                    context,
                    "adopted task and material roots must not overlap: "
                    f"{task_path!r} and {material_path!r}",
                )

    for registered_path in task_paths + material_paths:
        for excluded_path in excluded_paths:
            if _path_contains(excluded_path, registered_path):
                raise _error(
                    context,
                    f"registered root {registered_path!r} is inside excluded root "
                    f"{excluded_path!r}",
                )


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(str(path), f"cannot read JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _error(str(path), "top-level JSON value must be an object")
    return value


def _relative_to(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _resolve_workspace_root(root: Path) -> Path:
    lexical_root = root.absolute()
    if lexical_root.is_symlink():
        raise _error(str(root), "workspace root must not be a symlink")
    try:
        resolved = lexical_root.resolve(strict=True)
    except OSError as exc:
        raise _error(str(root), f"workspace root cannot be resolved: {exc}") from exc
    if not resolved.is_dir():
        raise _error(str(root), "workspace root must be a directory")
    return resolved


def _safe_existing_file(root: Path, relative: str) -> Path:
    validate_relative_path(relative, relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise _error(relative, "symlink components and targets are not allowed")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _error(relative, f"required file cannot be resolved: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _error(relative, "resolved path escapes the workspace") from exc
    if not resolved.is_file():
        raise _error(relative, "must resolve to a regular file")
    return candidate


def _has_nested_git_boundary(root: Path, directory: Path) -> bool:
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise _error(str(directory), "path escapes the workspace") from exc
    current = directory
    while current != root:
        marker = current / ".git"
        if marker.is_symlink() or marker.exists():
            return True
        current = current.parent
    return False


def load_workspace(root: Path) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    """Load and validate one workspace without writing to it."""

    root = _resolve_workspace_root(root)
    config_relative = ".workspace-organizer/config.json"
    config_path = _safe_existing_file(root, config_relative)
    config = load_json(config_path)
    validate_config(config, config_relative)

    task_paths: List[Tuple[Path, str]] = []
    active_root = root / "20_任务"
    if active_root.exists():
        canonical = sorted(active_root.glob("*/TASK.md"), key=lambda path: path.as_posix())
        nested = sorted(
            set(active_root.rglob("TASK.md")) - set(canonical),
            key=lambda path: path.as_posix(),
        )
        for task_path in nested:
            relative = _relative_to(root, task_path)
            if is_excluded_path(relative, config) or _has_nested_git_boundary(
                root, task_path.parent
            ):
                continue
            _safe_existing_file(root, relative)
            raise _error(
                relative, "canonical TASK.md must be one level below 20_任务"
            )
        for task_path in canonical:
            relative = _relative_to(root, task_path)
            if not is_excluded_path(relative, config):
                task_paths.append((task_path, "active"))

    for relative in config["adopted_task_paths"]:
        bundle = root.joinpath(*PurePosixPath(relative).parts)
        task_path = bundle / "TASK.md"
        task_relative = _relative_to(root, task_path)
        if is_excluded_path(task_relative, config):
            raise _error(relative, "registered adopted task is excluded")
        task_paths.append((task_path, "adopted"))

    archive_root = root / "90_归档"
    if archive_root.exists():
        archived = sorted(
            archive_root.glob("*/*/*/TASK.md"), key=lambda path: path.as_posix()
        )
        for task_path in archived:
            relative = _relative_to(root, task_path)
            if not is_excluded_path(relative, config):
                task_paths.append((task_path, "archived"))

    records: List[Tuple[str, Dict[str, Any]]] = []
    seen_ids: Dict[str, str] = {}
    seen_paths: List[str] = []
    for task_path, location in task_paths:
        relative = _relative_to(root, task_path)
        task_path = _safe_existing_file(root, relative)
        if _has_nested_git_boundary(root, task_path.parent):
            if location == "adopted":
                raise _error(relative, "registered adopted task crosses a nested Git boundary")
            continue
        data = parse_task(task_path)
        validate_task(data, relative)
        task_id = data["id"]
        if task_id in seen_ids:
            raise _error(relative, f"duplicate ID also used by {seen_ids[task_id]}")
        seen_ids[task_id] = relative
        seen_paths.append(relative)

        if location == "active":
            if task_path.parent.name != task_id:
                raise _error(relative, "canonical bundle directory must equal task ID")
            if data["status"] == "archived":
                raise _error(relative, "archived tasks belong under 90_归档")
        elif location == "adopted":
            if data["status"] == "archived":
                raise _error(relative, "archived tasks cannot remain registered as adopted")
        else:
            if data["status"] != "archived":
                raise _error(relative, "records under 90_归档 must have archived status")
            destination = task_path.relative_to(archive_root).parts
            closed_year, area, path_id, _ = destination
            if (
                closed_year != data["closed_at"][:4]
                or area != data["area"]
                or path_id != task_id
            ):
                raise _error(relative, "archive path must derive from closed_at, area, and ID")
        records.append((relative, data))

    _reject_normalized_duplicates(seen_paths, "task record paths")
    return config, records


def validate_lifecycle(path: Path) -> Dict[str, Any]:
    lifecycle = load_json(path)
    if lifecycle.get("schema_version") != 1:
        raise _error(str(path), "schema_version must be 1")
    transitions = lifecycle.get("transitions")
    if not isinstance(transitions, dict) or set(transitions) != STATUSES:
        raise _error(str(path), "transition keys must cover every status exactly once")
    for source, destinations in transitions.items():
        if not isinstance(destinations, list) or not all(
            isinstance(destination, str) for destination in destinations
        ):
            raise _error(str(path), f"{source} transitions must be a string array")
        if len(destinations) != len(set(destinations)):
            raise _error(str(path), f"{source} transitions must be a unique array")
        if not set(destinations) <= STATUSES or source in destinations:
            raise _error(str(path), f"{source} has an unknown or self transition")
    if set(lifecycle.get("open_statuses", [])) != OPEN_STATUSES:
        raise _error(str(path), "open_statuses disagree with TASK.md schema")
    if set(lifecycle.get("closed_statuses", [])) != CLOSED_STATUSES:
        raise _error(str(path), "closed_statuses disagree with TASK.md schema")
    if set(lifecycle.get("terminal_statuses", [])) != {"archived"}:
        raise _error(str(path), "archived must be the only terminal status")
    if transitions["archived"]:
        raise _error(str(path), "archived must have no normal transition")
    if set(lifecycle.get("archive_eligible_from", [])) != CLOSED_STATUSES:
        raise _error(str(path), "archive eligibility must use the closed states")
    return lifecycle


def _canonical_digest(items: List[Dict[str, Any]]) -> str:
    payload = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_exact_keys(item: Mapping[str, Any], keys: set, context: str) -> None:
    actual = set(item)
    if actual == keys:
        return
    missing = sorted(keys - actual)
    unknown = sorted(actual - keys)
    details = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if unknown:
        details.append(f"unknown {', '.join(unknown)}")
    raise _error(context, "; ".join(details))


def validate_generated_view(view: Mapping[str, Any], context: str = "generated view") -> None:
    _require_exact_keys(
        view,
        {"schema_version", "view", "profile", "source_sha256", "items"},
        context,
    )
    if type(view["schema_version"]) is not int or view["schema_version"] != 1:
        raise _error(context, "schema_version must be integer 1")
    view_name = view["view"]
    if not isinstance(view_name, str) or view_name not in {
        "todo",
        "timeline",
        "materials",
    }:
        raise _error(context, "view has an unknown value")
    if view["profile"] != "default":
        raise _error(context, "profile must be default")
    if not isinstance(view["source_sha256"], str) or not SHA256_RE.fullmatch(
        view["source_sha256"]
    ):
        raise _error(context, "source_sha256 must be lowercase SHA-256 hex")
    if not isinstance(view["items"], list) or not all(
        isinstance(item, dict) for item in view["items"]
    ):
        raise _error(context, "items must be an array of objects")

    for index, item in enumerate(view["items"]):
        item_context = f"{context}.items[{index}]"
        if view_name == "todo":
            _require_exact_keys(
                item,
                {
                    "id",
                    "title",
                    "status",
                    "priority",
                    "due",
                    "sensitivity",
                    "next_action",
                    "record",
                },
                item_context,
            )
            _validate_slug(item["id"], f"{item_context}.id", minimum=3, maximum=64)
            _validate_human_string(item["title"], f"{item_context}.title", 200)
            if (
                not isinstance(item["status"], str)
                or item["status"] not in OPEN_STATUSES
            ):
                raise _error(item_context, "TODO status must be open")
            if (
                not isinstance(item["priority"], str)
                or item["priority"] not in PRIORITIES
            ):
                raise _error(item_context, "TODO priority is invalid")
            if item["due"] is not None:
                _parse_date(item["due"], f"{item_context}.due")
            if (
                not isinstance(item["sensitivity"], str)
                or item["sensitivity"] not in VISIBLE_SENSITIVITIES
            ):
                raise _error(item_context, "TODO sensitivity is not default-visible")
            _validate_human_string(
                item["next_action"], f"{item_context}.next_action", 500
            )
            record = validate_relative_path(item["record"], f"{item_context}.record")
            if not record.endswith("/TASK.md"):
                raise _error(item_context, "record must point to TASK.md")
        elif view_name == "timeline":
            _require_exact_keys(
                item,
                {
                    "date",
                    "event",
                    "id",
                    "title",
                    "status",
                    "priority",
                    "sensitivity",
                    "record",
                },
                item_context,
            )
            _parse_date(item["date"], f"{item_context}.date")
            if item["event"] != "due":
                raise _error(item_context, "timeline event must be due")
            _validate_slug(item["id"], f"{item_context}.id", minimum=3, maximum=64)
            _validate_human_string(item["title"], f"{item_context}.title", 200)
            if (
                not isinstance(item["status"], str)
                or item["status"] not in OPEN_STATUSES
            ):
                raise _error(item_context, "timeline status must be open")
            if (
                not isinstance(item["priority"], str)
                or item["priority"] not in PRIORITIES
            ):
                raise _error(item_context, "timeline priority is invalid")
            if (
                not isinstance(item["sensitivity"], str)
                or item["sensitivity"] not in VISIBLE_SENSITIVITIES
            ):
                raise _error(item_context, "timeline sensitivity is not default-visible")
            record = validate_relative_path(item["record"], f"{item_context}.record")
            if not record.endswith("/TASK.md"):
                raise _error(item_context, "record must point to TASK.md")
        else:
            _require_exact_keys(
                item,
                {"path", "role", "task_id", "sensitivity", "bytes", "sha256"},
                item_context,
            )
            validate_relative_path(item["path"], f"{item_context}.path")
            if not isinstance(item["role"], str) or item["role"] not in MATERIAL_ROLES:
                raise _error(item_context, "material role is invalid")
            if item["task_id"] is not None:
                _validate_slug(
                    item["task_id"],
                    f"{item_context}.task_id",
                    minimum=3,
                    maximum=64,
                )
            if item["role"] == "library" and item["task_id"] is not None:
                raise _error(item_context, "library material must have null task_id")
            if item["role"] != "library" and item["task_id"] is None:
                raise _error(item_context, "task material must have a task_id")
            if (
                not isinstance(item["sensitivity"], str)
                or item["sensitivity"] not in VISIBLE_SENSITIVITIES
            ):
                raise _error(item_context, "material sensitivity is not default-visible")
            if type(item["bytes"]) is not int or item["bytes"] < 0:
                raise _error(item_context, "material bytes must be a non-negative integer")
            if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(
                item["sha256"]
            ):
                raise _error(item_context, "material sha256 must be lowercase hex")

    if view_name == "todo":
        expected = sorted(
            view["items"],
            key=lambda item: (
                PRIORITY_RANK[item["priority"]],
                item["due"] is None,
                item["due"] or "",
                item["id"],
            ),
        )
        identifiers = [item["id"] for item in view["items"]]
        if view["items"] != expected or len(identifiers) != len(set(identifiers)):
            raise _error(context, "TODO items must be sorted and have unique IDs")
    elif view_name == "timeline":
        expected = sorted(
            view["items"],
            key=lambda item: (
                item["date"],
                PRIORITY_RANK[item["priority"]],
                item["id"],
            ),
        )
        identifiers = [item["id"] for item in view["items"]]
        if view["items"] != expected or len(identifiers) != len(set(identifiers)):
            raise _error(context, "timeline items must be sorted and have unique IDs")
    else:
        expected = sorted(
            view["items"],
            key=lambda item: unicodedata.normalize("NFC", item["path"]),
        )
        paths = [item["path"] for item in view["items"]]
        if view["items"] != expected or len(paths) != len(set(paths)):
            raise _error(context, "material items must be sorted and have unique paths")

    expected_digest = _canonical_digest(view["items"])
    if view["source_sha256"] != expected_digest:
        raise _error(context, "source_sha256 does not match the canonical items array")


def validate_schema_documents(repo_root: Path) -> None:
    schemas: Dict[str, Dict[str, Any]] = {}
    for relative in (
        "schemas/task.schema.json",
        "schemas/workspace-config.schema.json",
        "schemas/generated-view.schema.json",
    ):
        schema = load_json(repo_root / relative)
        schemas[relative] = schema
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise _error(relative, "must declare JSON Schema draft 2020-12")
        if not isinstance(schema.get("$id"), str):
            raise _error(relative, "must declare a stable $id")

    config_pattern = schemas["schemas/workspace-config.schema.json"]["$defs"][
        "relativePath"
    ]["pattern"]
    view_pattern = schemas["schemas/generated-view.schema.json"]["$defs"][
        "relativePath"
    ]["pattern"]
    if config_pattern != view_pattern:
        raise _error("relativePath schemas", "configuration and view patterns differ")
    try:
        compiled = re.compile(config_pattern)
    except re.error as exc:
        raise _error("relativePath schemas", f"invalid regular expression: {exc}") from exc

    probes = {
        "valid/path": True,
        "Unicode 路径/资料.md": True,
        "": False,
        "/absolute": False,
        "trailing/": False,
        "empty//segment": False,
        "dot/./segment": False,
        "parent/../segment": False,
        "backslash\\segment": False,
        "nul\x00segment": False,
    }
    for value, expected in probes.items():
        schema_accepts = compiled.fullmatch(value) is not None
        try:
            validate_relative_path(value, "schema probe")
            validator_accepts = True
        except ContractError:
            validator_accepts = False
        if schema_accepts != expected or validator_accepts != expected:
            raise _error(
                "relativePath schemas",
                f"pattern/validator disagree for {value!r}: "
                f"schema={schema_accepts} validator={validator_accepts}",
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="workspace fixture to validate")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        validate_schema_documents(repo_root)
        validate_lifecycle(repo_root / "contracts" / "lifecycle.json")
        config, records = load_workspace(args.workspace)
        catalog_root = args.workspace / ".workspace-organizer" / "catalog"
        for name in ("todo", "timeline", "materials"):
            validate_generated_view(
                load_json(catalog_root / f"{name}.json"),
                f".workspace-organizer/catalog/{name}.json",
            )
    except ContractError as exc:
        print(f"workspace-model validation failed: {exc}", file=sys.stderr)
        return 1

    print(
        "workspace-model validation passed: "
        f"workspace={config['workspace_id']} tasks={len(records)} schema_version=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
