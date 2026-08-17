from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple
from urllib.parse import quote, unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_workspace_model import (  # noqa: E402
    CLOSED_STATUSES,
    OPEN_STATUSES,
    PRIORITIES,
    ContractError,
    effective_material_sensitivity,
    is_excluded_path,
    load_json,
    load_workspace,
    validate_config,
    validate_generated_view,
    validate_lifecycle,
    validate_relative_path,
    validate_schema_documents,
    validate_task,
)


WORKSPACE = REPO_ROOT / "examples" / "workspace"
CATALOG = WORKSPACE / ".workspace-organizer" / "catalog"
OVERVIEW = WORKSPACE / "00_总览"
PRIORITY_RANK = {
    name: index
    for index, name in enumerate(("urgent", "high", "normal", "low"))
}
VISIBLE_SENSITIVITIES = {"public", "internal"}
MATERIAL_ROLES = ("inputs", "work", "deliverables", "records", "history")


def canonical_digest(items: List[Dict[str, Any]]) -> str:
    payload = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_view(name: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "view": name,
        "profile": "default",
        "source_sha256": canonical_digest(items),
        "items": items,
    }


def load_example_records() -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    return load_workspace(WORKSPACE)


def build_todo(records: Iterable[Tuple[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for record, task in records:
        if task["status"] not in OPEN_STATUSES:
            continue
        if task["sensitivity"] not in VISIBLE_SENSITIVITIES:
            continue
        items.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "priority": task["priority"],
                "due": task["due"],
                "sensitivity": task["sensitivity"],
                "next_action": task["next_action"],
                "record": record,
            }
        )
    items.sort(
        key=lambda item: (
            PRIORITY_RANK[item["priority"]],
            item["due"] is None,
            item["due"] or "",
            item["id"],
        )
    )
    return make_view("todo", items)


def build_timeline(todo: Mapping[str, Any]) -> Dict[str, Any]:
    items = [
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
        for item in todo["items"]
        if item["due"] is not None
    ]
    items.sort(
        key=lambda item: (
            item["date"],
            PRIORITY_RANK[item["priority"]],
            item["id"],
        )
    )
    return make_view("timeline", items)


def _material_item(
    path: Path,
    role: str,
    task_id: Any,
    sensitivity: str,
) -> Dict[str, Any]:
    relative = path.relative_to(WORKSPACE).as_posix()
    content = path.read_bytes()
    return {
        "path": relative,
        "role": role,
        "task_id": task_id,
        "sensitivity": sensitivity,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def build_materials(
    config: Mapping[str, Any], records: Iterable[Tuple[str, Mapping[str, Any]]]
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for record, task in records:
        if task["sensitivity"] not in VISIBLE_SENSITIVITIES:
            continue
        bundle = WORKSPACE / Path(record).parent
        for role in MATERIAL_ROLES:
            role_root = bundle / role
            if not role_root.is_dir():
                continue
            for path in sorted(role_root.rglob("*"), key=lambda item: item.as_posix()):
                relative = path.relative_to(WORKSPACE).as_posix()
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and not is_excluded_path(relative, config)
                ):
                    items.append(
                        _material_item(path, role, task["id"], task["sensitivity"])
                    )

    library_roots = [WORKSPACE / "30_资料库"] + [
        WORKSPACE.joinpath(*Path(item["path"]).parts)
        for item in config["adopted_material_roots"]
    ]
    seen_library_paths = set()
    for library_root in library_roots:
        if not library_root.is_dir() or library_root.is_symlink():
            continue
        for path in sorted(library_root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(WORKSPACE).as_posix()
            if relative in seen_library_paths:
                continue
            seen_library_paths.add(relative)
            if (
                path.is_file()
                and not path.is_symlink()
                and not is_excluded_path(relative, config)
            ):
                sensitivity = effective_material_sensitivity(relative, config)
                if sensitivity not in VISIBLE_SENSITIVITIES:
                    continue
                items.append(
                    _material_item(path, "library", None, sensitivity)
                )

    items.sort(key=lambda item: unicodedata.normalize("NFC", item["path"]))
    return make_view("materials", items)


def marker(view: Mapping[str, Any]) -> str:
    return (
        "<!-- workspace-organizer:generated "
        f"view={view['view']} schema=1 source_sha256={view['source_sha256']} -->"
    )


def markdown_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def task_cell(item: Mapping[str, Any]) -> str:
    target = "../" + quote(item["record"], safe="/-._~")
    return f"[{item['id']}]({target}) — {markdown_escape(item['title'])}"


def render_todo(view: Mapping[str, Any]) -> str:
    lines = [
        marker(view),
        "# TODO",
        "",
        "| Priority | Due | Status | Task | Next action |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in view["items"]:
        lines.append(
            "| "
            + " | ".join(
                (
                    item["priority"],
                    item["due"] or "—",
                    item["status"],
                    task_cell(item),
                    markdown_escape(item["next_action"]),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def render_timeline(view: Mapping[str, Any]) -> str:
    lines = [
        marker(view),
        "# Timeline",
        "",
        "| Date | Event | Priority | Status | Task |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in view["items"]:
        lines.append(
            "| "
            + " | ".join(
                (
                    item["date"],
                    item["event"],
                    item["priority"],
                    item["status"],
                    task_cell(item),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def render_materials(view: Mapping[str, Any]) -> str:
    lines = [
        marker(view),
        "# Materials",
        "",
        "| Role | Task | Material | Bytes | SHA-256 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in view["items"]:
        target = "../" + quote(item["path"], safe="/-._~")
        material = f"[{markdown_escape(item['path'])}]({target})"
        lines.append(
            "| "
            + " | ".join(
                (
                    item["role"],
                    item["task_id"] or "—",
                    material,
                    str(item["bytes"]),
                    item["sha256"],
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


class WorkspaceModelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config, cls.records = load_example_records()
        cls.todo = build_todo(cls.records)
        cls.timeline = build_timeline(cls.todo)
        cls.materials = build_materials(cls.config, cls.records)

    def test_schema_documents_and_lifecycle_are_consistent(self) -> None:
        validate_schema_documents(REPO_ROOT)
        lifecycle = validate_lifecycle(REPO_ROOT / "contracts" / "lifecycle.json")
        self.assertEqual(set(lifecycle["transitions"]), OPEN_STATUSES | CLOSED_STATUSES | {"archived"})

    def test_transition_graph_is_complete_and_shared(self) -> None:
        lifecycle = validate_lifecycle(REPO_ROOT / "contracts" / "lifecycle.json")
        expected = {
            "planned": ["active", "cancelled"],
            "active": ["waiting", "blocked", "completed", "cancelled"],
            "waiting": ["active", "blocked", "completed", "cancelled"],
            "blocked": ["active", "waiting", "completed", "cancelled"],
            "completed": ["active", "archived"],
            "cancelled": ["planned", "active", "archived"],
            "archived": [],
        }
        self.assertEqual(lifecycle["transitions"], expected)
        for source in expected:
            for destination in expected:
                allowed = destination in expected[source]
                self.assertEqual(allowed, destination in lifecycle["transitions"][source])

    def test_all_representative_task_families_use_one_contract(self) -> None:
        self.assertEqual(len(self.records), 7)
        self.assertEqual(
            {task["type"] for _, task in self.records},
            {
                "research",
                "review",
                "reimbursement",
                "presentation",
                "competition",
                "collaboration",
                "contract",
            },
        )
        self.assertEqual(len({task["id"] for _, task in self.records}), 7)
        self.assertEqual(PRIORITIES, {"urgent", "high", "normal", "low"})

    def test_archive_metadata_candidates_and_destinations_are_decidable(self) -> None:
        by_id = {task["id"]: task for _, task in self.records}
        metadata_candidates = {
            task_id
            for task_id, task in by_id.items()
            if task["status"] in CLOSED_STATUSES
            and task["next_action"] is None
            and task["closed_at"] is not None
        }
        self.assertEqual(
            metadata_candidates,
            {"reimbursement-conference", "contract-vendor-renewal"},
        )
        reimbursement = by_id["reimbursement-conference"]
        destination = (
            f"90_归档/{reimbursement['closed_at'][:4]}/"
            f"{reimbursement['area']}/{reimbursement['id']}"
        )
        self.assertEqual(
            destination,
            "90_归档/2026/administration/reimbursement-conference",
        )

    def test_adopted_paths_are_exact_relative_unicode_paths(self) -> None:
        config = load_json(REPO_ROOT / "examples" / "adoption" / "config.json")
        validate_config(config, "examples/adoption/config.json")
        self.assertEqual(config["adopted_task_paths"], ["Existing Projects/Research α"])
        for invalid in ("/absolute", "../escape", "a//b", "a\\b", "a/./b"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ContractError):
                    validate_relative_path(invalid, "invalid")

    def test_material_sensitivity_uses_the_most_restrictive_declaration(self) -> None:
        self.assertEqual(
            effective_material_sensitivity(
                "Legacy Library/general/reference-note.md", self.config
            ),
            "internal",
        )
        self.assertEqual(
            effective_material_sensitivity(
                "Legacy Library/Restricted/hidden-note.md", self.config
            ),
            "restricted",
        )

    def test_configuration_rejects_ambiguous_path_overlap(self) -> None:
        base = {
            "schema_version": 1,
            "workspace_id": "overlap-test",
            "default_sensitivity": "internal",
            "adopted_task_paths": [],
            "adopted_material_roots": [],
            "exclude_paths": [],
        }
        invalid_cases = [
            {
                "adopted_task_paths": ["Tasks/Alpha", "Tasks/Alpha/Nested"],
            },
            {
                "adopted_task_paths": ["Tasks/Alpha"],
                "adopted_material_roots": [
                    {"path": "Tasks/Alpha/References", "sensitivity": "internal"}
                ],
            },
            {
                "adopted_task_paths": ["Tasks/Alpha"],
                "exclude_paths": ["Tasks"],
            },
            {
                "exclude_paths": ["Private", "Private/Nested"],
            },
        ]
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                config = copy.deepcopy(base)
                config.update(overrides)
                with self.assertRaises(ContractError):
                    validate_config(config, "overlap-test")

        nested_materials = copy.deepcopy(base)
        nested_materials["adopted_material_roots"] = [
            {"path": "Library", "sensitivity": "public"},
            {"path": "Library/Restricted", "sensitivity": "restricted"},
        ]
        nested_materials["exclude_paths"] = ["Library/Generated"]
        validate_config(nested_materials, "nested-materials")

    def test_loader_rejects_symlink_escape_before_reading_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            workspace = parent / "workspace"
            outside = parent / "outside"
            (workspace / ".workspace-organizer").mkdir(parents=True)
            outside.mkdir()
            (outside / "TASK.md").write_text("invalid", encoding="utf-8")
            (workspace / "External Task").symlink_to(
                outside, target_is_directory=True
            )
            config = {
                "schema_version": 1,
                "workspace_id": "symlink-test",
                "default_sensitivity": "internal",
                "adopted_task_paths": ["External Task"],
                "adopted_material_roots": [],
                "exclude_paths": [],
            }
            (workspace / ".workspace-organizer" / "config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )

            with self.assertRaisesRegex(ContractError, "symlink"):
                load_workspace(workspace)

    def test_loader_rejects_canonical_task_and_adopted_material_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / ".workspace-organizer").mkdir()
            bundle = workspace / "20_任务" / "research-agent-safety"
            bundle.mkdir(parents=True)
            source = WORKSPACE / "20_任务" / "research-agent-safety" / "TASK.md"
            (bundle / "TASK.md").write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            config = {
                "schema_version": 1,
                "workspace_id": "canonical-overlap-test",
                "default_sensitivity": "internal",
                "adopted_task_paths": [],
                "adopted_material_roots": [
                    {
                        "path": "20_任务/research-agent-safety/inputs",
                        "sensitivity": "public",
                    }
                ],
                "exclude_paths": [],
            }
            (workspace / ".workspace-organizer" / "config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )

            with self.assertRaisesRegex(ContractError, "task and material"):
                load_workspace(workspace)

    def test_loader_skips_excluded_and_nested_git_tasks_before_reading(self) -> None:
        for boundary in ("excluded", "nested-git"):
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as temporary:
                    workspace = Path(temporary)
                    (workspace / ".workspace-organizer").mkdir()
                    bundle = workspace / "20_任务" / boundary
                    bundle.mkdir(parents=True)
                    (bundle / "TASK.md").write_text("invalid", encoding="utf-8")
                    if boundary == "nested-git":
                        (bundle / ".git").mkdir()
                    config = {
                        "schema_version": 1,
                        "workspace_id": "boundary-test",
                        "default_sensitivity": "internal",
                        "adopted_task_paths": [],
                        "adopted_material_roots": [],
                        "exclude_paths": (
                            [f"20_任务/{boundary}"]
                            if boundary == "excluded"
                            else []
                        ),
                    }
                    (workspace / ".workspace-organizer" / "config.json").write_text(
                        json.dumps(config), encoding="utf-8"
                    )

                    _, records = load_workspace(workspace)
                    self.assertEqual(records, [])

    def test_relative_path_schemas_match_the_python_validator(self) -> None:
        config_schema = load_json(REPO_ROOT / "schemas/workspace-config.schema.json")
        view_schema = load_json(REPO_ROOT / "schemas/generated-view.schema.json")
        config_pattern = config_schema["$defs"]["relativePath"]["pattern"]
        view_pattern = view_schema["$defs"]["relativePath"]["pattern"]
        self.assertEqual(config_pattern, view_pattern)

        probes = {
            "valid/path": True,
            "trailing/": False,
            "empty//segment": False,
            "backslash\\segment": False,
            "dot/./segment": False,
            "parent/../segment": False,
            "nul\x00segment": False,
        }
        compiled = re.compile(config_pattern)
        for value, expected in probes.items():
            with self.subTest(value=value):
                self.assertEqual(compiled.fullmatch(value) is not None, expected)
                try:
                    validate_relative_path(value, "probe")
                    accepted = True
                except ContractError:
                    accepted = False
                self.assertEqual(accepted, expected)

    def test_invalid_task_records_fail_closed(self) -> None:
        _, source = self.records[0]
        cases = []
        missing_sensitivity = copy.deepcopy(source)
        del missing_sensitivity["sensitivity"]
        cases.append(missing_sensitivity)
        invalid_id = copy.deepcopy(source)
        invalid_id["id"] = "Not Stable"
        cases.append(invalid_id)
        unknown_field = copy.deepcopy(source)
        unknown_field["owner"] = "someone"
        cases.append(unknown_field)
        closed_with_action = copy.deepcopy(source)
        closed_with_action.update(
            status="completed",
            closed_at="2026-08-17T10:00:00+08:00",
        )
        cases.append(closed_with_action)
        malformed_time = copy.deepcopy(source)
        malformed_time["updated"] = "2026-08-17"
        cases.append(malformed_time)
        wrong_enum_type = copy.deepcopy(source)
        wrong_enum_type["sensitivity"] = ["internal"]
        cases.append(wrong_enum_type)

        for index, candidate in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ContractError):
                    validate_task(candidate, f"case-{index}")

    def test_generated_catalogs_match_normative_projection(self) -> None:
        for name, expected in (
            ("todo", self.todo),
            ("timeline", self.timeline),
            ("materials", self.materials),
        ):
            with self.subTest(view=name):
                actual = load_json(CATALOG / f"{name}.json")
                validate_generated_view(actual, f"catalog/{name}.json")
                self.assertEqual(actual, expected)
                self.assertEqual(actual["source_sha256"], canonical_digest(actual["items"]))
                serialized = json.dumps(
                    expected,
                    ensure_ascii=False,
                    indent=2,
                ) + "\n"
                self.assertEqual(
                    (CATALOG / f"{name}.json").read_text(encoding="utf-8"),
                    serialized,
                )

    def test_markdown_views_match_catalogs_byte_for_byte(self) -> None:
        expected = {
            "TODO.md": render_todo(self.todo),
            "TIMELINE.md": render_timeline(self.timeline),
            "MATERIALS.md": render_materials(self.materials),
        }
        for name, rendered in expected.items():
            with self.subTest(view=name):
                self.assertEqual((OVERVIEW / name).read_text(encoding="utf-8"), rendered)

    def test_generated_markdown_links_resolve_to_workspace_files(self) -> None:
        for name in ("TODO.md", "TIMELINE.md", "MATERIALS.md"):
            text = (OVERVIEW / name).read_text(encoding="utf-8")
            targets = re.findall(r"\]\(([^)]+)\)", text)
            self.assertTrue(targets, name)
            for target in targets:
                with self.subTest(view=name, target=target):
                    resolved = (OVERVIEW / unquote(target)).resolve()
                    self.assertTrue(resolved.is_file(), resolved)

    def test_generated_view_validator_rejects_order_and_ownership_drift(self) -> None:
        wrong_order = copy.deepcopy(self.todo)
        wrong_order["items"].reverse()
        wrong_order["source_sha256"] = canonical_digest(wrong_order["items"])
        with self.assertRaises(ContractError):
            validate_generated_view(wrong_order, "wrong-order")

        wrong_owner = copy.deepcopy(self.materials)
        library = next(item for item in wrong_owner["items"] if item["role"] == "library")
        library["task_id"] = "research-agent-safety"
        wrong_owner["source_sha256"] = canonical_digest(wrong_owner["items"])
        with self.assertRaises(ContractError):
            validate_generated_view(wrong_owner, "wrong-owner")

    def test_sensitive_tasks_and_materials_do_not_affect_default_views(self) -> None:
        output = "\n".join(
            json.dumps(view, ensure_ascii=False, sort_keys=True)
            for view in (self.todo, self.timeline, self.materials)
        )
        for forbidden in (
            "review-paper-robustness",
            "Review a robustness manuscript",
            "private-review-note.md",
            "reimbursement-conference",
            "synthetic-receipt.txt",
            "contract-vendor-renewal",
            "terms-summary.md",
            "hidden-note.md",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, output)

        changed_records = copy.deepcopy(self.records)
        for _, task in changed_records:
            if task["sensitivity"] in {"confidential", "restricted"}:
                task["title"] = "Changed but still excluded"
                if task["next_action"] is not None:
                    task["next_action"] = "Changed but still excluded"
        self.assertEqual(build_todo(changed_records), self.todo)
        self.assertEqual(build_timeline(build_todo(changed_records)), self.timeline)

    def test_default_projection_is_repeatable(self) -> None:
        config, records = load_example_records()
        todo = build_todo(records)
        timeline = build_timeline(todo)
        materials = build_materials(config, records)
        self.assertEqual(todo, self.todo)
        self.assertEqual(timeline, self.timeline)
        self.assertEqual(materials, self.materials)
        self.assertNotIn("generated_at", json.dumps((todo, timeline, materials)))


if __name__ == "__main__":
    unittest.main()
