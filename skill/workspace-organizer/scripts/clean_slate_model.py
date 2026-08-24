#!/usr/bin/env python3
"""Clean-slate Markdown domain model for workspace-organizer.

This module is deliberately independent from the historical v1 implementation
in :mod:`workspace_organizer`.  It contains the small, deterministic domain
layer needed by the new hybrid (Obsidian + agent) workflow:

* strict, safe YAML-frontmatter parsing for Task, Capture and Artifact notes;
* lifecycle validation and the task transition graph;
* compare-and-swap (CAS) updates which never overwrite a concurrent edit;
* append-only event receipts for semantic edits.

The implementation uses only the Python standard library.  Frontmatter is a
conservative YAML subset (JSON flow values, quoted/plain scalars and simple
block lists/maps); anchors, tags, aliases, duplicate keys and executable YAML
constructs are rejected.  Unknown properties are retained and are never
silently discarded.  When a CAS update is applied to an existing note, only the
changed frontmatter lines are rewritten; the user's body and unknown property
lines remain byte-for-byte unchanged.

The filesystem/preview/approval/archive orchestration belongs in a higher
layer.  This module does not move or delete files.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple, Type, TypeVar, Union


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2
# Name used by the clean-slate adapters.  Keep ``SCHEMA_VERSION`` as the
# concise spelling for callers that do not distinguish entity schemas.
CLEAN_SCHEMA_VERSION = SCHEMA_VERSION

OPEN_STATUSES = frozenset({"planned", "active", "waiting", "blocked"})
CLOSED_STATUSES = frozenset({"completed", "cancelled"})
TASK_STATUSES = OPEN_STATUSES | CLOSED_STATUSES
PRIORITIES = frozenset({"urgent", "high", "normal", "low"})
SENSITIVITIES = frozenset({"public", "internal", "confidential", "restricted"})

# The graph intentionally has no ``archived`` business status.  Storage state
# is handled by archive/restore helpers below.
TRANSITIONS: Dict[str, frozenset[str]] = {
    "planned": frozenset({"active", "cancelled"}),
    "active": frozenset({"waiting", "blocked", "completed", "cancelled"}),
    "waiting": frozenset({"active", "blocked", "completed", "cancelled"}),
    "blocked": frozenset({"active", "waiting", "completed", "cancelled"}),
    # Re-opening a closed task is a semantic transition; archiving is not.
    "completed": frozenset({"active"}),
    "cancelled": frozenset({"planned", "active"}),
}

ARTIFACT_ROLES = frozenset(
    {
        "input",
        "work",
        "deliverable",
        "record",
        "history",
        "library",
        "inbox",
        # Accept the v1 directory spellings at the model boundary.  New
        # writers use the singular values above.
        "inputs",
        "deliverables",
        "records",
    }
)
CAPTURE_STATUSES = frozenset({"inbox", "deferred", "triaged"})
CAPTURE_TYPES = frozenset({"text", "file", "link", "email", "transcript", "other"})
CAPTURE_DISPOSITIONS = frozenset({"task", "artifact", "library", "defer"})

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TASK_ID_RE = re.compile(
    r"^(?P<prefix>[0-9]{8}T[0-9]{6})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

TASK_REQUIRED = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "title",
        "outcome",
        "status",
        "storage_state",
        "area",
        "type",
        "priority",
        "scheduled_on",
        "due_on",
        "next_action",
        "waiting_on",
        "follow_up_on",
        "sensitivity",
        "created_at",
        "updated_at",
        "started_at",
        "closed_at",
        "archived_at",
        "closure_summary",
        "tags",
        "aliases",
    }
)

CAPTURE_REQUIRED = frozenset(
    {
        "kind",
        "schema_version",
        "capture_id",
        "title",
        "status",
        "capture_type",
        "payload_path",
        "source",
        "sensitivity",
        "captured_at",
        "updated_at",
        "triaged_at",
        "disposition",
        "target_task",
        "tags",
    }
)

ARTIFACT_REQUIRED = frozenset(
    {
        "kind",
        "schema_version",
        "artifact_id",
        "payload_path",
        "owner_task",
        "role",
        "sensitivity",
        "provenance",
        "sha256",
        "derived_from",
        "created_at",
    }
)


# ---------------------------------------------------------------------------
# Errors and small utility functions
# ---------------------------------------------------------------------------


class ModelError(ValueError):
    """Base class for malformed or unsafe clean-slate model input."""


class FrontmatterError(ModelError):
    """Frontmatter is not valid in the supported safe YAML subset."""


class ValidationError(ModelError):
    """A parsed record does not satisfy its entity schema."""


class StateTransitionError(ValidationError):
    """A requested task lifecycle transition is not permitted."""


class CASConflict(ModelError):
    """The bytes being updated no longer match the expected digest."""

    def __init__(self, expected: str, actual: str, *, context: str = "record") -> None:
        self.expected = expected
        self.actual = actual
        self.context = context
        super().__init__(
            f"{context}: CAS digest mismatch (expected {expected}, actual {actual})"
        )


class ImmutableFieldError(ModelError):
    """An update attempted to change a record's stable identity or kind."""


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest of *value*."""

    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Union[str, Path]) -> str:
    """Hash a file without loading it all into memory."""

    digest = hashlib.sha256()
    target = Path(path)
    if target.is_symlink():
        raise ModelError(f"{target}: symlink payloads are not trusted")
    if not target.is_file():
        raise ModelError(f"{target}: expected a regular file")
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _ensure_nfc(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{context}: must be a string")
    if value != _nfc(value):
        raise ValidationError(f"{context}: must use Unicode NFC")
    return value


def _single_line(value: Any, context: str, *, allow_empty: bool = False, maximum: int = 512) -> str:
    value = _ensure_nfc(value, context)
    if not allow_empty and not value:
        raise ValidationError(f"{context}: must not be empty")
    if len(value) > maximum:
        raise ValidationError(f"{context}: exceeds {maximum} characters")
    if value != value.strip() or "\n" in value or "\r" in value:
        raise ValidationError(f"{context}: must be trimmed single-line text")
    return value


def parse_date(value: Any, context: str = "date") -> Optional[date]:
    if value is None:
        return None
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValidationError(f"{context}: must be YYYY-MM-DD or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{context}: must be an actual Gregorian date") from exc


def parse_timestamp(value: Any, context: str = "timestamp") -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise ValidationError(
            f"{context}: must be RFC 3339 with seconds and an explicit offset, or null"
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"{context}: must be an actual RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{context}: must include an explicit offset")
    return parsed


def format_timestamp(value: Union[datetime, date, str]) -> str:
    """Format a timestamp for frontmatter, normalizing UTC to ``Z``.

    A string is parsed and returned in a normalized form; a date is rejected
    because lifecycle fields require a timestamp.
    """

    if isinstance(value, str):
        parsed = parse_timestamp(value)
        assert parsed is not None
    elif isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            raise ValidationError("timestamp: datetime must be timezone-aware")
    else:
        raise ValidationError("timestamp: expected RFC 3339 string or datetime")
    # Keep second precision unless a caller explicitly supplies microseconds.
    text = parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def _validate_slug(value: Any, context: str, *, maximum: int = 64) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or not ID_RE.fullmatch(value):
        raise ValidationError(f"{context}: must be a lowercase ASCII slug")
    return value


def _validate_task_id(value: Any, context: str = "id") -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value != _nfc(value):
        raise ValidationError(f"{context}: must be a stable task identifier")
    # The timestamp-slug form is the default identity format.  Keep accepting
    # a plain lowercase slug for imported records; it remains immutable once
    # created.  ``T`` is intentionally uppercase in the timestamp prefix.
    match = TASK_ID_RE.fullmatch(value)
    if match:
        try:
            datetime.strptime(match.group("prefix"), "%Y%m%dT%H%M%S")
        except ValueError as exc:
            raise ValidationError(f"{context}: invalid timestamp prefix") from exc
    elif not ID_RE.fullmatch(value):
        raise ValidationError(f"{context}: must be a lowercase ASCII slug or timestamp-slug")
    return value


def _validate_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value != _nfc(value):
        raise ValidationError(f"{context}: must be a stable ASCII identifier")
    if ID_RE.fullmatch(value):
        return value
    timestamp_match = TASK_ID_RE.fullmatch(value)
    if timestamp_match:
        try:
            datetime.strptime(timestamp_match.group("prefix"), "%Y%m%dT%H%M%S")
        except ValueError as exc:
            raise ValidationError(f"{context}: invalid timestamp prefix") from exc
        return value
    raise ValidationError(f"{context}: must be a lowercase slug or timestamp-slug")


def _validate_key(value: Any, context: str, *, maximum: int = 64) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(f"{context}: must be a non-empty key")
    if value != value.lower() or not ID_RE.fullmatch(value):
        raise ValidationError(f"{context}: must be a lowercase ASCII slug")
    return value


def _validate_string_list(value: Any, context: str, *, maximum: int = 64) -> List[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{context}: must be a list")
    result: List[str] = []
    for index, item in enumerate(value):
        item_context = f"{context}[{index}]"
        item = _single_line(item, item_context, maximum=maximum)
        if item in result:
            raise ValidationError(f"{item_context}: duplicate value")
        result.append(item)
    return result


def _validate_choice(value: Any, allowed: Iterable[str], context: str) -> str:
    options = frozenset(allowed)
    if not isinstance(value, str) or value not in options:
        raise ValidationError(f"{context}: unsupported value {value!r}")
    return value


def _validate_ordered_timestamps(data: Mapping[str, Any], context: str) -> None:
    names = ("created_at", "started_at", "closed_at", "archived_at", "updated_at")
    parsed: Dict[str, Optional[datetime]] = {
        name: parse_timestamp(data.get(name), f"{context}.{name}") for name in names
    }
    created = parsed["created_at"]
    updated = parsed["updated_at"]
    if created is not None and updated is not None and updated < created:
        raise ValidationError(f"{context}.updated_at: cannot precede created_at")
    for earlier_name in ("started_at", "closed_at", "archived_at"):
        earlier = parsed[earlier_name]
        if earlier is not None and created is not None and earlier < created:
            raise ValidationError(f"{context}.{earlier_name}: cannot precede created_at")
        if earlier is not None and updated is not None and earlier > updated:
            raise ValidationError(f"{context}.updated_at: must be at or after {earlier_name}")
    closed = parsed["closed_at"]
    archived = parsed["archived_at"]
    if closed is not None and archived is not None and archived < closed:
        raise ValidationError(f"{context}.archived_at: cannot precede closed_at")


# ---------------------------------------------------------------------------
# Safe YAML frontmatter subset
# ---------------------------------------------------------------------------


def _json_loads_safe(value: str, context: str) -> Any:
    """Decode JSON while rejecting duplicate object keys and non-finite nums."""

    def pairs(items: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise FrontmatterError(f"{context}: duplicate flow-map key {key!r}")
            result[key] = item
        return result

    def reject_constant(token: str) -> Any:
        raise FrontmatterError(f"{context}: non-finite JSON number {token!r} is not allowed")

    try:
        return json.loads(
            value,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except FrontmatterError:
        raise
    except json.JSONDecodeError:
        raise


def _split_lines(text: str) -> Tuple[List[str], str]:
    """Return lines including endings and the dominant line ending."""

    lines = text.splitlines(keepends=True)
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    return lines, newline


def _parse_flow_or_scalar(raw: str, context: str) -> Any:
    """Parse a safe scalar/flow value.

    JSON is a strict subset of YAML and gives us deterministic handling for
    lists/maps.  Plain YAML strings are accepted for human-readable fields.
    Dangerous YAML features are rejected before parsing.
    """

    value = raw.strip()
    if not value:
        return None
    if value.startswith(("&", "*", "!")) or value in {"<<", "---", "..."}:
        raise FrontmatterError(f"{context}: anchors, aliases and tags are not allowed")
    if value.startswith("!!") or value.startswith("{") and not value.endswith("}"):
        raise FrontmatterError(f"{context}: malformed or tagged YAML value")
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value.startswith("[") or value.startswith("{"):
        try:
            return _json_loads_safe(value, context)
        except json.JSONDecodeError:
            # A small fallback for ordinary YAML flow lists (single quotes or
            # unquoted slugs), while still refusing arbitrary object syntax.
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                if not inner:
                    return []
                parts = _split_flow_items(inner, context)
                return [_parse_flow_or_scalar(part, context) for part in parts]
            if value.startswith("{") and value.endswith("}"):
                inner = value[1:-1].strip()
                if not inner:
                    return {}
                result_map: Dict[str, Any] = {}
                for part_index, part in enumerate(_split_flow_items(inner, context)):
                    key_raw, separator, item_raw = part.partition(":")
                    if not separator:
                        raise FrontmatterError(f"{context}: flow-map item lacks ':'")
                    key_raw = key_raw.strip()
                    if key_raw.startswith(('"', "'")):
                        key = _parse_flow_or_scalar(key_raw, f"{context}.key[{part_index}]")
                    else:
                        key = key_raw
                    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
                        raise FrontmatterError(f"{context}: invalid flow-map key {key!r}")
                    if key in result_map:
                        raise FrontmatterError(f"{context}: duplicate flow-map key {key!r}")
                    result_map[key] = _parse_flow_or_scalar(item_raw, f"{context}.{key}")
                return result_map
            raise FrontmatterError(f"{context}: invalid JSON/YAML flow value")
    if value.startswith(("\"", "'")):
        if value.startswith('"'):
            try:
                parsed = _json_loads_safe(value, context)
            except json.JSONDecodeError as exc:
                raise FrontmatterError(f"{context}: invalid quoted string") from exc
            if not isinstance(parsed, str):
                raise FrontmatterError(f"{context}: quoted scalar must be a string")
            return parsed
        # YAML's single quote escape is two consecutive single quotes.
        if len(value) < 2 or not value.endswith("'"):
            raise FrontmatterError(f"{context}: invalid single-quoted string")
        return value[1:-1].replace("''", "'")
    # Reject explicit YAML tags and block indicators.  A '#' is retained as a
    # literal in plain values; this avoids surprising title truncation.
    if value.startswith(("|", ">", ">-", "|-")):
        raise FrontmatterError(f"{context}: block scalar syntax is not supported")
    # YAML integer/float values are useful for schema_version and optional
    # extension properties.  Keep dates and timestamps as strings.
    if re.fullmatch(r"[-+]?[0-9]+", value):
        try:
            return int(value)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?", value):
        try:
            return float(value)
        except ValueError:
            pass
    if not _nfc(value) == value:
        raise FrontmatterError(f"{context}: plain string must use Unicode NFC")
    return value


def _split_flow_items(value: str, context: str) -> List[str]:
    parts: List[str] = []
    start = 0
    quote: Optional[str] = None
    escaped = False
    depth = 0
    for index, char in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            continue
        if quote == "'":
            if char == "'":
                # Two single quotes represent one quote; skip the second in a
                # best-effort way.  Full validity is checked by scalar parser.
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise FrontmatterError(f"{context}: unbalanced flow value")
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    if quote is not None or depth != 0:
        raise FrontmatterError(f"{context}: unbalanced quotes/brackets")
    parts.append(value[start:].strip())
    if any(not part for part in parts):
        raise FrontmatterError(f"{context}: empty flow-list item")
    return parts


def _parse_block_value(lines: Sequence[str], start: int, context: str) -> Tuple[Any, int]:
    """Parse an indented YAML list/map following a top-level empty value."""

    if start >= len(lines):
        return None, start
    # Consume only lines indented by at least two spaces.  Blank lines belong
    # to the block but do not affect its value.
    block: List[str] = []
    index = start
    while index < len(lines):
        raw = lines[index]
        line = raw.rstrip("\r\n")
        if line == "---":
            break
        if not line.strip():
            block.append("")
            index += 1
            continue
        if not line.startswith(("  ", "\t")):
            break
        if line.startswith("\t"):
            raise FrontmatterError(f"{context}: tabs are not allowed for indentation")
        block.append(line[2:])
        index += 1
    meaningful = [line for line in block if line]
    if not meaningful:
        return None, index
    if all(line.startswith("- ") or line == "-" for line in meaningful):
        result: List[Any] = []
        for item_index, line in enumerate(meaningful):
            raw_item = line[1:].strip()
            result.append(_parse_flow_or_scalar(raw_item, f"{context}[{item_index}]"))
        return result, index
    # Simple nested mapping.  Nested values may themselves be flow values but
    # are intentionally not recursively indented beyond one level.
    result_map: Dict[str, Any] = {}
    for line_index, line in enumerate(meaningful):
        key, separator, raw = line.partition(":")
        if not separator or not KEY_RE.fullmatch(key.strip()):
            raise FrontmatterError(f"{context}: unsupported nested YAML syntax")
        key = key.strip()
        if key in result_map:
            raise FrontmatterError(f"{context}: duplicate nested key {key!r}")
        result_map[key] = _parse_flow_or_scalar(raw.strip(), f"{context}.{key}")
    return result_map, index


@dataclass(frozen=True)
class FrontmatterDocument:
    """Parsed frontmatter plus the exact original text.

    ``raw_text`` is retained so callers can perform surgical CAS updates.  The
    ``body`` starts immediately after the closing delimiter; it is not
    normalized or stripped.
    """

    fields: Mapping[str, Any]
    body: str
    raw_text: str
    line_ending: str = "\n"
    key_lines: Mapping[str, int] = field(default_factory=dict)
    closing_line: int = 0

    @property
    def raw_bytes(self) -> bytes:
        return self.raw_text.encode("utf-8")

    @property
    def digest(self) -> str:
        return sha256_bytes(self.raw_bytes)

    def with_fields(self, fields: Mapping[str, Any]) -> "FrontmatterDocument":
        return FrontmatterDocument(
            fields=dict(fields),
            body=self.body,
            raw_text=render_frontmatter(fields, self.body),
            line_ending="\n",
            key_lines={},
            closing_line=0,
        )


def parse_frontmatter_bytes(content: bytes, context: str = "note.md") -> FrontmatterDocument:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrontmatterError(f"{context}: must be UTF-8") from exc
    lines, newline = _split_lines(text)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise FrontmatterError(f"{context}: first line must be ---")
    fields: Dict[str, Any] = {}
    key_lines: Dict[str, int] = {}
    closing_line: Optional[int] = None
    index = 1
    while index < len(lines):
        top_line_index = index
        raw_line = lines[index]
        line = raw_line.rstrip("\r\n")
        if line == "---":
            closing_line = index
            break
        if not line.strip():
            index += 1
            continue
        if line.startswith((" ", "\t")):
            raise FrontmatterError(f"{context}: line {index + 1} is unexpectedly indented")
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$", line)
        if not match:
            raise FrontmatterError(f"{context}: line {index + 1} must be key: value")
        key = match.group(1)
        if key in fields:
            raise FrontmatterError(f"{context}: duplicate frontmatter key {key!r}")
        raw_value = match.group(2)
        if raw_value and not raw_value.startswith((" ", "\t")):
            # ``key:value`` is legal YAML but disallowed here to avoid
            # accidental parsing of prose as frontmatter.
            raise FrontmatterError(f"{context}: line {index + 1} requires a space after ':'")
        value_text = raw_value.strip()
        if value_text:
            value = _parse_flow_or_scalar(value_text, f"{context}:{index + 1}")
            index += 1
        else:
            value, index = _parse_block_value(lines, index + 1, f"{context}:{index + 1}")
        fields[key] = value
        key_lines[key] = top_line_index
    if closing_line is None:
        raise FrontmatterError(f"{context}: frontmatter has no closing ---")
    # ``index`` points at the delimiter; for a block it may have advanced over
    # continuation lines, while ``closing_line`` remains authoritative.
    body = "".join(lines[closing_line + 1 :])
    return FrontmatterDocument(
        fields=fields,
        body=body,
        raw_text=text,
        line_ending=newline,
        key_lines=key_lines,
        closing_line=closing_line,
    )


def parse_frontmatter(text: str, context: str = "note.md") -> FrontmatterDocument:
    return parse_frontmatter_bytes(text.encode("utf-8"), context)


def _plain_safe_string(value: str) -> bool:
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        return False
    if value.lower() in {"null", "true", "false", "yes", "no", "on", "off", "~"}:
        return False
    if re.fullmatch(r"[-+]?[0-9]+(?:\.[0-9]+)?", value):
        return False
    if value.startswith(('-', '?', ':', '!', '&', '*', '#', '{', '}', '[', ']', ',', '|', '>', '@', '`', '"', "'")):
        return False
    if ": " in value or " #" in value:
        return False
    return value == _nfc(value)


def _dump_value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        return value if _plain_safe_string(value) else json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=isinstance(value, dict))
    raise FrontmatterError(f"cannot render unsupported frontmatter value {type(value).__name__}")


def render_frontmatter(fields: Mapping[str, Any], body: str = "") -> str:
    """Render a deterministic note with standard YAML delimiters."""

    if not isinstance(fields, Mapping):
        raise FrontmatterError("fields must be a mapping")
    lines = ["---"]
    for key, value in fields.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise FrontmatterError(f"invalid frontmatter key {key!r}")
        lines.append(f"{key}: {_dump_value(value)}")
    lines.append("---")
    rendered = "\n".join(lines) + "\n"
    if body:
        rendered += body
    return rendered


def _surgical_render(doc: FrontmatterDocument, updates: Mapping[str, Any]) -> bytes:
    """Patch one-line frontmatter values while preserving all other bytes."""

    text = doc.raw_text
    lines, newline = _split_lines(text)
    # Recompute key line positions robustly; this avoids relying on block-list
    # bookkeeping in the parser.
    top_keys: Dict[str, int] = {}
    for index in range(1, doc.closing_line):
        line = lines[index].rstrip("\r\n")
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$", line)
        if match:
            top_keys[match.group(1)] = index
    missing: List[Tuple[str, Any]] = []
    for key, value in updates.items():
        dumped = _dump_value(value)
        if key in top_keys:
            line_index = top_keys[key]
            old_line = lines[line_index]
            ending = "\r\n" if old_line.endswith("\r\n") else "\n" if old_line.endswith("\n") else ""
            # Reject a block value: replacing only its header would leave stale
            # continuation lines and change its meaning.
            if line_index + 1 < doc.closing_line:
                continuation = lines[line_index + 1].rstrip("\r\n")
                if continuation.startswith(("  ", "\t")):
                    raise FrontmatterError(f"cannot CAS-update block field {key!r}; rewrite explicitly")
            lines[line_index] = f"{key}: {dumped}{ending}"
        else:
            missing.append((key, value))
    if missing:
        insert_at = doc.closing_line
        ending = newline
        lines[insert_at:insert_at] = [f"{key}: {_dump_value(value)}{ending}" for key, value in missing]
    return "".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Entity records and validation
# ---------------------------------------------------------------------------


RecordT = TypeVar("RecordT", bound="MarkdownRecord")


@dataclass
class MarkdownRecord(Mapping[str, Any]):
    """Base immutable-ish record wrapper around frontmatter and Markdown body."""

    fields: Dict[str, Any]
    body: str = ""
    _raw_bytes: Optional[bytes] = field(default=None, repr=False, compare=False)
    _document: Optional[FrontmatterDocument] = field(default=None, repr=False, compare=False)

    KIND: ClassVar[str] = ""

    def __post_init__(self) -> None:
        self.fields = copy.deepcopy(dict(self.fields))
        if not isinstance(self.body, str):
            raise TypeError("body must be text")
        self.validate()

    # Mapping-like access keeps adapters (views, CLI serializers and Obsidian
    # integrations) simple while retaining the richer record object.
    def __getitem__(self, key: str) -> Any:
        return self.fields[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def keys(self):
        return self.fields.keys()

    def items(self):
        return self.fields.items()

    def values(self):
        return self.fields.values()

    def __iter__(self):
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    @classmethod
    def from_bytes(cls: Type[RecordT], content: bytes, context: str = "note.md") -> RecordT:
        doc = parse_frontmatter_bytes(content, context)
        record = cls(dict(doc.fields), doc.body, bytes(content), doc)
        return record

    @classmethod
    def from_text(cls: Type[RecordT], text: str, context: str = "note.md") -> RecordT:
        return cls.from_bytes(text.encode("utf-8"), context)

    @classmethod
    def from_fields(cls: Type[RecordT], fields: Mapping[str, Any], body: str = "") -> RecordT:
        return cls(dict(fields), body)

    def validate(self) -> None:
        validate_record(self.fields, expected_kind=self.KIND or None)

    @property
    def kind(self) -> str:
        return str(self.fields.get("kind", self.KIND))

    @property
    def record_id(self) -> str:
        if self.kind == "artifact":
            return str(self.fields.get("artifact_id", self.fields.get("id", "")))
        if self.kind == "capture":
            return str(self.fields.get("capture_id", self.fields.get("id", "")))
        return str(self.fields.get("id", ""))

    @property
    def id(self) -> str:
        return self.record_id

    @property
    def digest(self) -> str:
        return sha256_bytes(self.to_bytes())

    @property
    def source_digest(self) -> Optional[str]:
        return sha256_bytes(self._raw_bytes) if self._raw_bytes is not None else None

    def to_text(self, *, canonical: bool = False) -> str:
        if not canonical and self._raw_bytes is not None:
            return self._raw_bytes.decode("utf-8")
        return render_frontmatter(self.fields, self.body)

    def to_bytes(self, *, canonical: bool = False) -> bytes:
        if not canonical and self._raw_bytes is not None:
            return bytes(self._raw_bytes)
        return self.to_text(canonical=True).encode("utf-8")

    def with_fields(self: RecordT, updates: Mapping[str, Any], *, canonical: bool = False) -> RecordT:
        merged = copy.deepcopy(self.fields)
        merged.update(copy.deepcopy(dict(updates)))
        if canonical:
            return type(self)(merged, self.body)
        # Preserve exact source bytes when no update is requested; otherwise
        # use canonical output (CAS callers use surgical rendering instead).
        return type(self)(merged, self.body)


@dataclass
class Task(MarkdownRecord):
    KIND: ClassVar[str] = "task"

    def validate(self) -> None:
        validate_task(self.fields)

    def transition(
        self,
        target_status: str,
        *,
        now: Optional[Union[str, datetime]] = None,
        closure_summary: Optional[str] = None,
        next_action: Optional[str] = None,
        waiting_on: Any = None,
        follow_up_on: Any = None,
    ) -> "Task":
        data = transition_task(
            self.fields,
            target_status,
            now=now,
            closure_summary=closure_summary,
            next_action=next_action,
            waiting_on=waiting_on,
            follow_up_on=follow_up_on,
        )
        return Task(data, self.body)


@dataclass
class Capture(MarkdownRecord):
    KIND: ClassVar[str] = "capture"

    def validate(self) -> None:
        validate_capture(self.fields)


@dataclass
class Artifact(MarkdownRecord):
    KIND: ClassVar[str] = "artifact"

    def validate(self) -> None:
        validate_artifact(self.fields)


def _require_fields(data: Mapping[str, Any], required: Iterable[str], context: str) -> None:
    missing = [key for key in required if key not in data]
    if missing:
        raise ValidationError(f"{context}: missing required field(s): {', '.join(sorted(missing))}")


def _check_kind_version(data: Mapping[str, Any], kind: str, context: str) -> None:
    if data.get("kind") != kind:
        raise ValidationError(f"{context}.kind: must be {kind!r}")
    version = data.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ValidationError(f"{context}.schema_version: must be {SCHEMA_VERSION}")


def validate_task(data: Mapping[str, Any], context: str = "task") -> None:
    if not isinstance(data, Mapping):
        raise ValidationError(f"{context}: expected a mapping")
    _require_fields(data, TASK_REQUIRED, context)
    _check_kind_version(data, "task", context)
    _validate_task_id(data["id"], f"{context}.id")
    _single_line(data["title"], f"{context}.title", maximum=240)
    _single_line(data["outcome"], f"{context}.outcome", maximum=1000)
    status = _validate_choice(data["status"], TASK_STATUSES, f"{context}.status")
    storage = _validate_choice(data["storage_state"], {"active", "archived"}, f"{context}.storage_state")
    _validate_key(data["area"], f"{context}.area")
    _validate_key(data["type"], f"{context}.type")
    _validate_choice(data["priority"], PRIORITIES, f"{context}.priority")
    parse_date(data["scheduled_on"], f"{context}.scheduled_on")
    parse_date(data["due_on"], f"{context}.due_on")
    parse_date(data["follow_up_on"], f"{context}.follow_up_on")
    _validate_choice(data["sensitivity"], SENSITIVITIES, f"{context}.sensitivity")
    for key in ("created_at", "updated_at", "started_at", "closed_at", "archived_at"):
        parse_timestamp(data[key], f"{context}.{key}")
    if data["created_at"] is None or data["updated_at"] is None:
        raise ValidationError(f"{context}: created_at and updated_at are required timestamps")
    _validate_ordered_timestamps(data, context)
    if status in OPEN_STATUSES:
        if data["storage_state"] != "active":
            raise ValidationError(f"{context}: open task cannot have storage_state archived")
        _single_line(data["next_action"], f"{context}.next_action", maximum=1000)
        if data["closed_at"] is not None or data["archived_at"] is not None or data["closure_summary"] is not None:
            raise ValidationError(f"{context}: open task cannot contain closure timestamps/summary")
    else:
        if data["closed_at"] is None:
            raise ValidationError(f"{context}.closed_at: required for a closed task")
        _single_line(data["closure_summary"], f"{context}.closure_summary", maximum=4000)
        if data["next_action"] is not None:
            raise ValidationError(f"{context}.next_action: must be null when closed")
    if storage == "archived":
        if status not in CLOSED_STATUSES:
            raise ValidationError(f"{context}: only closed tasks may be archived")
        if data["archived_at"] is None:
            raise ValidationError(f"{context}.archived_at: required when archived")
    elif data["archived_at"] is not None:
        raise ValidationError(f"{context}.archived_at: must be null for active storage")
    waiting_on = data["waiting_on"]
    if waiting_on is not None:
        _single_line(waiting_on, f"{context}.waiting_on", maximum=1000)
    _validate_string_list(data["tags"], f"{context}.tags", maximum=128)
    _validate_string_list(data["aliases"], f"{context}.aliases", maximum=240)


def validate_capture(data: Mapping[str, Any], context: str = "capture") -> None:
    if not isinstance(data, Mapping):
        raise ValidationError(f"{context}: expected a mapping")
    # ``id`` is accepted as a convenience alias, but canonical captures use
    # capture_id.  Do not mutate the caller's mapping.
    normalized = dict(data)
    if "capture_id" not in normalized and "id" in normalized:
        normalized["capture_id"] = normalized["id"]
    _require_fields(normalized, CAPTURE_REQUIRED, context)
    _check_kind_version(normalized, "capture", context)
    _validate_id(normalized["capture_id"], f"{context}.capture_id")
    _single_line(normalized["title"], f"{context}.title", maximum=240)
    status = _validate_choice(normalized["status"], CAPTURE_STATUSES, f"{context}.status")
    _validate_choice(normalized["capture_type"], CAPTURE_TYPES, f"{context}.capture_type")
    # Text/link/email captures keep their original content in the Capture note
    # body and therefore do not need a separate payload.  A file capture must
    # point at the copied, workspace-relative source bytes; all other capture
    # types may either omit the payload or provide one when an integration has
    # materialized it.
    _validate_relative_path_or_null(normalized["payload_path"], f"{context}.payload_path")
    if normalized["capture_type"] == "file" and normalized["payload_path"] is None:
        raise ValidationError(f"{context}.payload_path: required for file captures")
    source = normalized["source"]
    if not isinstance(source, (str, Mapping)):
        raise ValidationError(f"{context}.source: must be text or a mapping")
    if isinstance(source, str):
        _single_line(source, f"{context}.source", maximum=2000)
    else:
        _validate_mapping_strings(source, f"{context}.source")
    _validate_choice(normalized["sensitivity"], SENSITIVITIES, f"{context}.sensitivity")
    captured = parse_timestamp(normalized["captured_at"], f"{context}.captured_at")
    updated = parse_timestamp(normalized["updated_at"], f"{context}.updated_at")
    triaged = parse_timestamp(normalized["triaged_at"], f"{context}.triaged_at")
    if captured is None or updated is None:
        raise ValidationError(f"{context}: captured_at and updated_at are required timestamps")
    if updated < captured:
        raise ValidationError(f"{context}.updated_at: cannot precede captured_at")
    if triaged is not None and (triaged < captured or triaged > updated):
        raise ValidationError(f"{context}.triaged_at: must be between captured_at and updated_at")
    if status == "triaged":
        if normalized["triaged_at"] is None or normalized["disposition"] is None:
            raise ValidationError(f"{context}: triaged capture requires triaged_at and disposition")
    elif status == "deferred":
        if normalized["triaged_at"] is not None:
            raise ValidationError(f"{context}.triaged_at: deferred captures are not fully triaged")
        if normalized["disposition"] not in {None, "defer"}:
            raise ValidationError(f"{context}.disposition: deferred captures require defer")
    else:
        if normalized["triaged_at"] is not None or normalized["disposition"] is not None:
            raise ValidationError(f"{context}: inbox captures cannot contain triage results")
    disposition = normalized["disposition"]
    if disposition is not None:
        _validate_choice(disposition, CAPTURE_DISPOSITIONS, f"{context}.disposition")
    target = normalized["target_task"]
    if target is not None:
        _validate_task_id(target, f"{context}.target_task")
    if disposition in {"task", "artifact"} and target is None:
        raise ValidationError(f"{context}.target_task: required for {disposition} disposition")
    if disposition in {"library", "defer"} and target is not None:
        raise ValidationError(f"{context}.target_task: must be null for {disposition} disposition")
    if disposition == "defer" and status != "deferred":
        raise ValidationError(f"{context}: defer disposition requires status deferred")
    if status == "deferred" and disposition not in {None, "defer"}:
        raise ValidationError(f"{context}: deferred capture cannot have disposition {disposition!r}")
    _validate_string_list(normalized["tags"], f"{context}.tags", maximum=128)


def _validate_mapping_strings(value: Mapping[Any, Any], context: str) -> None:
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValidationError(f"{context}: keys must be strings")
        _single_line(key, f"{context}.{key}", maximum=128)
        if not isinstance(item, (str, int, float, bool)) and item is not None:
            raise ValidationError(f"{context}.{key}: unsupported nested value")
        if isinstance(item, str):
            _single_line(item, f"{context}.{key}", maximum=2000)


def _validate_relative_path_or_null(value: Any, context: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != _nfc(value):
        raise ValidationError(f"{context}: must be a normalized relative POSIX path or null")
    if value.startswith("/") or "\\" in value or "\x00" in value or "//" in value:
        raise ValidationError(f"{context}: must be a normalized relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"{context}: must not contain . or .. segments")
    return value


def validate_artifact(data: Mapping[str, Any], context: str = "artifact") -> None:
    if not isinstance(data, Mapping):
        raise ValidationError(f"{context}: expected a mapping")
    normalized = dict(data)
    if "artifact_id" not in normalized and "id" in normalized:
        normalized["artifact_id"] = normalized["id"]
    _require_fields(normalized, ARTIFACT_REQUIRED, context)
    _check_kind_version(normalized, "artifact", context)
    _validate_id(normalized["artifact_id"], f"{context}.artifact_id")
    _validate_relative_path_or_null(normalized["payload_path"], f"{context}.payload_path")
    owner = normalized["owner_task"]
    if owner is not None:
        _validate_task_id(owner, f"{context}.owner_task")
    role = normalized["role"]
    role = _validate_choice(role, ARTIFACT_ROLES, f"{context}.role")
    task_roles = {"input", "inputs", "work", "deliverable", "deliverables", "record", "records", "history"}
    if role in task_roles and owner is None:
        raise ValidationError(f"{context}.owner_task: required for task-owned role {role!r}")
    if role in {"library", "inbox"} and owner is not None:
        raise ValidationError(f"{context}.owner_task: must be null for role {role!r}")
    _validate_choice(normalized["sensitivity"], SENSITIVITIES, f"{context}.sensitivity")
    provenance = normalized["provenance"]
    if provenance is None or provenance == "":
        raise ValidationError(f"{context}.provenance: required")
    if isinstance(provenance, str):
        _single_line(provenance, f"{context}.provenance", maximum=4000)
    elif isinstance(provenance, Mapping):
        _validate_mapping_strings(provenance, f"{context}.provenance")
    else:
        raise ValidationError(f"{context}.provenance: must be text or a mapping")
    digest = normalized["sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ValidationError(f"{context}.sha256: must be a lowercase SHA-256 digest")
    derived = normalized["derived_from"]
    if derived is not None:
        if isinstance(derived, str):
            _validate_id(derived, f"{context}.derived_from")
        elif isinstance(derived, list):
            seen_derived: set[str] = set()
            for index, item in enumerate(derived):
                derived_id = _validate_id(item, f"{context}.derived_from[{index}]")
                if derived_id in seen_derived:
                    raise ValidationError(f"{context}.derived_from[{index}]: duplicate artifact id")
                seen_derived.add(derived_id)
        else:
            raise ValidationError(f"{context}.derived_from: must be null, id, or list of ids")
    if parse_timestamp(normalized["created_at"], f"{context}.created_at") is None:
        raise ValidationError(f"{context}.created_at: required")
    if "updated_at" in normalized:
        parse_timestamp(normalized["updated_at"], f"{context}.updated_at")


def validate_record(data: Mapping[str, Any], *, expected_kind: Optional[str] = None) -> None:
    kind = data.get("kind")
    if expected_kind and kind != expected_kind:
        raise ValidationError(f"record.kind: expected {expected_kind!r}, got {kind!r}")
    if kind == "task":
        validate_task(data)
    elif kind == "capture":
        validate_capture(data)
    elif kind == "artifact":
        validate_artifact(data)
    else:
        raise ValidationError(f"record.kind: unsupported kind {kind!r}")


def parse_record_bytes(content: bytes, context: str = "note.md") -> MarkdownRecord:
    doc = parse_frontmatter_bytes(content, context)
    kind = doc.fields.get("kind")
    cls: Type[MarkdownRecord]
    if kind == "task":
        cls = Task
    elif kind == "capture":
        cls = Capture
    elif kind == "artifact":
        cls = Artifact
    else:
        raise ValidationError(f"{context}.kind: unsupported kind {kind!r}")
    return cls(dict(doc.fields), doc.body, bytes(content), doc)


def parse_record(path_or_bytes: Union[Path, str, bytes], context: Optional[str] = None) -> MarkdownRecord:
    if isinstance(path_or_bytes, Path):
        content = path_or_bytes.read_bytes()
        return parse_record_bytes(content, context or str(path_or_bytes))
    if isinstance(path_or_bytes, bytes):
        return parse_record_bytes(path_or_bytes, context or "note.md")
    # A string containing frontmatter is treated as text; callers wanting a
    # filesystem path should pass Path explicitly to avoid ambiguity.
    return parse_record_bytes(path_or_bytes.encode("utf-8"), context or "note.md")


def parse_note_bytes(
    content: bytes,
    context: str = "note.md",
    *,
    expected_kind: Optional[str] = None,
) -> MarkdownRecord:
    """Public adapter name for parsing any clean-slate note."""

    record = parse_record_bytes(content, context)
    if expected_kind is not None and record.kind != expected_kind:
        raise ValidationError(f"{context}.kind: expected {expected_kind!r}")
    return record


def render_note_bytes(
    note: Union[MarkdownRecord, Mapping[str, Any]],
    body: Optional[str] = None,
    *,
    canonical: bool = True,
) -> bytes:
    """Render a record or field mapping to UTF-8 Markdown bytes."""

    if isinstance(note, MarkdownRecord):
        return note.to_bytes(canonical=canonical if canonical else False)
    return render_frontmatter(note, body or "").encode("utf-8")


# ---------------------------------------------------------------------------
# Task lifecycle and archive/storage transitions
# ---------------------------------------------------------------------------


def _coerce_now(now: Optional[Union[str, datetime]]) -> str:
    if now is None:
        return format_timestamp(datetime.now(timezone.utc))
    return format_timestamp(now)


def _require_advance(old: Optional[str], new: str, context: str) -> None:
    if old is None:
        return
    old_dt = parse_timestamp(old, f"{context}.old")
    new_dt = parse_timestamp(new, f"{context}.new")
    assert old_dt is not None and new_dt is not None
    if new_dt <= old_dt:
        raise StateTransitionError(f"{context}: timestamp must advance beyond updated_at")


def transition_task(
    task: Union[Task, Mapping[str, Any]],
    target_status: str,
    *,
    now: Optional[Union[str, datetime]] = None,
    closure_summary: Optional[str] = None,
    next_action: Optional[str] = None,
    waiting_on: Any = None,
    follow_up_on: Any = None,
) -> Dict[str, Any]:
    """Return a validated copy of *task* after a legal status transition.

    ``target_status`` never accepts ``archived``; use :func:`archive_task` for
    storage transitions.  Re-opening a closed task requires a new
    ``next_action`` argument so a closed note can never silently become an
    actionless open task.
    """

    data = copy.deepcopy(task.fields if isinstance(task, MarkdownRecord) else dict(task))
    validate_task(data)
    old_status = data["status"]
    if target_status not in TASK_STATUSES:
        raise StateTransitionError(f"unsupported target status {target_status!r}")
    if target_status == old_status:
        raise StateTransitionError(f"task {data['id']}: already in status {old_status!r}")
    if data["storage_state"] != "active":
        raise StateTransitionError("archived task must be restored before a status transition")
    allowed = TRANSITIONS.get(old_status, frozenset())
    if target_status not in allowed:
        raise StateTransitionError(f"task {data['id']}: {old_status} -> {target_status} is not allowed")
    timestamp = _coerce_now(now)
    _require_advance(data.get("updated_at"), timestamp, f"task {data['id']}")
    data["status"] = target_status
    data["updated_at"] = timestamp
    if target_status == "active" and old_status == "planned" and data.get("started_at") is None:
        data["started_at"] = timestamp
    if target_status in CLOSED_STATUSES:
        summary = closure_summary
        if summary is None:
            # A caller may supply an already prepared summary in the source
            # record when transitioning between closed states (not currently
            # allowed), but normal closure always requires explicit text.
            raise StateTransitionError("closing a task requires closure_summary")
        _single_line(summary, "closure_summary", maximum=4000)
        data["closure_summary"] = summary
        data["closed_at"] = timestamp
        data["next_action"] = None
        data["waiting_on"] = None
        data["follow_up_on"] = None
    elif old_status in CLOSED_STATUSES:
        action = next_action
        if action is None:
            raise StateTransitionError("re-opening a task requires next_action")
        _single_line(action, "next_action", maximum=1000)
        data["next_action"] = action
        data["closed_at"] = None
        data["closure_summary"] = None
        data["archived_at"] = None
    elif next_action is not None:
        _single_line(next_action, "next_action", maximum=1000)
        data["next_action"] = next_action
    if target_status == "waiting":
        if waiting_on is not None:
            _single_line(waiting_on, "waiting_on", maximum=1000)
            data["waiting_on"] = waiting_on
        if follow_up_on is not None:
            parse_date(follow_up_on, "follow_up_on")
            data["follow_up_on"] = follow_up_on
    elif target_status in {"active", "planned"}:
        # Explicit values can restore a waiting task's follow-up metadata;
        # otherwise clear stale waiting context when becoming actionable.
        if waiting_on is not None:
            _single_line(waiting_on, "waiting_on", maximum=1000)
            data["waiting_on"] = waiting_on
        elif old_status in {"waiting", "blocked"}:
            data["waiting_on"] = None
        if follow_up_on is not None:
            parse_date(follow_up_on, "follow_up_on")
            data["follow_up_on"] = follow_up_on
        elif old_status in {"waiting", "blocked"}:
            data["follow_up_on"] = None
    validate_task(data)
    return data


def archive_task(
    task: Union[Task, Mapping[str, Any]],
    *,
    now: Optional[Union[str, datetime]] = None,
) -> Dict[str, Any]:
    """Mark a closed active task as archived (without changing business status)."""

    data = copy.deepcopy(task.fields if isinstance(task, MarkdownRecord) else dict(task))
    validate_task(data)
    if data["storage_state"] != "active":
        raise StateTransitionError(f"task {data['id']}: already archived")
    if data["status"] not in CLOSED_STATUSES:
        raise StateTransitionError("only completed or cancelled tasks can be archived")
    timestamp = _coerce_now(now)
    _require_advance(data.get("updated_at"), timestamp, f"task {data['id']}")
    data["storage_state"] = "archived"
    data["archived_at"] = timestamp
    data["updated_at"] = timestamp
    validate_task(data)
    return data


def restore_task(
    task: Union[Task, Mapping[str, Any]],
    *,
    now: Optional[Union[str, datetime]] = None,
) -> Dict[str, Any]:
    """Restore an archived task to active storage while preserving closure state."""

    data = copy.deepcopy(task.fields if isinstance(task, MarkdownRecord) else dict(task))
    validate_task(data)
    if data["storage_state"] != "archived":
        raise StateTransitionError(f"task {data['id']}: is not archived")
    timestamp = _coerce_now(now)
    _require_advance(data.get("updated_at"), timestamp, f"task {data['id']}")
    data["storage_state"] = "active"
    data["archived_at"] = None
    data["updated_at"] = timestamp
    validate_task(data)
    return data


# ---------------------------------------------------------------------------
# CAS updates and event receipts
# ---------------------------------------------------------------------------


TASK_IMMUTABLE_FIELDS = frozenset({"kind", "schema_version", "id"})
CAPTURE_IMMUTABLE_FIELDS = frozenset({"kind", "schema_version", "capture_id", "id"})
ARTIFACT_IMMUTABLE_FIELDS = frozenset({"kind", "schema_version", "artifact_id", "id"})


def _record_class_for_data(data: Mapping[str, Any]) -> Type[MarkdownRecord]:
    kind = data.get("kind")
    if kind == "task":
        return Task
    if kind == "capture":
        return Capture
    if kind == "artifact":
        return Artifact
    raise ValidationError(f"record.kind: unsupported kind {kind!r}")


def _record_identity(data: Mapping[str, Any]) -> Tuple[str, str]:
    kind = str(data.get("kind"))
    if kind == "task":
        return kind, str(data.get("id"))
    if kind == "capture":
        return kind, str(data.get("capture_id", data.get("id")))
    return kind, str(data.get("artifact_id", data.get("id")))


@dataclass(frozen=True)
class EventReceipt:
    """Append-only evidence for one semantic edit."""

    event_id: str
    event_type: str
    entity_kind: str
    entity_id: str
    occurred_at: str
    actor: str
    result: str
    before_sha256: Optional[str] = None
    after_sha256: Optional[str] = None
    changed_fields: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        entity_kind: str,
        entity_id: str,
        actor: str = "agent",
        result: str = "applied",
        before_sha256: Optional[str] = None,
        after_sha256: Optional[str] = None,
        changed_fields: Iterable[str] = (),
        metadata: Optional[Mapping[str, Any]] = None,
        occurred_at: Optional[Union[str, datetime]] = None,
        event_id: Optional[str] = None,
    ) -> "EventReceipt":
        _single_line(event_type, "event_type", maximum=120)
        _single_line(entity_kind, "entity_kind", maximum=64)
        _single_line(entity_id, "entity_id", maximum=128)
        _single_line(actor, "actor", maximum=128)
        _single_line(result, "result", maximum=64)
        timestamp = _coerce_now(occurred_at)
        if before_sha256 is not None and not SHA256_RE.fullmatch(before_sha256):
            raise ModelError("before_sha256: invalid digest")
        if after_sha256 is not None and not SHA256_RE.fullmatch(after_sha256):
            raise ModelError("after_sha256: invalid digest")
        changed = tuple(sorted(set(str(item) for item in changed_fields)))
        for item in changed:
            if not KEY_RE.fullmatch(item):
                raise ModelError(f"changed_fields: invalid field name {item!r}")
        event_identifier = event_id or (timestamp.replace("-", "").replace(":", "") + "-" + secrets.token_hex(8))
        if not ID_RE.fullmatch(event_identifier):
            # Timestamp IDs contain a ``T`` and are valid under a looser event
            # identifier grammar; retain a safe ASCII representation.
            event_identifier = re.sub(r"[^A-Za-z0-9._-]", "-", event_identifier)
        return cls(
            event_id=event_identifier,
            event_type=event_type,
            entity_kind=entity_kind,
            entity_id=entity_id,
            occurred_at=timestamp,
            actor=actor,
            result=result,
            before_sha256=before_sha256,
            after_sha256=after_sha256,
            changed_fields=changed,
            metadata=copy.deepcopy(dict(metadata or {})),
        )

    @property
    def id(self) -> str:
        return self.event_id

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "entity_kind": self.entity_kind,
            "entity_id": self.entity_id,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "result": self.result,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "changed_fields": list(self.changed_fields),
        }
        if self.metadata:
            value["metadata"] = copy.deepcopy(dict(self.metadata))
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def append_to(self, path: Union[str, Path], *, fsync: bool = True) -> Path:
        return append_event(path, self, fsync=fsync)


# Friendly alias used by callers that call receipts "events".
Receipt = EventReceipt


@dataclass(frozen=True)
class CASUpdateResult:
    record: MarkdownRecord
    content: bytes
    before_sha256: Optional[str]
    after_sha256: str
    changed_fields: Tuple[str, ...]
    receipt: EventReceipt

    @property
    def bytes(self) -> bytes:
        return self.content

    @property
    def digest(self) -> str:
        return self.after_sha256


def cas_update(
    content: bytes,
    expected_sha256: str,
    updates: Mapping[str, Any],
    *,
    now: Optional[Union[str, datetime]] = None,
    actor: str = "agent",
    event_type: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    touch_updated_at: bool = True,
) -> CASUpdateResult:
    """Apply a field update only if *content* matches *expected_sha256*.

    The returned bytes are validated before being returned.  A digest mismatch
    raises :class:`CASConflict` and leaves the caller's bytes untouched.
    """

    if not isinstance(content, (bytes, bytearray)):
        raise TypeError("content must be bytes")
    content = bytes(content)
    actual = sha256_bytes(content)
    expected = str(expected_sha256).lower()
    if not SHA256_RE.fullmatch(expected):
        raise CASConflict(expected, actual, context="record")
    if actual != expected:
        raise CASConflict(expected, actual)
    record = parse_record_bytes(content)
    updates_dict = copy.deepcopy(dict(updates))
    kind, identity = _record_identity(record.fields)
    immutable = {
        "task": TASK_IMMUTABLE_FIELDS,
        "capture": CAPTURE_IMMUTABLE_FIELDS,
        "artifact": ARTIFACT_IMMUTABLE_FIELDS,
    }[kind]
    attempted_immutable = immutable.intersection(updates_dict)
    for field_name in attempted_immutable:
        if updates_dict[field_name] != record.fields.get(field_name):
            raise ImmutableFieldError(f"{kind} {identity}: field {field_name!r} is immutable")
        updates_dict.pop(field_name, None)
    if touch_updated_at and "updated_at" in record.fields and "updated_at" not in updates_dict:
        updates_dict["updated_at"] = _coerce_now(now)
    elif "updated_at" in updates_dict and updates_dict["updated_at"] is not None:
        updates_dict["updated_at"] = format_timestamp(updates_dict["updated_at"])
    # Avoid needless writes and permit deterministic no-op CAS operations.
    changed = tuple(sorted(key for key, value in updates_dict.items() if record.fields.get(key) != value))
    if not changed:
        receipt = EventReceipt.create(
            event_type=event_type or f"{kind}.update",
            entity_kind=kind,
            entity_id=identity,
            actor=actor,
            result="noop",
            before_sha256=actual,
            after_sha256=actual,
            changed_fields=(),
            metadata=metadata,
            occurred_at=now,
        )
        return CASUpdateResult(record, content, actual, actual, (), receipt)
    # Only rewrite values that actually changed.  In particular, an unknown
    # Obsidian property may use a multiline block representation; passing an
    # unchanged copy of it to the surgical renderer would unnecessarily reject
    # a perfectly safe CAS update (or normalize the user's spelling).
    effective_updates = {key: updates_dict[key] for key in changed}
    patched = _surgical_render(record._document or parse_frontmatter_bytes(content), effective_updates)
    updated_record = parse_record_bytes(patched)
    after = sha256_bytes(patched)
    receipt = EventReceipt.create(
        event_type=event_type or f"{kind}.update",
        entity_kind=kind,
        entity_id=identity,
        actor=actor,
        result="applied",
        before_sha256=actual,
        after_sha256=after,
        changed_fields=changed,
        metadata=metadata,
        occurred_at=now,
    )
    return CASUpdateResult(updated_record, patched, actual, after, changed, receipt)


def cas_update_path(
    path: Union[str, Path],
    expected_sha256: str,
    updates: Mapping[str, Any],
    **kwargs: Any,
) -> CASUpdateResult:
    """Read a note, perform :func:`cas_update`, and atomically replace it.

    This helper is intentionally narrow: it refuses symlinks and uses a temp
    sibling plus ``os.replace``.  Higher-level structural operations still need
    preview/approval; a field update is a semantic edit and may call this
    helper directly once the target is unambiguous.
    """

    target = Path(path)
    if target.is_symlink():
        raise ModelError(f"{target}: symlink targets are not writable")
    content = target.read_bytes()
    result = cas_update(content, expected_sha256, updates, **kwargs)
    # Re-check the source immediately before publishing the replacement.  A
    # digest read followed by an unconditional rename would otherwise allow a
    # concurrent Obsidian save to be silently overwritten.
    _atomic_write_bytes(target, result.content, expected_sha256=expected_sha256)
    return result


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    expected_sha256: Optional[str] = None,
) -> None:
    if path.is_symlink():
        raise ModelError(f"{path}: symlink targets are not writable")
    if expected_sha256 is None and path.exists():
        raise ModelError(f"{path}: destination already exists")
    if expected_sha256 is not None:
        current = sha256_file(path)
        if current != expected_sha256:
            raise CASConflict(expected_sha256, current, context=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.write-{secrets.token_hex(6)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_sha256 is not None:
            if path.is_symlink():
                raise ModelError(f"{path}: target became a symlink during CAS save")
            current = sha256_file(path)
            if current != expected_sha256:
                raise CASConflict(expected_sha256, current, context=str(path))
        elif path.exists():
            # A creator raced us after the initial existence check.
            raise CASConflict("<absent>", sha256_file(path), context=str(path))
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_task_note(path: Union[str, Path]) -> Task:
    """Load and validate a canonical Task note from *path*."""

    target = Path(path)
    if target.is_symlink():
        raise ModelError(f"{target}: symlink task notes are not trusted")
    return Task.from_bytes(target.read_bytes(), str(target))


def save_task_note(
    path: Union[str, Path],
    task: Union[Task, Mapping[str, Any]],
    expected_sha256: Optional[str] = None,
    *,
    actor: str = "agent",
    now: Optional[Union[str, datetime]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> CASUpdateResult:
    """Create or CAS-save a Task note atomically.

    Existing notes require an exact ``expected_sha256`` (or the source digest
    carried by a :class:`Task` loaded with :func:`load_task_note`).  New notes
    are created from canonical frontmatter.  The result always contains a
    receipt; callers may append it to ``.workspace-organizer/events.jsonl``.
    """

    target = Path(path)
    record = task if isinstance(task, Task) else Task.from_fields(task)
    desired = record.fields
    if target.exists():
        if target.is_symlink():
            raise ModelError(f"{target}: symlink task notes are not writable")
        original = target.read_bytes()
        actual = sha256_bytes(original)
        expected = expected_sha256 or record.source_digest
        if expected is None:
            raise CASConflict("<required>", actual, context=str(target))
        result = cas_update(
            original,
            expected,
            desired,
            actor=actor,
            now=now,
            metadata=metadata,
            touch_updated_at=False,
            event_type="task.save",
        )
        # ``Task.body`` is canonical state too.  CAS's surgical update keeps
        # the old body, so replace only the body when the caller supplied one.
        old_record = parse_record_bytes(original, str(target))
        if record.body != old_record.body:
            parsed = parse_frontmatter_bytes(result.content, str(target))
            lines, _ = _split_lines(parsed.raw_text)
            prefix = "".join(lines[: parsed.closing_line + 1])
            if not prefix.endswith(("\n", "\r\n")):
                prefix += parsed.line_ending
            final_content = (prefix + record.body).encode("utf-8")
            final_record = Task.from_bytes(final_content, str(target))
            final_digest = sha256_bytes(final_content)
            changed = tuple(sorted(set(result.changed_fields) | {"body"}))
            receipt = EventReceipt.create(
                event_type="task.save",
                entity_kind="task",
                entity_id=record.record_id,
                actor=actor,
                result="applied",
                before_sha256=actual,
                after_sha256=final_digest,
                changed_fields=changed,
                metadata=metadata,
                occurred_at=now,
            )
            result = CASUpdateResult(final_record, final_content, actual, final_digest, changed, receipt)
        _atomic_write_bytes(target, result.content, expected_sha256=expected)
        return result
    content = render_frontmatter(desired, record.body).encode("utf-8")
    created = Task.from_bytes(content, str(target))
    digest = sha256_bytes(content)
    receipt = EventReceipt.create(
        event_type="task.create",
        entity_kind="task",
        entity_id=created.record_id,
        actor=actor,
        result="created",
        before_sha256=None,
        after_sha256=digest,
        changed_fields=tuple(sorted(desired.keys())),
        metadata=metadata,
        occurred_at=now,
    )
    _atomic_write_bytes(target, content)
    return CASUpdateResult(created, content, None, digest, tuple(sorted(desired.keys())), receipt)


def append_event(path: Union[str, Path], receipt: EventReceipt, *, fsync: bool = True) -> Path:
    """Append one JSON receipt line, creating the parent directory if needed."""

    target = Path(path)
    if target.is_symlink():
        raise ModelError(f"{target}: symlink event log is not writable")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(receipt.to_json())
        stream.write("\n")
        stream.flush()
        if fsync:
            os.fsync(stream.fileno())
    return target


def event_receipt(**kwargs: Any) -> EventReceipt:
    """Convenience factory for adapters that prefer a function API."""

    return EventReceipt.create(**kwargs)


make_event_receipt = event_receipt


def read_events(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Read and validate JSONL receipts; malformed lines fail closed."""

    target = Path(path)
    if not target.exists():
        return []
    if target.is_symlink():
        raise ModelError(f"{target}: symlink event log is not trusted")
    events: List[Dict[str, Any]] = []
    for index, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ModelError(f"{target}:{index}: invalid JSON receipt") from exc
        if not isinstance(value, dict) or not value.get("event_id") or not value.get("event_type"):
            raise ModelError(f"{target}:{index}: malformed receipt")
        events.append(value)
    return events


# Additional names make the module convenient for CLI adapters and hidden
# consumers while keeping one implementation of each operation.
parse_task_bytes = lambda content, context="task.md": Task.from_bytes(content, context)
parse_capture_bytes = lambda content, context="capture.md": Capture.from_bytes(content, context)
parse_artifact_bytes = lambda content, context="artifact.md": Artifact.from_bytes(content, context)
render_record = render_frontmatter
update_with_cas = cas_update


__all__ = [
    "SCHEMA_VERSION",
    "CLEAN_SCHEMA_VERSION",
    "OPEN_STATUSES",
    "CLOSED_STATUSES",
    "TASK_STATUSES",
    "TRANSITIONS",
    "PRIORITIES",
    "SENSITIVITIES",
    "ARTIFACT_ROLES",
    "CAPTURE_STATUSES",
    "CAPTURE_TYPES",
    "CAPTURE_DISPOSITIONS",
    "ModelError",
    "FrontmatterError",
    "ValidationError",
    "StateTransitionError",
    "CASConflict",
    "ImmutableFieldError",
    "FrontmatterDocument",
    "MarkdownRecord",
    "Task",
    "Capture",
    "Artifact",
    "EventReceipt",
    "Receipt",
    "CASUpdateResult",
    "sha256_bytes",
    "sha256_text",
    "sha256_file",
    "parse_date",
    "parse_timestamp",
    "format_timestamp",
    "parse_frontmatter",
    "parse_frontmatter_bytes",
    "parse_note_bytes",
    "render_frontmatter",
    "render_note_bytes",
    "render_record",
    "validate_task",
    "validate_capture",
    "validate_artifact",
    "validate_record",
    "parse_task_bytes",
    "parse_capture_bytes",
    "parse_artifact_bytes",
    "parse_record",
    "parse_record_bytes",
    "transition_task",
    "archive_task",
    "restore_task",
    "cas_update",
    "update_with_cas",
    "cas_update_path",
    "load_task_note",
    "save_task_note",
    "event_receipt",
    "make_event_receipt",
    "append_event",
    "read_events",
]
