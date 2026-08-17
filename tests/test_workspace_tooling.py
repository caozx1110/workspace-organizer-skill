import base64
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "skill" / "workspace-organizer" / "scripts" / "workspace_organizer.py"
SPEC = importlib.util.spec_from_file_location("workspace_organizer_tooling", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tool)


def task_data(
    task_id="sample-task",
    *,
    status="active",
    sensitivity="internal",
    area="general",
    due="2026-09-30",
):
    closed = "2026-08-17T10:00:00+08:00" if status in {"completed", "cancelled", "archived"} else None
    archived = "2026-08-17T11:00:00+08:00" if status == "archived" else None
    updated = archived or closed or "2026-08-17T09:00:00+08:00"
    return {
        "schema_version": 1,
        "id": task_id,
        "title": f"Synthetic {task_id}",
        "status": status,
        "area": area,
        "type": "research",
        "priority": "normal",
        "due": due,
        "sensitivity": sensitivity,
        "next_action": "Review synthetic inputs" if status in tool.OPEN_STATUSES else None,
        "updated": updated,
        "closed_at": closed,
        "archived_at": archived,
        "tags": ["synthetic"],
    }


def write_task(bundle, data):
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "TASK.md").write_bytes(tool.serialize_task(data, f"# {data['title']}\n"))


def make_workspace(root, *, config_overrides=None):
    for relative in tool.MANAGED_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "workspace_id": "synthetic-workspace",
        "default_sensitivity": "internal",
        "adopted_task_paths": [],
        "adopted_material_roots": [],
        "exclude_paths": [],
    }
    if config_overrides:
        config.update(config_overrides)
    (root / ".workspace-organizer" / "config.json").write_bytes(tool._pretty_json(config))
    return config


def save_plan(path, plan):
    tool.write_immutable_json(path, plan)
    approval = path.with_suffix(".approval.json")
    tool.approve_plan(path, approval, confirmed=True)
    return approval


def tree_evidence(root):
    evidence = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            evidence[path.relative_to(root).as_posix()] = ("symlink", os.readlink(path))
        elif path.is_file():
            evidence[path.relative_to(root).as_posix()] = ("file", path.read_bytes())
        elif path.is_dir():
            evidence[path.relative_to(root).as_posix()] = ("directory", None)
    return evidence


class InitializationAndScanTests(unittest.TestCase):
    def test_initialize_adopts_exact_roots_without_moving_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            adopted = root / "Legacy Tasks" / "研究 项目"
            write_task(adopted, task_data("legacy-research"))
            material = root / "Legacy Library"
            material.mkdir()
            (material / "note.txt").write_text("synthetic", encoding="utf-8")
            plan = tool.build_initialization_plan(
                root,
                "adopted-workspace",
                adopted_task_paths=["Legacy Tasks/研究 项目"],
                adopted_material_roots=[{"path": "Legacy Library", "sensitivity": "internal"}],
            )
            plan_path = base / "init-plan.json"
            approval = save_plan(plan_path, plan)
            before = tree_evidence(root)
            dry = tool.dry_run(root, plan_path)
            self.assertFalse(dry["mutated"])
            self.assertEqual(tree_evidence(root), before)
            result = tool.apply_plan(root, plan_path, approval)
            self.assertEqual(result["status"], "verified")
            self.assertTrue(adopted.is_dir())
            self.assertEqual((material / "note.txt").read_text(encoding="utf-8"), "synthetic")
            config = tool.load_config(root)
            self.assertEqual(config["adopted_task_paths"], ["Legacy Tasks/研究 项目"])
            self.assertEqual(tool.apply_plan(root, plan_path, approval), result)
            self.assertEqual(tool.verify_plan(root, plan_path)["status"], "verified")

    def test_initialize_rejects_unaccepted_managed_name_and_stale_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            (root / "10_收件箱").mkdir()
            with self.assertRaises(tool.WorkspaceError):
                tool.build_initialization_plan(root, "collision-workspace")
            plan = tool.build_initialization_plan(
                root, "collision-workspace", accepted_existing_managed=["10_收件箱"]
            )
            plan_path = base / "plan.json"
            approval = save_plan(plan_path, plan)
            (root / "changed.txt").write_text("changed", encoding="utf-8")
            before = tree_evidence(root)
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(root, plan_path, approval)
            self.assertEqual(tree_evidence(root), before)

    def test_inventory_scan_is_stable_conservative_and_skips_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox = root / "10_收件箱"
            inbox.mkdir()
            (inbox / "资料 with space.txt").write_text("same", encoding="utf-8")
            (inbox / "duplicate.txt").write_text("same", encoding="utf-8")
            (inbox / "large.bin").write_bytes(b"12345")
            (inbox / "original.zip").write_bytes(b"not-opened")
            nested = root / "vendor"
            (nested / ".git").mkdir(parents=True)
            (nested / "secret.txt").write_text("not scanned", encoding="utf-8")
            if hasattr(os, "symlink"):
                os.symlink(root / "outside", root / "link")
            first = tool.scan_workspace(root, hash_limit=4)
            second = tool.scan_workspace(root, hash_limit=4)
            self.assertEqual(first, second)
            by_path = {item["path"]: item for item in first["entries"]}
            self.assertEqual(by_path["10_收件箱/original.zip"]["hash_state"], "metadata_only")
            self.assertIsNone(by_path["10_收件箱/original.zip"]["sha256"])
            self.assertEqual(by_path["10_收件箱/large.bin"]["hash_state"], "deferred_large_file")
            self.assertTrue(any(item["reason"] == "nested_git" for item in first["boundaries"]))
            self.assertNotIn("vendor/secret.txt", by_path)
            self.assertTrue(first["duplicate_candidates"])
            self.assertTrue(all(item["destination"] is None for item in first["proposals"]))
            self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])

    def test_compressed_inspection_requires_opt_in_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_workspace(root)
            safe = root / "10_收件箱" / "safe.zip"
            with zipfile.ZipFile(safe, "w") as archive:
                archive.writestr("folder/资料.txt", "synthetic")
            original = safe.read_bytes()
            with self.assertRaises(tool.WorkspaceError):
                tool.inspect_compressed(root, "10_收件箱/safe.zip", confirmed=False)
            result = tool.inspect_compressed(root, "10_收件箱/safe.zip", confirmed=True)
            self.assertFalse(result["content_extracted"])
            self.assertFalse(result["source_mutated"])
            self.assertEqual(safe.read_bytes(), original)
            unsafe = root / "10_收件箱" / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape.txt", "synthetic")
            with self.assertRaises(tool.WorkspaceError):
                tool.inspect_compressed(root, "10_收件箱/unsafe.zip", confirmed=True)


class OrganizePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        make_workspace(self.root)
        write_task(self.root / "20_任务" / "sample-task", task_data())

    def tearDown(self):
        self.temporary.cleanup()

    def _plan(self, moves, **kwargs):
        plan = tool.build_organize_plan(self.root, moves, **kwargs)
        path = self.base / f"plan-{len(list(self.base.glob('plan-*.json')))}.json"
        approval = save_plan(path, plan)
        return plan, path, approval

    def test_exact_approval_dry_run_apply_hash_evidence_and_idempotency(self):
        source = self.root / "10_收件箱" / "资料 with space.txt"
        source.write_bytes(b"synthetic-content")
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/资料 with space.txt",
            "destination": "20_任务/sample-task/inputs/资料 with space.txt",
        }])
        before = tree_evidence(self.root)
        self.assertEqual(tool.dry_run(self.root, plan_path)["status"], "ready")
        self.assertEqual(tree_evidence(self.root), before)
        record = tool.apply_plan(self.root, plan_path, approval)
        destination = self.root / "20_任务" / "sample-task" / "inputs" / "资料 with space.txt"
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b"synthetic-content")
        evidence = record["results"][0]
        self.assertEqual(evidence["source_snapshot"]["sha256"], tool._sha256_file(destination))
        self.assertTrue(evidence["post_apply"]["verified"])
        self.assertEqual(tool.apply_plan(self.root, plan_path, approval), record)
        self.assertEqual(tool.verify_plan(self.root, plan_path)["status"], "verified")
        self.assertEqual(plan["operations"][0]["source_snapshot"]["sha256"], tool._sha256_bytes(b"synthetic-content"))

    def test_stale_or_changed_plan_and_existing_destination_do_not_mutate(self):
        source = self.root / "10_收件箱" / "input.txt"
        source.write_text("before", encoding="utf-8")
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/input.txt",
            "destination": "20_任务/sample-task/inputs/input.txt",
        }])
        source.write_text("after", encoding="utf-8")
        before = tree_evidence(self.root)
        with self.assertRaises(tool.WorkspaceError):
            tool.apply_plan(self.root, plan_path, approval)
        self.assertEqual(tree_evidence(self.root), before)
        source.write_text("before", encoding="utf-8")
        destination = self.root / "20_任务" / "sample-task" / "inputs" / "input.txt"
        destination.parent.mkdir()
        destination.write_text("occupied", encoding="utf-8")
        dry = tool.dry_run(self.root, plan_path)
        self.assertEqual(dry["status"], "blocked")
        self.assertTrue(dry["collisions"])
        self.assertEqual(destination.read_text(encoding="utf-8"), "occupied")

    def test_approval_is_bound_to_exact_plan_bytes(self):
        source = self.root / "10_收件箱" / "input.txt"
        source.write_text("synthetic", encoding="utf-8")
        _, plan_path, approval = self._plan([{
            "source": "10_收件箱/input.txt",
            "destination": "99_待整理/input.txt",
        }])
        plan_path.write_bytes(plan_path.read_bytes() + b"\n")
        before = tree_evidence(self.root)
        with self.assertRaises(tool.WorkspaceError):
            tool.apply_plan(self.root, plan_path, approval)
        self.assertEqual(tree_evidence(self.root), before)

    def test_symlink_nested_git_exclusion_compressed_and_normalized_collisions_fail_closed(self):
        excluded = self.root / "excluded"
        excluded.mkdir()
        (excluded / "file.txt").write_text("synthetic", encoding="utf-8")
        config_path = self.root / ".workspace-organizer" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["exclude_paths"] = ["excluded"]
        config_path.write_bytes(tool._pretty_json(config))
        with self.assertRaises(tool.WorkspaceError):
            tool.build_organize_plan(self.root, [{"source": "excluded/file.txt", "destination": "99_待整理/file.txt"}])
        nested = self.root / "vendor"
        (nested / ".git").mkdir(parents=True)
        (nested / "file.txt").write_text("synthetic", encoding="utf-8")
        with self.assertRaises(tool.WorkspaceError):
            tool.build_organize_plan(self.root, [{"source": "vendor/file.txt", "destination": "99_待整理/vendor.txt"}])
        if hasattr(os, "symlink"):
            os.symlink(self.root / "10_收件箱", self.root / "linked-inbox")
            with self.assertRaises(tool.WorkspaceError):
                tool.build_organize_plan(self.root, [{"source": "linked-inbox/nope.txt", "destination": "99_待整理/nope.txt"}])
        compressed = self.root / "10_收件箱" / "original.zip"
        compressed.write_bytes(b"synthetic")
        with self.assertRaises(tool.WorkspaceError):
            tool.build_organize_plan(self.root, [{"source": "10_收件箱/original.zip", "destination": "99_待整理/original.zip"}])
        (self.root / "10_收件箱" / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "10_收件箱" / "b.txt").write_text("b", encoding="utf-8")
        with self.assertRaises(tool.WorkspaceError):
            tool.build_organize_plan(self.root, [
                {"source": "10_收件箱/a.txt", "destination": "30_资料库/Folder/a.txt"},
                {"source": "10_收件箱/b.txt", "destination": "30_资料库/folder/b.txt"},
            ])

    def test_partial_failure_records_completed_hashes_and_blocks_retry(self):
        for name in ("one.txt", "two.txt"):
            (self.root / "10_收件箱" / name).write_text(name, encoding="utf-8")
        _, plan_path, approval = self._plan([
            {"source": "10_收件箱/one.txt", "destination": "20_任务/sample-task/inputs/one.txt"},
            {"source": "10_收件箱/two.txt", "destination": "20_任务/sample-task/inputs/two.txt"},
        ])
        original = tool._move_file_verified
        calls = {"count": 0}

        def fail_second(source, destination, expected):
            calls["count"] += 1
            if calls["count"] == 2:
                raise tool.WorkspaceError("injected verification interruption")
            return original(source, destination, expected)

        with mock.patch.object(tool, "_move_file_verified", side_effect=fail_second):
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(self.root, plan_path, approval)
        verification = self.root / ".workspace-organizer" / "verification"
        record = json.loads(next(verification.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(len(record["results"]), 1)
        self.assertTrue(record["results"][0]["post_apply"]["verified"])
        self.assertFalse((self.root / "10_收件箱" / "one.txt").exists())
        self.assertTrue((self.root / "10_收件箱" / "two.txt").exists())
        with self.assertRaises(tool.WorkspaceError):
            tool.apply_plan(self.root, plan_path, approval)

    def test_handcrafted_protected_plan_and_control_symlink_are_rejected(self):
        source = self.root / "10_收件箱" / "input.txt"
        source.write_text("synthetic", encoding="utf-8")
        valid = tool.build_organize_plan(self.root, [{
            "source": "10_收件箱/input.txt", "destination": "99_待整理/input.txt"
        }])
        malicious = dict(valid)
        malicious["operations"] = [dict(valid["operations"][0])]
        malicious["operations"][0]["source"] = ".workspace-organizer/config.json"
        malicious["operations"][0]["source_snapshot"] = tool._snapshot_file(
            self.root / ".workspace-organizer" / "config.json"
        )
        malicious = tool._plan_with_id(malicious)
        plan_path = self.base / "handcrafted.json"
        tool.write_immutable_json(plan_path, malicious)
        approval = plan_path.with_suffix(".approval.json")
        before = tree_evidence(self.root)
        with self.assertRaises(tool.WorkspaceError):
            tool.approve_plan(plan_path, approval, confirmed=True)
        self.assertEqual(tree_evidence(self.root), before)
        plan_path.unlink()
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/input.txt", "destination": "99_待整理/input.txt"
        }])
        outside = self.base / "outside"
        outside.mkdir()
        os.symlink(outside, self.root / ".workspace-organizer" / "verification")
        with self.assertRaises(tool.WorkspaceError):
            tool.apply_plan(self.root, plan_path, approval)
        self.assertTrue(source.is_file())
        self.assertEqual(list(outside.iterdir()), [])


class IndexAndArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        make_workspace(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_indexes_are_deterministic_filter_sensitive_and_preserve_unknown_roles(self):
        public = self.root / "20_任务" / "public-task"
        write_task(public, task_data("public-task", sensitivity="public"))
        (public / "inputs").mkdir()
        (public / "inputs" / "资料 | note.txt").write_text("synthetic", encoding="utf-8")
        (public / "unknown").mkdir()
        (public / "unknown" / "not-indexed.txt").write_text("hidden", encoding="utf-8")
        confidential = self.root / "20_任务" / "confidential-task"
        write_task(confidential, task_data("confidential-task", sensitivity="confidential"))
        (confidential / "inputs").mkdir()
        (confidential / "inputs" / "secret-name.txt").write_text("secret", encoding="utf-8")
        library = self.root / "30_资料库" / "template.txt"
        library.write_text("template", encoding="utf-8")
        result = tool.generate_indexes(self.root)
        self.assertEqual(result["status"], "verified")
        todo = json.loads((self.root / ".workspace-organizer/catalog/todo.json").read_text(encoding="utf-8"))
        materials = json.loads((self.root / ".workspace-organizer/catalog/materials.json").read_text(encoding="utf-8"))
        rendered = "\n".join(path.read_text(encoding="utf-8") for path in (self.root / "00_总览").iterdir())
        self.assertEqual([item["id"] for item in todo["items"]], ["public-task"])
        material_paths = [item["path"] for item in materials["items"]]
        self.assertIn("20_任务/public-task/inputs/资料 | note.txt", material_paths)
        self.assertIn("30_资料库/template.txt", material_paths)
        self.assertNotIn("not-indexed.txt", rendered)
        self.assertNotIn("secret-name.txt", rendered)
        before = tree_evidence(self.root)
        second = tool.generate_indexes(self.root)
        self.assertEqual(second["status"], "already_current")
        self.assertEqual(tree_evidence(self.root), before)

    def test_generator_matches_normative_golden_workspace(self):
        source = REPO_ROOT / "examples" / "workspace"
        workspace = self.root / "golden-copy"
        shutil.copytree(source, workspace)
        expected = {
            relative: workspace.joinpath(*Path(relative).parts).read_bytes()
            for pair in tool.GENERATED_PATHS.values()
            for relative in pair
        }
        tool.generate_indexes(workspace)
        actual = {
            relative: workspace.joinpath(*Path(relative).parts).read_bytes()
            for relative in expected
        }
        self.assertEqual(actual, expected)

    def test_atomic_index_failure_and_user_owned_overview_preserve_prior_set(self):
        bundle = self.root / "20_任务" / "sample-task"
        data = task_data()
        write_task(bundle, data)
        tool.generate_indexes(self.root)
        generated_paths = [self.root.joinpath(*Path(relative).parts) for pair in tool.GENERATED_PATHS.values() for relative in pair]
        prior = {path: path.read_bytes() for path in generated_paths}
        data["next_action"] = "Changed synthetic action"
        data["updated"] = "2026-08-17T09:30:00+08:00"
        write_task(bundle, data)
        real_replace = tool._replace
        calls = {"count": 0}

        def fail_mid_commit(source, destination):
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("injected index interruption")
            real_replace(source, destination)

        with mock.patch.object(tool, "_replace", side_effect=fail_mid_commit):
            with self.assertRaises(OSError):
                tool.generate_indexes(self.root)
        self.assertEqual({path: path.read_bytes() for path in generated_paths}, prior)
        user_overview = self.root / "00_总览" / "TODO.md"
        user_overview.write_text("# User TODO\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in generated_paths}
        with self.assertRaises(tool.WorkspaceError):
            tool.generate_indexes(self.root)
        self.assertEqual({path: path.read_bytes() for path in generated_paths}, before)

    def test_archive_eligibility_apply_verification_and_rollback_evidence(self):
        bundle = self.root / "20_任务" / "closed-task"
        write_task(bundle, task_data("closed-task", status="completed", area="research", due=None))
        (bundle / "records").mkdir()
        material = bundle / "records" / "decision.txt"
        material.write_bytes(b"synthetic-decision")
        (bundle / "pending").mkdir()
        source_hash = tool._sha256_file(material)
        plan = tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        plan_path = Path(self.temporary.name).parent / f"archive-{next(tempfile._get_candidate_names())}.json"
        try:
            approval = save_plan(plan_path, plan)
            before = tree_evidence(self.root)
            self.assertFalse(tool.dry_run(self.root, plan_path)["mutated"])
            self.assertEqual(tree_evidence(self.root), before)
            record = tool.apply_plan(self.root, plan_path, approval)
            destination = self.root / "90_归档" / "2026" / "research" / "closed-task"
            self.assertFalse(bundle.exists())
            self.assertEqual(tool._sha256_file(destination / "records" / "decision.txt"), source_hash)
            archived, _ = tool.parse_task_bytes((destination / "TASK.md").read_bytes())
            self.assertEqual(archived["status"], "archived")
            self.assertEqual(archived["archived_at"], "2026-08-17T11:00:00+08:00")
            rollback = record["results"][0]["rollback"]
            restored = base64.b64decode(rollback["task_record_before_base64"])
            restored_data, _ = tool.parse_task_bytes(restored)
            self.assertEqual(restored_data["status"], "completed")
            self.assertEqual(tool.apply_plan(self.root, plan_path, approval), record)
            self.assertEqual(tool.verify_plan(self.root, plan_path)["status"], "verified")
        finally:
            plan_path.unlink(missing_ok=True)
            plan_path.with_suffix(".approval.json").unlink(missing_ok=True)

    def test_archive_rejects_unassigned_pending_nested_git_stale_and_failed_verification(self):
        bundle = self.root / "20_任务" / "closed-task"
        write_task(bundle, task_data("closed-task", status="completed", due=None))
        (bundle / "loose.txt").write_text("synthetic", encoding="utf-8")
        with self.assertRaises(tool.WorkspaceError):
            tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        (bundle / "loose.txt").unlink()
        (bundle / "pending").mkdir()
        (bundle / "pending" / "decision.txt").write_text("synthetic", encoding="utf-8")
        with self.assertRaises(tool.WorkspaceError):
            tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        (bundle / "pending" / "decision.txt").unlink()
        nested = bundle / "records" / "vendor"
        (nested / ".git").mkdir(parents=True)
        with self.assertRaises(tool.WorkspaceError):
            tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        (nested / ".git").rmdir()
        nested.rmdir()
        (bundle / "records").rmdir()
        plan = tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        plan_path = Path(self.temporary.name).parent / f"archive-fail-{next(tempfile._get_candidate_names())}.json"
        try:
            approval = save_plan(plan_path, plan)
            (bundle / "records").mkdir()
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(self.root, plan_path, approval)
            self.assertTrue(bundle.exists())
            (bundle / "records").rmdir()
            with mock.patch.object(tool, "_remove_empty_tree_after_verified_copy", side_effect=tool.WorkspaceError("injected stop")):
                with self.assertRaises(tool.WorkspaceError):
                    tool.apply_plan(self.root, plan_path, approval)
            self.assertTrue(bundle.exists())
            verification = self.root / ".workspace-organizer" / "verification" / f"{plan['plan_id']}.json"
            failed = json.loads(verification.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "failed")
            self.assertIn(plan["source"], failed["referenced_paths"])
        finally:
            plan_path.unlink(missing_ok=True)
            plan_path.with_suffix(".approval.json").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
