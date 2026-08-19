from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = (
    REPO_ROOT
    / "skill"
    / "workspace-organizer"
    / "scripts"
    / "workspace_organizer.py"
)
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "scenarios"
SPEC = importlib.util.spec_from_file_location("workspace_organizer_scenarios", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tool)


def load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def family_task(
    family: Mapping[str, Any],
    status: Optional[str] = None,
) -> Dict[str, Any]:
    status = status or family["workspace_status"]
    closed = "2026-08-19T10:00:00+08:00" if status in tool.CLOSED_STATUSES or status == "archived" else None
    archived = "2026-08-19T11:00:00+08:00" if status == "archived" else None
    if archived is not None:
        updated = archived
    elif closed is not None:
        updated = closed
    else:
        updated = "2026-08-19T09:00:00+08:00"
    next_action = None if closed is not None else (family.get("next_action") or "Review the synthetic task")
    return {
        "schema_version": 1,
        "id": family["id"],
        "title": family["title"],
        "status": status,
        "area": family["area"],
        "type": family["type"],
        "priority": family["priority"],
        "due": family["due"],
        "sensitivity": family["sensitivity"],
        "next_action": next_action,
        "updated": updated,
        "closed_at": closed,
        "archived_at": archived,
        "tags": ["synthetic", family["type"]],
    }


def write_task(bundle: Path, data: Mapping[str, Any]) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    body = f"# {data['title']}\n\nPublic synthetic scenario record.\n"
    (bundle / "TASK.md").write_bytes(tool.serialize_task(data, body))


def write_plan(path: Path, plan: Mapping[str, Any]) -> Path:
    tool.write_immutable_json(path, plan)
    approval = path.with_suffix(".approval.json")
    tool.approve_plan(path, approval, confirmed=True)
    return approval


def file_manifest(
    root: Path,
    *,
    excluded_roots: Iterable[str] = (),
) -> Dict[str, str]:
    excluded = tuple(excluded_roots)
    result: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in excluded):
            continue
        if path.is_symlink():
            result[relative] = f"symlink:{os.readlink(path)}"
        elif path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def generated_bytes(root: Path) -> Dict[str, bytes]:
    return {
        relative: (root / relative).read_bytes()
        for pair in tool.GENERATED_PATHS.values()
        for relative in pair
    }


EXPECTED_SCENARIO_TESTS = (
    "test_seven_families_share_one_schema_and_lifecycle",
    "test_empty_initialization_full_pipeline_and_difficult_files",
    "test_adopt_in_place_preserves_existing_content_and_archives_once",
    "test_stale_and_collision_cases_leave_manifest_unchanged",
    "test_index_failure_rolls_back_all_six_outputs",
    "test_public_fixtures_and_requirement_matrix_are_hygienic_and_complete",
)
EXPECTED_REQUIREMENT_SOURCES = {
    "issue_acceptance": "https://github.com/caozx1110/workspace-organizer-skill/issues/5",
    "epic_success_criteria": "https://github.com/caozx1110/workspace-organizer-skill/issues/1",
    "epic_definition_of_done": "https://github.com/caozx1110/workspace-organizer-skill/issues/1",
}
EXPECTED_GATE_REGISTRY = {
    "gate:skill-quick-validator": [
        "python3",
        "{skill_creator_root}/scripts/quick_validate.py",
        "skill/workspace-organizer",
    ],
    "gate:repository-tests": [
        "python3",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
    ],
}
EXPECTED_REQUIREMENTS = {
    "issue_acceptance": {
        "ISSUE-5-AC-01": (
            "All seven representative task families pass through the same task lifecycle and schema.",
            ("test_seven_families_share_one_schema_and_lifecycle",),
        ),
        "ISSUE-5-AC-02": (
            "New-workspace initialization and adopt-in-place flows are both covered end to end.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_adopt_in_place_preserves_existing_content_and_archives_once",
            ),
        ),
        "ISSUE-5-AC-03": (
            "Unicode paths, spaces, large binary placeholders, compressed files, duplicates, temporary files, nested Git repositories, and sensitive materials have explicit expected behavior.",
            ("test_empty_initialization_full_pipeline_and_difficult_files",),
        ),
        "ISSUE-5-AC-04": (
            "Dry-run, approval, apply, verification, deterministic indexes, archive, and rollback evidence are exercised.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_adopt_in_place_preserves_existing_content_and_archives_once",
                "test_index_failure_rolls_back_all_six_outputs",
            ),
        ),
        "ISSUE-5-AC-05": (
            "Tests assert absence of unapproved deletion, overwrite, movement, publication, and sensitive-data exposure.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_adopt_in_place_preserves_existing_content_and_archives_once",
                "test_stale_and_collision_cases_leave_manifest_unchanged",
            ),
        ),
        "ISSUE-5-AC-06": (
            "Repeated runs produce equivalent plans and generated views for unchanged inputs.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_adopt_in_place_preserves_existing_content_and_archives_once",
            ),
        ),
        "ISSUE-5-AC-07": (
            "Every global success criterion in Epic #1 maps to at least one repeatable scenario or focused test.",
            ("test_public_fixtures_and_requirement_matrix_are_hygienic_and_complete",),
        ),
        "ISSUE-5-AC-08": (
            "Fixtures contain no private paths, credentials, personal records, or proprietary datasets.",
            ("test_public_fixtures_and_requirement_matrix_are_hygienic_and_complete",),
        ),
    },
    "epic_success_criteria": {
        "EPIC-SC-01": (
            "A new workspace can be initialized with a predictable human-readable structure and machine-readable control data.",
            ("test_empty_initialization_full_pipeline_and_difficult_files",),
        ),
        "EPIC-SC-02": (
            "Every task has a stable ASCII identifier and one canonical `TASK.md`; TODO, timeline, and material indexes are generated deterministically from task records.",
            (
                "test_seven_families_share_one_schema_and_lifecycle",
                "test_empty_initialization_full_pipeline_and_difficult_files",
            ),
        ),
        "EPIC-SC-03": (
            "Research, paper review, reimbursement, presentation, competition, external collaboration, and contract scenarios fit the same lifecycle model through labels and optional templates.",
            ("test_seven_families_share_one_schema_and_lifecycle",),
        ),
        "EPIC-SC-04": (
            "File classification and migration run as scan -> proposal -> dry-run -> explicit approval -> apply -> verify, with no automatic overwrite or deletion.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_stale_and_collision_cases_leave_manifest_unchanged",
            ),
        ),
        "EPIC-SC-05": (
            "Completed work archives once under `90_归档/YYYY/<area>/<task-id>/`, while ambiguous, duplicate, temporary, and compressed materials remain isolated for confirmation.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_adopt_in_place_preserves_existing_content_and_archives_once",
            ),
        ),
        "EPIC-SC-06": (
            "A later static HTML dashboard can render TODO and timeline views from the same source data without becoming a second source of truth.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "tests.test_workspace_model.WorkspaceModelContractTests.test_generated_catalogs_match_normative_projection",
            ),
        ),
    },
    "epic_definition_of_done": {
        "EPIC-DOD-01": (
            "The skill passes the `skill-creator` structural validator and all repository tests.",
            ("gate:skill-quick-validator", "gate:repository-tests"),
        ),
        "EPIC-DOD-02": (
            "Initialization, adoption, task updates, generated views, archival, and rollback behavior are documented and tested.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_adopt_in_place_preserves_existing_content_and_archives_once",
                "test_index_failure_rolls_back_all_six_outputs",
            ),
        ),
        "EPIC-DOD-03": (
            "Generated indexes are deterministic and can locate the canonical task and material roles without scanning every binary on each request.",
            ("test_empty_initialization_full_pipeline_and_difficult_files",),
        ),
        "EPIC-DOD-04": (
            "Destructive behavior is absent by default; move operations require approved plans and produce verification evidence.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "test_stale_and_collision_cases_leave_manifest_unchanged",
            ),
        ),
        "EPIC-DOD-05": (
            "Representative task families work without introducing task-specific lifecycle forks.",
            ("test_seven_families_share_one_schema_and_lifecycle",),
        ),
        "EPIC-DOD-06": (
            "Public examples and test fixtures contain no private paths, proprietary data, credentials, or personal documents.",
            ("test_public_fixtures_and_requirement_matrix_are_hygienic_and_complete",),
        ),
        "EPIC-DOD-07": (
            "v1 is usable without the HTML dashboard; the v2 dashboard reads the same canonical data and excludes sensitive content by default.",
            (
                "test_empty_initialization_full_pipeline_and_difficult_files",
                "tests.test_workspace_model.WorkspaceModelContractTests.test_sensitive_tasks_and_materials_do_not_affect_default_views",
            ),
        ),
    },
}


def _flatten_tests(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    cases = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            cases.extend(_flatten_tests(item))
        else:
            cases.append(item)
    return cases


def _reference_resolves_once(reference: str) -> bool:
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(reference)
        cases = _flatten_tests(suite)
    except (AttributeError, ImportError, TypeError, ValueError):
        return False
    return (
        len(cases) == 1
        and isinstance(cases[0], unittest.TestCase)
        and cases[0].__class__.__name__ != "_FailedTest"
        and cases[0].id() == reference
    )


def validate_requirement_matrix(
    matrix: Mapping[str, Any],
    scenario_case: type[unittest.TestCase],
) -> list[str]:
    errors = []
    expected_keys = {
        "schema_version",
        "suite",
        "sources",
        "gate_registry",
        "tests",
        *EXPECTED_REQUIREMENTS,
    }
    if set(matrix) != expected_keys:
        errors.append("top-level matrix keys do not match the reviewed schema")
    if matrix.get("schema_version") != 1:
        errors.append("matrix schema_version must be 1")
    if matrix.get("suite") != "workspace-organizer-scenarios":
        errors.append("matrix suite identity changed")
    if matrix.get("sources") != EXPECTED_REQUIREMENT_SOURCES:
        errors.append("Issue/Epic source URL registry changed")
    if matrix.get("gate_registry") != EXPECTED_GATE_REGISTRY:
        errors.append("delivery gate registry changed or contains an unknown gate")

    declared_tests = matrix.get("tests")
    if declared_tests != list(EXPECTED_SCENARIO_TESTS):
        errors.append("scenario test registry changed")
    actual_tests = {
        name for name in dir(scenario_case) if name.startswith("test_")
    }
    if actual_tests != set(EXPECTED_SCENARIO_TESTS):
        errors.append("ScenarioValidationTests methods do not match the reviewed registry")
    for reference in EXPECTED_SCENARIO_TESTS:
        qualified = f"{scenario_case.__module__}.{scenario_case.__qualname__}.{reference}"
        if not callable(getattr(scenario_case, reference, None)):
            errors.append(f"local test reference is not callable: {reference}")
        elif not _reference_resolves_once(qualified):
            errors.append(f"local test reference does not resolve exactly once: {reference}")

    for group, expected_rows in EXPECTED_REQUIREMENTS.items():
        rows = matrix.get(group)
        if not isinstance(rows, list):
            errors.append(f"{group} must be a list")
            continue
        actual_ids = [row.get("id") for row in rows if isinstance(row, dict)]
        if len(actual_ids) != len(rows) or len(set(actual_ids)) != len(actual_ids):
            errors.append(f"{group} contains an invalid or duplicate ID")
        if set(actual_ids) != set(expected_rows):
            errors.append(f"{group} IDs do not match the reviewed snapshot")
        by_id = {
            row["id"]: row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        for requirement_id, (criterion, coverage) in expected_rows.items():
            row = by_id.get(requirement_id)
            if row is None:
                continue
            if set(row) != {"id", "criterion", "coverage"}:
                errors.append(f"{requirement_id} fields changed")
            if row.get("criterion") != criterion:
                errors.append(f"{requirement_id} criterion differs from the tracked source text")
            if row.get("coverage") != list(coverage):
                errors.append(f"{requirement_id} coverage differs from the reviewed mapping")
            for reference in row.get("coverage", []):
                if reference in EXPECTED_SCENARIO_TESTS:
                    if reference not in (declared_tests or []):
                        errors.append(f"undeclared local test reference: {reference}")
                elif isinstance(reference, str) and reference.startswith("tests."):
                    if not _reference_resolves_once(reference):
                        errors.append(
                            f"dotted test reference does not resolve exactly one real test: {reference}"
                        )
                elif isinstance(reference, str) and reference.startswith("gate:"):
                    if reference not in EXPECTED_GATE_REGISTRY:
                        errors.append(f"unknown delivery gate reference: {reference}")
                else:
                    errors.append(f"unknown coverage reference: {reference!r}")
    return errors


class ScenarioValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = load_json(FIXTURE_ROOT / "scenarios.json")
        cls.matrix = load_json(FIXTURE_ROOT / "requirement-matrix.json")

    def _initialize(
        self,
        base: Path,
        root: Path,
        workspace_id: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        plan = tool.build_initialization_plan(root, workspace_id, **kwargs)
        plan_path = base / f"{workspace_id}-init.json"
        tool.write_immutable_json(plan_path, plan)
        before = file_manifest(root)
        dry = tool.dry_run(root, plan_path)
        self.assertEqual(dry["status"], "ready")
        self.assertFalse(dry["mutated"])
        self.assertEqual(file_manifest(root), before)
        approval = plan_path.with_suffix(".approval.json")
        tool.approve_plan(plan_path, approval, confirmed=True)
        record = tool.apply_plan(root, plan_path, approval)
        self.assertEqual(record["status"], "verified")
        self.assertEqual(tool.verify_plan(root, plan_path)["status"], "verified")
        self.assertEqual(tool.apply_plan(root, plan_path, approval), record)
        return record

    def _assert_only_file_move(
        self,
        before: Mapping[str, str],
        after: Mapping[str, str],
        source: str,
        destination: str,
    ) -> None:
        expected_paths = (set(before) - {source}) | {destination}
        self.assertEqual(set(after), expected_paths)
        self.assertEqual(after[destination], before[source])
        for relative in set(before) - {source}:
            self.assertEqual(after[relative], before[relative], relative)

    def _assert_only_archive_move(
        self,
        before: Mapping[str, str],
        after: Mapping[str, str],
        source: str,
        destination: str,
    ) -> None:
        source_files = {
            relative for relative in before if relative.startswith(f"{source}/")
        }
        expected_destinations = {
            f"{destination}/{relative.removeprefix(source + '/')}"
            for relative in source_files
        }
        self.assertEqual(
            set(after),
            (set(before) - source_files) | expected_destinations,
        )
        for relative in set(before) - source_files:
            self.assertEqual(after[relative], before[relative], relative)
        for relative in source_files:
            local = relative.removeprefix(source + "/")
            archived = f"{destination}/{local}"
            if local != "TASK.md":
                self.assertEqual(after[archived], before[relative], archived)
        self.assertNotEqual(
            after[f"{destination}/TASK.md"],
            before[f"{source}/TASK.md"],
        )

    def test_seven_families_share_one_schema_and_lifecycle(self) -> None:
        lifecycle = load_json(REPO_ROOT / "contracts" / "lifecycle.json")
        families = self.scenarios["families"]
        expected_types = {
            "research",
            "review",
            "reimbursement",
            "presentation",
            "competition",
            "collaboration",
            "contract",
        }
        self.assertEqual({family["type"] for family in families}, expected_types)
        self.assertEqual(len(families), 7)

        transition_path = ("planned", "active", "completed", "archived")
        field_sets = set()
        for family in families:
            stable_id = family["id"]
            for current, following in zip(transition_path, transition_path[1:]):
                self.assertIn(following, lifecycle["transitions"][current])
            for status in transition_path:
                with self.subTest(family=family["type"], status=status):
                    task = family_task(family, status)
                    tool.validate_task(task, f"{family['type']}:{status}")
                    serialized = tool.serialize_task(task, "# Synthetic task\n")
                    parsed, _ = tool.parse_task_bytes(serialized)
                    self.assertEqual(parsed, task)
                    self.assertEqual(parsed["id"], stable_id)
                    self.assertEqual(parsed["type"], family["type"])
                    field_sets.add(tuple(sorted(parsed)))
        self.assertEqual(len(field_sets), 1, "task types must not add lifecycle-specific fields")

    def test_empty_initialization_full_pipeline_and_difficult_files(self) -> None:
        difficult = self.scenarios["difficult_cases"]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()

            first_init = tool.build_initialization_plan(root, "scenario-workspace")
            second_init = tool.build_initialization_plan(root, "scenario-workspace")
            self.assertEqual(first_init, second_init)
            self._initialize(base, root, "scenario-workspace")
            for managed in tool.INITIALIZATION_DIRECTORIES:
                self.assertTrue((root / managed).is_dir(), managed)

            families = self.scenarios["families"]
            for family in families:
                bundle = root / "20_任务" / family["id"]
                write_task(bundle, family_task(family))
                material = family["material"]
                material_path = bundle / material["role"] / material["path"]
                material_path.parent.mkdir(parents=True, exist_ok=True)
                material_path.write_text(material["content"], encoding="utf-8")

            move = difficult["organize_move"]
            move_source = root / move["source"]
            move_source.parent.mkdir(parents=True, exist_ok=True)
            move_source.write_text(move["content"], encoding="utf-8")

            large = difficult["large_placeholder"]
            large_path = root / large["path"]
            large_path.write_bytes(large["byte"].encode("ascii") * large["size"])

            compressed = difficult["compressed_original"]
            compressed_path = root / compressed["path"]
            with zipfile.ZipFile(compressed_path, "w") as archive:
                for member in compressed["members"]:
                    archive.writestr(member["path"], member["content"])

            exact = difficult["exact_duplicates"]
            for relative in exact["paths"]:
                (root / relative).write_text(exact["content"], encoding="utf-8")
            for item in difficult["suspected_duplicates"]:
                (root / item["path"]).write_text(item["content"], encoding="utf-8")

            temporary_file = difficult["temporary_file"]
            temporary_path = root / temporary_file["path"]
            temporary_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(temporary_file["content"], encoding="utf-8")

            nested = difficult["nested_git"]
            nested_root = root / nested["root"]
            (nested_root / ".git").mkdir(parents=True)
            protected_path = root / nested["protected_file"]
            protected_path.write_text(nested["content"], encoding="utf-8")

            first_scan = tool.scan_workspace(root, hash_limit=large["scan_hash_limit"])
            second_scan = tool.scan_workspace(root, hash_limit=large["scan_hash_limit"])
            self.assertEqual(first_scan, second_scan)
            by_path = {item["path"]: item for item in first_scan["entries"]}
            self.assertEqual(by_path[large["path"]]["hash_state"], "deferred_large_file")
            self.assertIsNone(by_path[large["path"]]["sha256"])
            self.assertEqual(by_path[compressed["path"]]["hash_state"], "metadata_only")
            self.assertNotIn(nested["protected_file"], by_path)
            self.assertIn(
                {"path": nested["root"], "reason": "nested_git"},
                first_scan["boundaries"],
            )

            exact_group = next(
                item
                for item in first_scan["duplicate_candidates"]
                if set(item["paths"]) == set(exact["paths"])
            )
            self.assertEqual(exact_group["action"], "confirmation_required")
            suspected_paths = {item["path"] for item in difficult["suspected_duplicates"]}
            self.assertFalse(
                any(suspected_paths <= set(item["paths"]) for item in first_scan["duplicate_candidates"])
            )
            proposals = {item["source"]: item for item in first_scan["proposals"]}
            for relative in {
                move["source"],
                large["path"],
                compressed["path"],
                *exact["paths"],
                *suspected_paths,
                temporary_file["path"],
            }:
                self.assertEqual(proposals[relative]["classification"], "confirmation_required")
                self.assertIsNone(proposals[relative]["destination"])
                self.assertEqual(proposals[relative]["sensitivity"], "restricted")

            compressed_before = compressed_path.read_bytes()
            with self.assertRaises(tool.WorkspaceError):
                tool.inspect_compressed(root, compressed["path"], confirmed=False)
            inspected = tool.inspect_compressed(root, compressed["path"], confirmed=True)
            self.assertEqual(
                [item["path"] for item in inspected["entries"]],
                sorted(member["path"] for member in compressed["members"]),
            )
            self.assertFalse(inspected["content_extracted"])
            self.assertFalse(inspected["source_mutated"])
            self.assertEqual(compressed_path.read_bytes(), compressed_before)

            moves = [{"source": move["source"], "destination": move["destination"]}]
            first_plan = tool.build_organize_plan(root, moves)
            second_plan = tool.build_organize_plan(root, moves)
            self.assertEqual(first_plan, second_plan)
            organize_path = base / "organize.json"
            tool.write_immutable_json(organize_path, first_plan)
            before_move = file_manifest(root, excluded_roots=(".workspace-organizer",))
            self.assertEqual(tool.dry_run(root, organize_path)["status"], "ready")
            self.assertEqual(
                file_manifest(root, excluded_roots=(".workspace-organizer",)),
                before_move,
            )
            approval = organize_path.with_suffix(".approval.json")
            tool.approve_plan(organize_path, approval, confirmed=True)
            move_record = tool.apply_plan(root, organize_path, approval)
            self.assertEqual(tool.verify_plan(root, organize_path)["status"], "verified")
            self.assertEqual(tool.apply_plan(root, organize_path, approval), move_record)
            after_move = file_manifest(root, excluded_roots=(".workspace-organizer",))
            self._assert_only_file_move(before_move, after_move, move["source"], move["destination"])
            rollback = move_record["results"][0]["post_apply"]["rollback"]
            self.assertEqual(rollback["copy_from"], move["destination"])
            self.assertEqual(rollback["restore_to"], move["source"])
            self.assertEqual(rollback["required_sha256"], after_move[move["destination"]])

            indexed = tool.generate_indexes(root)
            self.assertEqual(indexed["status"], "verified")
            first_outputs = generated_bytes(root)
            self.assertEqual(tool.generate_indexes(root)["status"], "already_current")
            self.assertEqual(generated_bytes(root), first_outputs)

            todo = json.loads(
                (root / ".workspace-organizer/catalog/todo.json").read_text(encoding="utf-8")
            )
            materials = json.loads(
                (root / ".workspace-organizer/catalog/materials.json").read_text(encoding="utf-8")
            )
            todo_ids = {item["id"] for item in todo["items"]}
            self.assertEqual(
                todo_ids,
                {
                    "research-agent-safety",
                    "presentation-quarterly-update",
                    "competition-robotics",
                    "collaboration-dataset",
                },
            )
            material_paths = {item["path"] for item in materials["items"]}
            self.assertIn(move["destination"], material_paths)
            generated_text = b"\n".join(first_outputs.values()).decode("utf-8")
            for forbidden in (
                "review-paper-robustness",
                "confidential-review-note.md",
                "contract-vendor-renewal",
                "restricted-terms.txt",
                temporary_file["path"],
                compressed["path"],
                nested["protected_file"],
            ):
                self.assertNotIn(forbidden, generated_text)
            self.assertTrue(
                all(
                    item["path"].startswith((".workspace-organizer/catalog/", "00_总览/"))
                    for item in indexed["outputs"]
                ),
                "index must remain a local derived projection, not a publication action",
            )

            archive_family = next(
                family for family in families if family["id"] == "reimbursement-conference"
            )
            archive_source = f"20_任务/{archive_family['id']}"
            archive_destination = (
                f"90_归档/2026/{archive_family['area']}/{archive_family['id']}"
            )
            first_archive = tool.build_archive_plan(
                root,
                archive_family["id"],
                "2026-08-19T11:00:00+08:00",
            )
            second_archive = tool.build_archive_plan(
                root,
                archive_family["id"],
                "2026-08-19T11:00:00+08:00",
            )
            self.assertEqual(first_archive, second_archive)
            archive_path = base / "archive.json"
            tool.write_immutable_json(archive_path, first_archive)
            before_archive = file_manifest(root, excluded_roots=(".workspace-organizer",))
            self.assertEqual(tool.dry_run(root, archive_path)["status"], "ready")
            self.assertEqual(
                file_manifest(root, excluded_roots=(".workspace-organizer",)),
                before_archive,
            )
            archive_approval = archive_path.with_suffix(".approval.json")
            tool.approve_plan(archive_path, archive_approval, confirmed=True)
            archive_record = tool.apply_plan(root, archive_path, archive_approval)
            self.assertEqual(tool.verify_plan(root, archive_path)["status"], "verified")
            self.assertEqual(
                tool.apply_plan(root, archive_path, archive_approval), archive_record
            )
            after_archive = file_manifest(root, excluded_roots=(".workspace-organizer",))
            self._assert_only_archive_move(
                before_archive,
                after_archive,
                archive_source,
                archive_destination,
            )
            archive_result = archive_record["results"][0]
            self.assertEqual(archive_result["rollback"]["restore_path"], archive_source)
            self.assertEqual(archive_result["rollback"]["archived_path"], archive_destination)
            restored_task = base64.b64decode(
                archive_result["rollback"]["task_record_before_base64"], validate=True
            )
            self.assertEqual(
                hashlib.sha256(restored_task).hexdigest(),
                archive_result["task_record_before_sha256"],
            )
            with self.assertRaises(tool.WorkspaceError):
                tool.build_archive_plan(
                    root,
                    archive_family["id"],
                    "2026-08-19T12:00:00+08:00",
                )
            self.assertEqual(tool.generate_indexes(root)["status"], "verified")
            post_archive_outputs = generated_bytes(root)
            self.assertEqual(tool.generate_indexes(root)["status"], "already_current")
            self.assertEqual(generated_bytes(root), post_archive_outputs)
            for relative in (
                large["path"],
                compressed["path"],
                *exact["paths"],
                *suspected_paths,
                temporary_file["path"],
                nested["protected_file"],
            ):
                self.assertTrue((root / relative).is_file(), relative)

    def test_adopt_in_place_preserves_existing_content_and_archives_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            inbox = root / "10_收件箱"
            inbox.mkdir()
            (inbox / "unclassified note.txt").write_text(
                "Synthetic unclassified note.\n", encoding="utf-8"
            )
            adopted_relative = "Existing Projects/研究 α"
            adopted = root / adopted_relative
            family = {
                "id": "adopted-contract",
                "title": "Adopt a synthetic legacy contract",
                "type": "contract",
                "area": "operations",
                "workspace_status": "completed",
                "priority": "normal",
                "due": None,
                "sensitivity": "internal",
                "next_action": None,
            }
            write_task(adopted, family_task(family))
            (adopted / "records").mkdir()
            (adopted / "records" / "decision record.md").write_text(
                "Synthetic adoption decision.\n", encoding="utf-8"
            )
            material_relative = "Legacy Library/资料 with spaces"
            material = root / material_relative
            material.mkdir(parents=True)
            (material / "public reference.txt").write_text(
                "Synthetic adopted reference.\n", encoding="utf-8"
            )
            unrelated = root / "Unmanaged notes" / "keep me.txt"
            unrelated.parent.mkdir()
            unrelated.write_text("Synthetic unmanaged content.\n", encoding="utf-8")
            nested = root / "third-party" / "repository"
            (nested / ".git").mkdir(parents=True)
            (nested / "leave untouched.txt").write_text(
                "Synthetic nested repository content.\n", encoding="utf-8"
            )

            kwargs = {
                "adopted_task_paths": [adopted_relative],
                "adopted_material_roots": [
                    {"path": material_relative, "sensitivity": "public"}
                ],
                "accepted_existing_managed": ["10_收件箱"],
            }
            first_plan = tool.build_initialization_plan(
                root, "adoption-scenario", **kwargs
            )
            second_plan = tool.build_initialization_plan(
                root, "adoption-scenario", **kwargs
            )
            self.assertEqual(first_plan, second_plan)
            before_adoption = file_manifest(root)
            self._initialize(base, root, "adoption-scenario", **kwargs)
            after_adoption = file_manifest(root)
            for relative, digest in before_adoption.items():
                self.assertEqual(after_adoption[relative], digest, relative)
            config = tool.load_config(root)
            self.assertEqual(config["adopted_task_paths"], [adopted_relative])
            self.assertEqual(
                config["adopted_material_roots"],
                [{"path": material_relative, "sensitivity": "public"}],
            )
            self.assertTrue(adopted.is_dir())
            self.assertTrue(unrelated.is_file())
            self.assertTrue((nested / "leave untouched.txt").is_file())

            self.assertEqual(tool.generate_indexes(root)["status"], "verified")
            adoption_views = generated_bytes(root)
            self.assertEqual(tool.generate_indexes(root)["status"], "already_current")
            self.assertEqual(generated_bytes(root), adoption_views)
            materials = json.loads(
                (root / ".workspace-organizer/catalog/materials.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                f"{material_relative}/public reference.txt",
                {item["path"] for item in materials["items"]},
            )
            self.assertNotIn("leave untouched.txt", json.dumps(materials))

            archive_plan = tool.build_archive_plan(
                root, "adopted-contract", "2026-08-19T11:00:00+08:00"
            )
            duplicate_plan = tool.build_archive_plan(
                root, "adopted-contract", "2026-08-19T11:00:00+08:00"
            )
            self.assertEqual(archive_plan, duplicate_plan)
            archive_path = base / "adopted-archive.json"
            tool.write_immutable_json(archive_path, archive_plan)
            before_archive = file_manifest(root, excluded_roots=(".workspace-organizer",))
            dry = tool.dry_run(root, archive_path)
            self.assertEqual(dry["status"], "ready")
            registration = next(
                item
                for item in dry["intended_mutations"]
                if item["action"] == "update_config_registration"
            )
            self.assertEqual(registration["removed_path"], adopted_relative)
            self.assertEqual(
                file_manifest(root, excluded_roots=(".workspace-organizer",)),
                before_archive,
            )
            approval = archive_path.with_suffix(".approval.json")
            tool.approve_plan(archive_path, approval, confirmed=True)
            record = tool.apply_plan(root, archive_path, approval)
            self.assertEqual(tool.verify_plan(root, archive_path)["status"], "verified")
            destination = "90_归档/2026/operations/adopted-contract"
            after_archive = file_manifest(root, excluded_roots=(".workspace-organizer",))
            self._assert_only_archive_move(
                before_archive,
                after_archive,
                adopted_relative,
                destination,
            )
            result = record["results"][0]
            self.assertEqual(result["config_before"]["adopted_task_paths"], [adopted_relative])
            self.assertEqual(result["config_after"]["adopted_task_paths"], [])
            self.assertEqual(result["rollback"]["mode"], "verified_exact_archive_rollback_only")
            self.assertEqual(tool.load_config(root)["adopted_task_paths"], [])
            self.assertTrue(unrelated.is_file())
            self.assertTrue((nested / "leave untouched.txt").is_file())
            with self.assertRaises(tool.WorkspaceError):
                tool.build_archive_plan(
                    root, "adopted-contract", "2026-08-19T12:00:00+08:00"
                )

    def test_stale_and_collision_cases_leave_manifest_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            self._initialize(base, root, "negative-scenario")
            family = self.scenarios["families"][0]
            write_task(root / "20_任务" / family["id"], family_task(family))

            stale_source = root / "10_收件箱" / "stale source.txt"
            stale_source.write_text("Synthetic approved bytes.\n", encoding="utf-8")
            stale_plan = tool.build_organize_plan(
                root,
                [{
                    "source": "10_收件箱/stale source.txt",
                    "destination": f"20_任务/{family['id']}/inputs/stale source.txt",
                }],
            )
            stale_path = base / "stale.json"
            stale_approval = write_plan(stale_path, stale_plan)
            stale_source.write_text("Synthetic changed bytes.\n", encoding="utf-8")
            before_stale = file_manifest(root)
            self.assertEqual(tool.dry_run(root, stale_path)["status"], "blocked")
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(root, stale_path, stale_approval)
            self.assertEqual(file_manifest(root), before_stale)

            collision_source = root / "10_收件箱" / "collision source.txt"
            collision_source.write_text("Synthetic source.\n", encoding="utf-8")
            collision_relative = f"20_任务/{family['id']}/inputs/collision target.txt"
            collision_plan = tool.build_organize_plan(
                root,
                [{
                    "source": "10_收件箱/collision source.txt",
                    "destination": collision_relative,
                }],
            )
            collision_path = base / "collision.json"
            collision_approval = write_plan(collision_path, collision_plan)
            collision_target = root / collision_relative
            collision_target.parent.mkdir(parents=True, exist_ok=True)
            collision_target.write_text("Synthetic existing target.\n", encoding="utf-8")
            before_collision = file_manifest(root)
            dry = tool.dry_run(root, collision_path)
            self.assertEqual(dry["status"], "blocked")
            self.assertTrue(dry["collisions"])
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(root, collision_path, collision_approval)
            self.assertEqual(file_manifest(root), before_collision)
            self.assertEqual(
                collision_target.read_text(encoding="utf-8"),
                "Synthetic existing target.\n",
            )
            self.assertTrue(collision_source.is_file())

    def test_index_failure_rolls_back_all_six_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            self._initialize(base, root, "index-rollback-scenario")
            family = self.scenarios["families"][0]
            bundle = root / "20_任务" / family["id"]
            write_task(bundle, family_task(family))
            self.assertEqual(tool.generate_indexes(root)["status"], "verified")
            before = generated_bytes(root)

            changed = family_task(family)
            changed["next_action"] = "Review the changed synthetic protocol"
            changed["updated"] = "2026-08-19T09:30:00+08:00"
            write_task(bundle, changed)
            original_install = tool._install_index_target
            calls = {"count": 0}

            def fail_second(*args: Any, **kwargs: Any) -> None:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise tool.WorkspaceError("injected scenario index failure")
                original_install(*args, **kwargs)

            with mock.patch.object(tool, "_install_index_target", side_effect=fail_second):
                with self.assertRaises(tool.WorkspaceError):
                    tool.generate_indexes(root)
            self.assertEqual(calls["count"], 2)
            self.assertEqual(generated_bytes(root), before)
            cache = root / ".workspace-organizer" / "cache"
            self.assertFalse(cache.exists() and any(cache.glob("index-*")))

            self.assertEqual(tool.generate_indexes(root)["status"], "verified")
            after = generated_bytes(root)
            self.assertNotEqual(after["00_总览/TODO.md"], before["00_总览/TODO.md"])
            self.assertEqual(tool.generate_indexes(root)["status"], "already_current")
            self.assertEqual(generated_bytes(root), after)

    def test_public_fixtures_and_requirement_matrix_are_hygienic_and_complete(self) -> None:
        fixture_files = sorted(FIXTURE_ROOT.rglob("*"))
        self.assertTrue(fixture_files)
        self.assertTrue(all(path.suffix == ".json" for path in fixture_files if path.is_file()))
        forbidden = (
            "/" + "Users/",
            "/" + "home/",
            "C:" + "\\Users\\",
            "file:" + "//",
            "gh" + "p_",
            "gh" + "o_",
            "AK" + "IA",
            "-----BEGIN " + "PRIVATE KEY-----",
            "password" + "=",
            "token" + "=",
        )
        for path in fixture_files:
            if not path.is_file():
                continue
            self.assertLess(path.stat().st_size, 64 * 1024, path)
            content = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, content, f"{token!r} found in {path}")
            json.loads(content)

        self.assertEqual(self.scenarios["classification"], "public-synthetic")
        self.assertLessEqual(
            self.scenarios["difficult_cases"]["large_placeholder"]["size"],
            8 * 1024,
            "large-file behavior must use a generated compact placeholder",
        )
        self.assertEqual(validate_requirement_matrix(self.matrix, self.__class__), [])

        mutations = {}
        nonexistent_dotted = copy.deepcopy(self.matrix)
        nonexistent_dotted["epic_success_criteria"][-1]["coverage"][-1] = (
            "tests.test_workspace_model.WorkspaceModelContractTests.test_missing_scenario"
        )
        mutations["nonexistent dotted test"] = (
            nonexistent_dotted,
            "dotted test reference does not resolve exactly one real test",
        )
        unknown_gate = copy.deepcopy(self.matrix)
        unknown_gate["epic_definition_of_done"][0]["coverage"][0] = "gate:unknown"
        mutations["unknown gate"] = (
            unknown_gate,
            "unknown delivery gate reference",
        )
        wrong_id = copy.deepcopy(self.matrix)
        wrong_id["issue_acceptance"][0]["id"] = "ISSUE-5-AC-99"
        mutations["wrong requirement ID"] = (
            wrong_id,
            "IDs do not match the reviewed snapshot",
        )
        changed_criterion = copy.deepcopy(self.matrix)
        changed_criterion["epic_definition_of_done"][-1]["criterion"] += " Changed."
        mutations["changed criterion text"] = (
            changed_criterion,
            "criterion differs from the tracked source text",
        )
        for name, (mutated, expected_error) in mutations.items():
            with self.subTest(rejected_mutation=name):
                errors = validate_requirement_matrix(mutated, self.__class__)
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )


if __name__ == "__main__":
    unittest.main()
