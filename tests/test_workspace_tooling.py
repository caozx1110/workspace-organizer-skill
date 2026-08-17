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


def journal_records(root, plan_id):
    control = root / ".workspace-organizer"
    return sorted(control.rglob(f"{plan_id}.*.json")) if control.exists() else []


def journal_record(root, plan_id, suffix):
    matches = [path for path in journal_records(root, plan_id) if path.name == f"{plan_id}.{suffix}.json"]
    if len(matches) != 1:
        raise AssertionError(f"expected one {suffix} record, found {matches}")
    return json.loads(matches[0].read_text(encoding="utf-8"))


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

    def test_adopted_roots_cannot_overlap_any_managed_role(self):
        base = {
            "schema_version": 1,
            "workspace_id": "synthetic-workspace",
            "default_sensitivity": "internal",
            "adopted_task_paths": [],
            "adopted_material_roots": [],
            "exclude_paths": [],
        }
        for managed in tool.MANAGED_DIRECTORIES:
            task_config = dict(base)
            task_config["adopted_task_paths"] = [f"{managed}/adopted-task"]
            with self.assertRaises(tool.WorkspaceError, msg=managed):
                tool.validate_config(task_config)
            material_config = dict(base)
            material_config["adopted_material_roots"] = [{"path": managed, "sensitivity": "public"}]
            with self.assertRaises(tool.WorkspaceError, msg=managed):
                tool.validate_config(material_config)

    def test_compressed_inspection_bounds_source_central_directory_and_entry_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_workspace(root)
            archive_path = root / "10_收件箱" / "bounded.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("one.txt", "one")
                archive.writestr("two.txt", "two")
            with self.assertRaises(tool.WorkspaceError):
                tool.inspect_compressed(root, "10_收件箱/bounded.zip", confirmed=True, max_entries=1)
            with self.assertRaises(tool.WorkspaceError):
                tool.inspect_compressed(root, "10_收件箱/bounded.zip", confirmed=True, max_metadata_bytes=32)
            with self.assertRaises(tool.WorkspaceError):
                tool.inspect_compressed(
                    root,
                    "10_收件箱/bounded.zip",
                    confirmed=True,
                    max_source_bytes=archive_path.stat().st_size - 1,
                )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_compressed_source_symlink_race_fails_before_external_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            make_workspace(root)
            source = root / "10_收件箱" / "race.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("safe.txt", "approved")
            outside = base / "outside.zip"
            with zipfile.ZipFile(outside, "w") as archive:
                archive.writestr("secret.txt", "external-secret")
            original_open = tool._open_file_descriptor
            raced = {"done": False}

            def race(*args, **kwargs):
                if args[1] == "10_收件箱/race.zip" and not raced["done"]:
                    raced["done"] = True
                    source.unlink()
                    os.symlink(outside, source)
                return original_open(*args, **kwargs)

            with mock.patch.object(tool, "_open_file_descriptor", side_effect=race):
                with self.assertRaises((tool.WorkspaceError, OSError)):
                    tool.inspect_compressed(root, "10_收件箱/race.zip", confirmed=True)
            self.assertTrue(source.is_symlink())
            with zipfile.ZipFile(outside) as archive:
                self.assertIn(b"external-secret", archive.read("secret.txt"))


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
        original = tool._stage_file_at
        calls = {"count": 0}

        def fail_second(source_fd, destination_parent_fd, temporary_name, expected):
            calls["count"] += 1
            if calls["count"] == 2:
                raise tool.WorkspaceError("injected verification interruption")
            return original(source_fd, destination_parent_fd, temporary_name, expected)

        with mock.patch.object(tool, "_stage_file_at", side_effect=fail_second):
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(self.root, plan_path, approval)
        record = journal_record(self.root, tool.load_plan(plan_path)[0]["plan_id"], "result")
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

    def test_intent_and_wal_write_failures_make_no_user_structure_changes(self):
        source = self.root / "10_收件箱" / "input.txt"
        source.write_text("synthetic", encoding="utf-8")
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/input.txt", "destination": "99_待整理/new/input.txt"
        }])
        before = tree_evidence(self.root)
        with mock.patch.object(tool, "_write_intent", side_effect=OSError("injected intent failure")):
            with self.assertRaises(OSError):
                tool.apply_plan(self.root, plan_path, approval)
        self.assertEqual(tree_evidence(self.root), before)
        self.assertEqual(journal_records(self.root, plan["plan_id"]), [])

        with mock.patch.object(tool, "_write_wal_stage", side_effect=OSError("injected WAL failure")):
            with self.assertRaises(OSError):
                tool.apply_plan(self.root, plan_path, approval)
        self.assertTrue(source.is_file())
        self.assertFalse((self.root / "99_待整理" / "new").exists())
        self.assertEqual(journal_record(self.root, plan["plan_id"], "result")["status"], "failed")
        with self.assertRaises(tool.WorkspaceError):
            tool.apply_plan(self.root, plan_path, approval)

    def test_result_write_failure_leaves_discoverable_intent_and_blocks_retry(self):
        source = self.root / "10_收件箱" / "result-failure.txt"
        source.write_text("synthetic", encoding="utf-8")
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/result-failure.txt",
            "destination": "99_待整理/result-failure.txt",
        }])
        with mock.patch.object(tool, "_write_result", side_effect=OSError("injected result failure")):
            with self.assertRaises(OSError):
                tool.apply_plan(self.root, plan_path, approval)
        self.assertFalse(source.exists())
        self.assertTrue((self.root / "99_待整理" / "result-failure.txt").is_file())
        self.assertEqual(journal_record(self.root, plan["plan_id"], "intent")["plan"], plan)
        self.assertFalse(any(path.name.endswith(".result.json") for path in journal_records(self.root, plan["plan_id"])))
        with self.assertRaises(tool.WorkspaceError):
            tool.apply_plan(self.root, plan_path, approval)

    def test_keyboard_interrupt_mid_copy_cleans_temporary_and_preserves_source(self):
        source = self.root / "10_收件箱" / "interrupt.txt"
        source.write_bytes(b"approved-content")
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/interrupt.txt",
            "destination": "20_任务/sample-task/inputs/interrupt.txt",
        }])

        def interrupt_copy(source_fd, destination_fd):
            tool._write_all(destination_fd, b"partial")
            raise KeyboardInterrupt("injected mid-copy interrupt")

        with mock.patch.object(tool, "_copy_fd_data", side_effect=interrupt_copy):
            with self.assertRaises(KeyboardInterrupt):
                tool.apply_plan(self.root, plan_path, approval)
        destination_parent = self.root / "20_任务" / "sample-task" / "inputs"
        self.assertEqual(source.read_bytes(), b"approved-content")
        self.assertFalse((destination_parent / "interrupt.txt").exists())
        self.assertFalse(any(path.name.startswith(".workspace-organizer-") for path in destination_parent.iterdir()))
        self.assertEqual(journal_record(self.root, plan["plan_id"], "result")["status"], "failed")
        with self.assertRaises(tool.WorkspaceError):
            tool.apply_plan(self.root, plan_path, approval)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_source_symlink_race_never_copies_external_content_or_deletes_link(self):
        source = self.root / "10_收件箱" / "race.txt"
        source.write_bytes(b"approved-content")
        outside = self.base / "outside-secret.txt"
        outside.write_bytes(b"external-secret")
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/race.txt", "destination": "99_待整理/race.txt"
        }])
        original_wal = tool._write_wal_stage
        raced = {"done": False}

        def race(journal, sequence, stage, evidence):
            original_wal(journal, sequence, stage, evidence)
            if stage == "stage-file-copy" and not raced["done"]:
                raced["done"] = True
                source.unlink()
                os.symlink(outside, source)

        with mock.patch.object(tool, "_write_wal_stage", side_effect=race):
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(self.root, plan_path, approval)
        self.assertTrue(source.is_symlink())
        self.assertEqual(outside.read_bytes(), b"external-secret")
        self.assertEqual((self.root / "99_待整理" / "race.txt").read_bytes(), b"approved-content")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_destination_parent_symlink_race_never_writes_outside_or_deletes_source(self):
        source = self.root / "10_收件箱" / "destination-race.txt"
        source.write_bytes(b"approved-content")
        outside = self.base / "outside-directory"
        outside.mkdir()
        plan, plan_path, approval = self._plan([{
            "source": "10_收件箱/destination-race.txt",
            "destination": "20_任务/sample-task/inputs/destination-race.txt",
        }])
        original_wal = tool._write_wal_stage
        raced = {"done": False}
        inputs = self.root / "20_任务" / "sample-task" / "inputs"
        held = self.root / "20_任务" / "sample-task" / "inputs-held"

        def race(journal, sequence, stage, evidence):
            original_wal(journal, sequence, stage, evidence)
            if stage == "remove-source-file" and not raced["done"]:
                raced["done"] = True
                inputs.rename(held)
                os.symlink(outside, inputs)

        with mock.patch.object(tool, "_write_wal_stage", side_effect=race):
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(self.root, plan_path, approval)
        self.assertEqual(source.read_bytes(), b"approved-content")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((held / "destination-race.txt").read_bytes(), b"approved-content")


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
            with mock.patch.object(tool, "_remove_approved_tree_at", side_effect=tool.WorkspaceError("injected stop")):
                with self.assertRaises(tool.WorkspaceError):
                    tool.apply_plan(self.root, plan_path, approval)
            self.assertTrue(bundle.exists())
            failed = journal_record(self.root, plan["plan_id"], "result")
            self.assertEqual(failed["status"], "failed")
            self.assertIn(plan["source"], failed["referenced_paths"])
        finally:
            plan_path.unlink(missing_ok=True)
            plan_path.with_suffix(".approval.json").unlink(missing_ok=True)

    def test_materials_use_nfc_codepoint_order_and_exclude_fixed_sensitive_roles(self):
        write_task(self.root / "20_任务" / "sample-task", task_data("sample-task", sensitivity="public"))
        library = self.root / "30_资料库"
        (library / "a.txt").write_text("a", encoding="utf-8")
        (library / "Z.txt").write_text("Z", encoding="utf-8")
        (self.root / "10_收件箱" / "inbox-secret.txt").write_text("secret", encoding="utf-8")
        (self.root / ".workspace-organizer" / "control-secret.txt").write_text("secret", encoding="utf-8")
        outputs = tool.build_generated_outputs(self.root)
        catalog = json.loads(outputs[".workspace-organizer/catalog/materials.json"].decode("utf-8"))
        paths = [item["path"] for item in catalog["items"]]
        self.assertLess(paths.index("30_资料库/Z.txt"), paths.index("30_资料库/a.txt"))
        self.assertFalse(any("inbox-secret" in path or "control-secret" in path for path in paths))

    def test_archive_plan_rejects_title_body_and_other_metadata_tampering(self):
        bundle = self.root / "20_任务" / "closed-task"
        write_task(bundle, task_data("closed-task", status="completed", due=None))
        plan = tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        before_after = base64.b64decode(plan["task_record_after_base64"])
        after_data, after_body = tool.parse_task_bytes(before_after)
        mutations = []
        changed_title = dict(after_data)
        changed_title["title"] = "Tampered title"
        mutations.append(tool.serialize_task(changed_title, after_body))
        changed_due = dict(after_data)
        changed_due["due"] = "2027-01-01"
        mutations.append(tool.serialize_task(changed_due, after_body))
        mutations.append(tool.serialize_task(after_data, after_body + "tampered body\n"))
        for index, after_bytes in enumerate(mutations):
            malicious = dict(plan)
            malicious["task_record_after_base64"] = base64.b64encode(after_bytes).decode("ascii")
            malicious["task_record_after_sha256"] = tool._sha256_bytes(after_bytes)
            malicious = tool._plan_with_id({key: value for key, value in malicious.items() if key != "plan_id"})
            path = Path(self.temporary.name).parent / f"tampered-archive-{index}-{next(tempfile._get_candidate_names())}.json"
            try:
                tool.write_immutable_json(path, malicious)
                with self.assertRaises(tool.WorkspaceError):
                    tool.approve_plan(path, path.with_suffix(".approval.json"), confirmed=True)
            finally:
                path.unlink(missing_ok=True)
                path.with_suffix(".approval.json").unlink(missing_ok=True)

    def test_archive_keyboard_interrupt_cleans_partial_temporary_tree(self):
        bundle = self.root / "20_任务" / "closed-task"
        write_task(bundle, task_data("closed-task", status="completed", area="research", due=None))
        (bundle / "records").mkdir()
        (bundle / "records" / "large.bin").write_bytes(b"approved-content")
        plan = tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        plan_path = Path(self.temporary.name).parent / f"archive-interrupt-{next(tempfile._get_candidate_names())}.json"
        try:
            approval = save_plan(plan_path, plan)

            def interrupt_copy(source_fd, destination_fd):
                tool._write_all(destination_fd, b"partial")
                raise KeyboardInterrupt("injected archive copy interruption")

            with mock.patch.object(tool, "_copy_fd_data", side_effect=interrupt_copy):
                with self.assertRaises(KeyboardInterrupt):
                    tool.apply_plan(self.root, plan_path, approval)
            destination_parent = self.root / "90_归档" / "2026" / "research"
            self.assertTrue(bundle.is_dir())
            self.assertFalse((destination_parent / "closed-task").exists())
            self.assertFalse(any(path.name.startswith(".workspace-organizer-") for path in destination_parent.iterdir()))
            self.assertEqual(journal_record(self.root, plan["plan_id"], "result")["status"], "failed")
        finally:
            plan_path.unlink(missing_ok=True)
            plan_path.with_suffix(".approval.json").unlink(missing_ok=True)

    def test_adopted_archive_config_delete_gap_is_durable_and_blocks_retry(self):
        adopted = self.root / "Legacy Tasks" / "adopted-task"
        write_task(adopted, task_data("adopted-task", status="completed", area="research", due=None))
        (adopted / "records").mkdir()
        (adopted / "records" / "decision.txt").write_text("approved", encoding="utf-8")
        config_path = self.root / ".workspace-organizer" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["adopted_task_paths"] = ["Legacy Tasks/adopted-task"]
        config_path.write_bytes(tool._pretty_json(config))
        plan = tool.build_archive_plan(self.root, "adopted-task", "2026-08-17T11:00:00+08:00")
        plan_path = Path(self.temporary.name).parent / f"adopted-gap-{next(tempfile._get_candidate_names())}.json"
        try:
            approval = save_plan(plan_path, plan)
            with mock.patch.object(
                tool, "_remove_approved_tree_at", side_effect=KeyboardInterrupt("injected config/delete gap")
            ):
                with self.assertRaises(KeyboardInterrupt):
                    tool.apply_plan(self.root, plan_path, approval)
            destination = self.root / "90_归档" / "2026" / "research" / "adopted-task"
            self.assertTrue(adopted.is_dir())
            self.assertTrue(destination.is_dir())
            self.assertNotIn("Legacy Tasks/adopted-task", tool.load_config(self.root)["adopted_task_paths"])
            result = journal_record(self.root, plan["plan_id"], "result")
            self.assertEqual(result["status"], "failed")
            wal_stages = [
                json.loads(path.read_text(encoding="utf-8"))["stage"]
                for path in journal_records(self.root, plan["plan_id"])
                if ".wal-" in path.name
            ]
            self.assertIn("adopted-config-verified", wal_stages)
            self.assertIn("remove-archive-source", wal_stages)
            with self.assertRaises(tool.WorkspaceError):
                tool.apply_plan(self.root, plan_path, approval)
        finally:
            plan_path.unlink(missing_ok=True)
            plan_path.with_suffix(".approval.json").unlink(missing_ok=True)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_archive_destination_parent_race_never_writes_outside_or_deletes_source(self):
        bundle = self.root / "20_任务" / "closed-task"
        write_task(bundle, task_data("closed-task", status="completed", area="research", due=None))
        (bundle / "records").mkdir()
        (bundle / "records" / "decision.txt").write_text("approved", encoding="utf-8")
        plan = tool.build_archive_plan(self.root, "closed-task", "2026-08-17T11:00:00+08:00")
        plan_path = Path(self.temporary.name).parent / f"archive-race-{next(tempfile._get_candidate_names())}.json"
        outside = Path(self.temporary.name).parent / f"outside-{next(tempfile._get_candidate_names())}"
        outside.mkdir()
        destination_parent = self.root / "90_归档" / "2026" / "research"
        held_parent = self.root / "90_归档" / "2026" / "research-held"
        original_wal = tool._write_wal_stage
        raced = {"done": False}

        def race(journal, sequence, stage, evidence):
            original_wal(journal, sequence, stage, evidence)
            if stage == "remove-archive-source" and not raced["done"]:
                raced["done"] = True
                destination_parent.rename(held_parent)
                os.symlink(outside, destination_parent)

        try:
            approval = save_plan(plan_path, plan)
            with mock.patch.object(tool, "_write_wal_stage", side_effect=race):
                with self.assertRaises(tool.WorkspaceError):
                    tool.apply_plan(self.root, plan_path, approval)
            self.assertTrue(bundle.is_dir())
            self.assertEqual(list(outside.iterdir()), [])
            self.assertTrue((held_parent / "closed-task" / "TASK.md").is_file())
            self.assertEqual(journal_record(self.root, plan["plan_id"], "result")["status"], "failed")
        finally:
            plan_path.unlink(missing_ok=True)
            plan_path.with_suffix(".approval.json").unlink(missing_ok=True)
            outside.rmdir()


if __name__ == "__main__":
    unittest.main()
