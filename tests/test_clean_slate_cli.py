"""End-to-end tests for the clean-slate workspace-organizer CLI.

These tests intentionally invoke the command line entry point in a separate
Python process.  The CLI is the boundary used by Chat/Agent integrations, so
testing only the domain model would miss receipt, plan/approval, and filesystem
safety regressions.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "skill" / "workspace-organizer" / "scripts" / "clean_slate.py"
SCRIPTS = CLI.parent
sys.path.insert(0, str(SCRIPTS))

import clean_slate_model as model  # noqa: E402


class CleanSlateCliE2ETests(unittest.TestCase):
    """Exercise the public JSON CLI against disposable workspaces."""

    def setUp(self) -> None:
        # Keep all generated paths short and local.  In particular, do not
        # create case-only siblings such as task.md/TASK.md: the test suite is
        # also run on case-insensitive macOS filesystems.
        self._temporary = tempfile.TemporaryDirectory(prefix="workspace-organizer-cli-")
        self.root = Path(self._temporary.name) / "workspace"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_cli(
        self,
        *arguments: str,
        check: bool = True,
    ) -> Tuple[subprocess.CompletedProcess[str], Optional[Dict[str, Any]]]:
        process = subprocess.run(
            [sys.executable, "-B", str(CLI), *arguments],
            cwd=str(REPO_ROOT),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        payload: Optional[Dict[str, Any]] = None
        if process.stdout.strip():
            try:
                decoded = json.loads(process.stdout)
            except json.JSONDecodeError as exc:  # pragma: no cover - useful failure detail
                self.fail(
                    "CLI did not emit JSON\n"
                    f"command: {arguments!r}\nstdout: {process.stdout!r}\n"
                    f"stderr: {process.stderr!r}\nerror: {exc}"
                )
            self.assertIsInstance(decoded, dict)
            payload = decoded
        if check and process.returncode != 0:
            self.fail(
                "CLI command failed\n"
                f"command: {arguments!r}\nreturncode: {process.returncode}\n"
                f"stdout: {process.stdout}\nstderr: {process.stderr}"
            )
        return process, payload

    def init_workspace(self, *, workspace_id: str = "cli-e2e") -> Dict[str, Any]:
        _, result = self.run_cli("init", str(self.root), "--workspace-id", workspace_id, "--yes")
        assert result is not None
        self.assertEqual(result["status"], "initialized")
        return result

    def create_task(
        self,
        task_id: str,
        title: str,
        *,
        status: str = "planned",
        sensitivity: Optional[str] = None,
        scheduled_on: Optional[str] = None,
        due_on: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Dict[str, Any]:
        args = [
            "task",
            "create",
            str(self.root),
            "--id",
            task_id,
            "--title",
            title,
            "--status",
            status,
        ]
        if sensitivity is not None:
            args.extend(("--sensitivity", sensitivity))
        if scheduled_on is not None:
            args.extend(("--scheduled-on", scheduled_on))
        if due_on is not None:
            args.extend(("--due-on", due_on))
        if body is not None:
            args.extend(("--body", body))
        args.append("--yes")
        _, result = self.run_cli(*args)
        assert result is not None
        self.assertEqual(result["status"], "created")
        return result

    def approve(self, plan: Path, approval: Path) -> Dict[str, Any]:
        _, result = self.run_cli(
            "approve",
            "--plan",
            str(plan),
            "--output",
            str(approval),
            "--yes",
        )
        assert result is not None
        self.assertEqual(result["status"], "approved")
        return result

    def rewrite_plan(self, plan: Path, mutate: Any) -> Dict[str, Any]:
        """Mutate a plan and recompute its digest as an adversary would.

        Keeping the digest self-consistent is important for regression tests:
        an apply failure must come from the operation's semantic/path checks,
        rather than only from the outer approval byte binding.
        """

        value = json.loads(plan.read_text(encoding="utf-8"))
        self.assertIsInstance(value, dict)
        mutate(value)
        value.pop("plan_digest", None)
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        value["plan_digest"] = hashlib.sha256(canonical).hexdigest()
        plan.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return value

    def prepare_closed_task(self, task_id: str) -> Tuple[Path, Path]:
        """Create, close, plan, and approve a task archive operation."""

        self.create_task(task_id, "Closed task", status="active")
        self.run_cli(
            "task",
            "complete",
            str(self.root),
            "--task-id",
            task_id,
            "--summary",
            "Ready for archive regression.",
        )
        plan = self.root / (task_id + ".archive.plan.json")
        self.run_cli(
            "archive",
            "plan",
            str(self.root),
            "--task-id",
            task_id,
            "--output",
            str(plan),
        )
        approval = self.root / (task_id + ".archive.approval.json")
        self.approve(plan, approval)
        return plan, approval

    def prepare_closed_task_with_artifact(
        self, task_id: str
    ) -> Tuple[Path, Path, bytes, Dict[str, Any]]:
        """Create a closed task whose bundle contains one valid Artifact."""

        self.create_task(task_id, "Artifact task", status="active")
        source = self.root.parent / (task_id + "-source.bin")
        source_bytes = (task_id + " artifact bytes\n").encode("utf-8")
        source.write_bytes(source_bytes)
        capture_id = task_id + "-capture"
        self.run_cli(
            "capture",
            "create",
            str(self.root),
            "--id",
            capture_id,
            "--title",
            "Artifact source",
            "--file",
            str(source),
            "--yes",
        )
        triage_plan = self.root / (task_id + ".triage.plan.json")
        self.run_cli(
            "capture",
            "triage",
            str(self.root),
            "--capture-id",
            capture_id,
            "--disposition",
            "artifact",
            "--task-id",
            task_id,
            "--output",
            str(triage_plan),
        )
        triage_approval = self.root / (task_id + ".triage.approval.json")
        self.approve(triage_plan, triage_approval)
        triaged = self.run_cli(
            "capture",
            "triage-apply",
            str(self.root),
            "--plan",
            str(triage_plan),
            "--approval",
            str(triage_approval),
        )[1]
        assert triaged is not None
        artifact = triaged["artifact"]
        self.run_cli(
            "task",
            "complete",
            str(self.root),
            "--task-id",
            task_id,
            "--summary",
            "Ready for artifact archive regression.",
        )
        archive_plan = self.root / (task_id + ".archive.plan.json")
        self.run_cli(
            "archive",
            "plan",
            str(self.root),
            "--task-id",
            task_id,
            "--output",
            str(archive_plan),
        )
        archive_approval = self.root / (task_id + ".archive.approval.json")
        self.approve(archive_plan, archive_approval)
        return archive_plan, archive_approval, source_bytes, artifact

    def test_init_task_lifecycle_and_compare_and_swap(self) -> None:
        init = self.init_workspace()
        self.assertEqual(init["config"], ".workspace-organizer/config.yaml")
        for directory in (
            "00_总览",
            "01_导航",
            "10_收件箱",
            "20_任务",
            "30_资料库",
            "90_归档",
            "99_待整理",
            ".workspace-organizer",
        ):
            self.assertTrue((self.root / directory).is_dir(), directory)

        created = self.create_task(
            "task-lifecycle",
            "Lifecycle task",
            body="# Lifecycle task\n\nUser notes remain intact.\n",
        )
        note = self.root / created["record"]
        first_sha = created["sha256"]
        _, shown = self.run_cli("task", "show", str(self.root), "--task-id", "task-lifecycle")
        assert shown is not None
        self.assertNotIn("body", shown["task"])
        _, with_body = self.run_cli(
            "task",
            "show",
            str(self.root),
            "--task-id",
            "task-lifecycle",
            "--include-body",
        )
        assert with_body is not None
        self.assertIn("User notes remain intact.", with_body["task"]["body"])

        updated = self.run_cli(
            "task",
            "update",
            str(self.root),
            "--task-id",
            "task-lifecycle",
            "--title",
            "Lifecycle task (edited)",
            "--expected-sha",
            first_sha,
        )[1]
        assert updated is not None
        self.assertEqual(updated["status"], "updated")
        self.assertIn("title", updated["changed_fields"])
        after_update = note.read_bytes()

        # Reusing the old digest must fail closed and must not overwrite the
        # successful edit (the core CAS contract).
        stale_process, stale_payload = self.run_cli(
            "task",
            "update",
            str(self.root),
            "--task-id",
            "task-lifecycle",
            "--title",
            "stale overwrite",
            "--expected-sha",
            first_sha,
            check=False,
        )
        self.assertEqual(stale_process.returncode, 2)
        self.assertIsNone(stale_payload)
        self.assertRegex(stale_process.stderr.lower(), r"cas|digest|mismatch")
        self.assertEqual(note.read_bytes(), after_update)

        # Walk through every useful open-state branch, then close and reopen
        # with an explicit next action.  Business status and storage state are
        # intentionally asserted separately.
        for command, extra in (
            ("start", ()),
            ("wait", ("--waiting-on", "vendor", "--follow-up-on", "2026-08-25")),
            ("block", ()),
            ("start", ("--next-action", "Resume the work")),
        ):
            _, transition = self.run_cli(
                "task",
                command,
                str(self.root),
                "--task-id",
                "task-lifecycle",
                *extra,
            )
            assert transition is not None
            self.assertEqual(transition["status"], "updated")

        _, closed = self.run_cli(
            "task",
            "complete",
            str(self.root),
            "--task-id",
            "task-lifecycle",
            "--summary",
            "Finished and recorded.",
        )
        assert closed is not None
        self.assertEqual(closed["to"], "completed")
        _, reopened = self.run_cli(
            "task",
            "reopen",
            str(self.root),
            "--task-id",
            "task-lifecycle",
            "--next-action",
            "Check the recorded result",
        )
        assert reopened is not None
        self.assertEqual(reopened["to"], "active")
        _, final = self.run_cli(
            "task",
            "show",
            str(self.root),
            "--task-id",
            "task-lifecycle",
        )
        assert final is not None
        self.assertEqual(final["task"]["status"], "active")
        self.assertEqual(final["task"]["storage_state"], "active")
        self.assertIsNone(final["task"]["closed_at"])
        self.assertEqual(final["task"]["next_action"], "Check the recorded result")

    def test_capture_text_and_file_triage_requires_exact_approval(self) -> None:
        self.init_workspace()
        text_capture = self.run_cli(
            "capture",
            "create",
            str(self.root),
            "--id",
            "capture-text",
            "--title",
            "A text capture",
            "--text",
            "Keep this source note exactly.",
            "--yes",
        )[1]
        assert text_capture is not None
        text_note = self.root / text_capture["record"]
        original_text_note = text_note.read_bytes()
        self.assertIn(b"Keep this source note exactly.", original_text_note)
        original_text_record = model.parse_record(text_note)

        text_plan = self.root / "text-triage.plan.json"
        planned = self.run_cli(
            "capture",
            "triage",
            str(self.root),
            "--capture-id",
            "capture-text",
            "--disposition",
            "task",
            "--output",
            str(text_plan),
        )[1]
        assert planned is not None
        self.assertEqual(planned["status"], "planned")
        plan_data = json.loads(text_plan.read_text(encoding="utf-8"))
        self.assertIn("new_task", plan_data)
        text_approval = self.root / "text-triage.approval.json"
        self.approve(text_plan, text_approval)
        applied = self.run_cli(
            "capture",
            "triage-apply",
            str(self.root),
            "--plan",
            str(text_plan),
            "--approval",
            str(text_approval),
        )[1]
        assert applied is not None
        self.assertEqual(applied["status"], "verified")
        task_id = str(applied["target_task"])
        self.assertTrue((self.root / "20_任务" / task_id / f"{task_id}.md").is_file())
        # Triage necessarily updates the Capture lifecycle frontmatter, while
        # preserving the human-authored body and source record itself.
        triaged_text_record = model.parse_record(text_note)
        self.assertIsInstance(triaged_text_record, model.Capture)
        self.assertEqual(triaged_text_record.body, original_text_record.body)
        self.assertEqual(triaged_text_record.fields["status"], "triaged")
        captures = self.run_cli("capture", "list", str(self.root))[1]
        assert captures is not None
        self.assertEqual(captures["items"][0]["status"], "triaged")
        self.assertEqual(captures["items"][0]["target_task"], task_id)

        # A file capture copies immutable source bytes into the Inbox first;
        # attaching it to the task then creates an owner/hash-bound Artifact.
        external = self.root.parent / "source-input.txt"
        external_bytes = b"file capture bytes\n"
        external.write_bytes(external_bytes)
        file_capture = self.run_cli(
            "capture",
            "create",
            str(self.root),
            "--id",
            "capture-file",
            "--title",
            "A file capture",
            "--file",
            str(external),
            "--yes",
        )[1]
        assert file_capture is not None
        payload_rel = file_capture["payload"]["path"]
        self.assertEqual((self.root / payload_rel).read_bytes(), external_bytes)

        file_plan = self.root / "file-triage.plan.json"
        self.run_cli(
            "capture",
            "triage",
            str(self.root),
            "--capture-id",
            "capture-file",
            "--disposition",
            "artifact",
            "--task-id",
            task_id,
            "--output",
            str(file_plan),
        )
        file_approval = self.root / "file-triage.approval.json"
        self.approve(file_plan, file_approval)
        file_applied = self.run_cli(
            "capture",
            "triage-apply",
            str(self.root),
            "--plan",
            str(file_plan),
            "--approval",
            str(file_approval),
        )[1]
        assert file_applied is not None
        artifact = file_applied["artifact"]
        artifact_note = self.root / artifact["record"]
        artifact_payload = self.root / artifact["payload"]
        parsed_artifact = model.parse_record(artifact_note)
        self.assertIsInstance(parsed_artifact, model.Artifact)
        self.assertEqual(parsed_artifact.fields["owner_task"], task_id)
        self.assertEqual(parsed_artifact.fields["payload_path"], artifact["payload"])
        self.assertEqual(parsed_artifact.fields["sha256"], hashlib.sha256(external_bytes).hexdigest())
        self.assertEqual(artifact_payload.read_bytes(), external_bytes)
        self.assertEqual(external.read_bytes(), external_bytes)

    def test_views_generate_deterministic_observable_pages(self) -> None:
        self.init_workspace()
        self.create_task(
            "today-task",
            "Today task",
            status="active",
            scheduled_on="2026-08-24",
            due_on="2026-08-25",
        )
        self.create_task("waiting-task", "Waiting task", status="active")
        self.run_cli(
            "task",
            "wait",
            str(self.root),
            "--task-id",
            "waiting-task",
            "--waiting-on",
            "Vendor",
            "--follow-up-on",
            "2026-08-24",
        )
        self.create_task(
            "hidden-task",
            "Confidential task",
            status="active",
            sensitivity="confidential",
            scheduled_on="2026-08-24",
        )
        self.run_cli(
            "capture",
            "create",
            str(self.root),
            "--id",
            "inbox-item",
            "--text",
            "An item waiting for triage",
            "--yes",
        )

        _, generated = self.run_cli("views", "generate", str(self.root), "--now", "2026-08-24")
        assert generated is not None
        self.assertEqual(generated["status"], "generated")
        self.assertEqual(
            set(generated["paths"]),
            {
                "00_总览/TODAY.md",
                "00_总览/NEXT.md",
                "00_总览/INBOX.md",
                "00_总览/WAITING.md",
                "00_总览/ARCHIVE_INDEX.md",
            },
        )
        for relative in generated["paths"]:
            first_line = (self.root / relative).read_text(encoding="utf-8").splitlines()[0]
            self.assertTrue(first_line.startswith("<!-- workspace-organizer:generated"))
            self.assertIn("source_sha256=" + generated["source_sha256"], first_line)
        today = (self.root / "00_总览/TODAY.md").read_text(encoding="utf-8")
        next_page = (self.root / "00_总览/NEXT.md").read_text(encoding="utf-8")
        waiting = (self.root / "00_总览/WAITING.md").read_text(encoding="utf-8")
        inbox = (self.root / "00_总览/INBOX.md").read_text(encoding="utf-8")
        self.assertIn("Today task", today)
        self.assertNotIn("Confidential task", today)
        self.assertIn("Today task", next_page)
        self.assertIn("Waiting task", next_page)
        self.assertNotIn("Confidential task", next_page)
        self.assertIn("Vendor", waiting)
        self.assertIn("inbox-item", inbox)

        _, second = self.run_cli("views", "generate", str(self.root), "--now", "2026-08-24")
        assert second is not None
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["source_sha256"], generated["source_sha256"])

    def test_archive_and_restore_round_trip_rewrites_artifact_custody(self) -> None:
        self.init_workspace()
        self.create_task("archive-task", "Archive task", status="active")
        source = self.root.parent / "archive-source.bin"
        source_bytes = b"archive artifact payload\x00\x01"
        source.write_bytes(source_bytes)
        self.run_cli(
            "capture",
            "create",
            str(self.root),
            "--id",
            "archive-capture",
            "--title",
            "Archive source",
            "--file",
            str(source),
            "--yes",
        )
        triage_plan = self.root / "archive-triage.plan.json"
        self.run_cli(
            "capture",
            "triage",
            str(self.root),
            "--capture-id",
            "archive-capture",
            "--disposition",
            "artifact",
            "--task-id",
            "archive-task",
            "--output",
            str(triage_plan),
        )
        triage_approval = self.root / "archive-triage.approval.json"
        self.approve(triage_plan, triage_approval)
        triaged = self.run_cli(
            "capture",
            "triage-apply",
            str(self.root),
            "--plan",
            str(triage_plan),
            "--approval",
            str(triage_approval),
        )[1]
        assert triaged is not None
        artifact_record_rel = triaged["artifact"]["record"]
        self.run_cli(
            "task",
            "complete",
            str(self.root),
            "--task-id",
            "archive-task",
            "--summary",
            "Archived after delivery.",
        )
        task_before_archive = self.run_cli("task", "show", str(self.root), "--task-id", "archive-task")[1]
        assert task_before_archive is not None
        closed_year = task_before_archive["task"]["closed_at"][:4]

        archive_plan = self.root / "archive.plan.json"
        archive_planned = self.run_cli(
            "archive",
            "plan",
            str(self.root),
            "--task-id",
            "archive-task",
            "--output",
            str(archive_plan),
        )[1]
        assert archive_planned is not None
        destination_rel = f"90_归档/通用/{closed_year}/archive-task"
        self.assertEqual(archive_planned["destination"], destination_rel)
        archive_approval = self.root / "archive.approval.json"
        self.approve(archive_plan, archive_approval)
        archived = self.run_cli(
            "archive",
            "apply",
            str(self.root),
            "--plan",
            str(archive_plan),
            "--approval",
            str(archive_approval),
        )[1]
        assert archived is not None
        self.assertFalse((self.root / "20_任务/archive-task").exists())
        archived_bundle = self.root / destination_rel
        archived_note = archived_bundle / "archive-task.md"
        archived_task = model.parse_record(archived_note)
        self.assertIsInstance(archived_task, model.Task)
        self.assertEqual(archived_task.fields["status"], "completed")
        self.assertEqual(archived_task.fields["storage_state"], "archived")
        archived_artifact = model.parse_record(self.root / artifact_record_rel.replace("20_任务/archive-task", destination_rel))
        self.assertIsInstance(archived_artifact, model.Artifact)
        self.assertTrue(str(archived_artifact.fields["payload_path"]).startswith(destination_rel + "/"))
        self.assertEqual(
            (self.root / archived_artifact.fields["payload_path"]).read_bytes(),
            source_bytes,
        )

        restore_plan = self.root / "restore.plan.json"
        restored_plan = self.run_cli(
            "restore",
            "plan",
            str(self.root),
            "--task-id",
            "archive-task",
            "--output",
            str(restore_plan),
        )[1]
        assert restored_plan is not None
        restore_approval = self.root / "restore.approval.json"
        self.approve(restore_plan, restore_approval)
        restored = self.run_cli(
            "restore",
            "apply",
            str(self.root),
            "--plan",
            str(restore_plan),
            "--approval",
            str(restore_approval),
        )[1]
        assert restored is not None
        self.assertFalse(archived_bundle.exists())
        restored_note = self.root / "20_任务/archive-task/archive-task.md"
        restored_task = model.parse_record(restored_note)
        self.assertIsInstance(restored_task, model.Task)
        self.assertEqual(restored_task.fields["status"], "completed")
        self.assertEqual(restored_task.fields["storage_state"], "active")
        self.assertIsNotNone(restored_task.fields["closed_at"])
        restored_artifact = model.parse_record(self.root / "20_任务/archive-task/04_记录" / Path(artifact_record_rel).name)
        self.assertIsInstance(restored_artifact, model.Artifact)
        self.assertEqual(
            restored_artifact.fields["payload_path"],
            "20_任务/archive-task/01_输入/archive-source.bin",
        )
        self.assertEqual(
            (self.root / restored_artifact.fields["payload_path"]).read_bytes(),
            source_bytes,
        )

    def test_damaged_canonical_task_is_fail_closed(self) -> None:
        self.init_workspace()
        created = self.create_task("damaged-task", "Damaged canonical task", status="active")
        note = self.root / created["record"]
        # Keep the canonical filename, but destroy the frontmatter.  A
        # managed canonical note must be an explicit error, never silently
        # skipped as if the task did not exist.
        note.write_text("---\nkind: task\nschema_version: 2\n", encoding="utf-8")

        listed, payload = self.run_cli("task", "list", str(self.root), check=False)
        self.assertEqual(listed.returncode, 2)
        self.assertIsNone(payload)
        self.assertRegex(listed.stderr.lower(), r"invalid (?:canonical|managed) task note")
        shown, payload = self.run_cli(
            "task",
            "show",
            str(self.root),
            "--task-id",
            "damaged-task",
            check=False,
        )
        self.assertEqual(shown.returncode, 2)
        self.assertIsNone(payload)
        self.assertRegex(shown.stderr.lower(), r"invalid (?:canonical|managed) task note")

    def test_control_and_config_symlinks_are_rejected(self) -> None:
        self.init_workspace()
        config = self.root / ".workspace-organizer/config.yaml"
        config_copy = self.root.parent / "config-copy.yaml"
        config_copy.write_bytes(config.read_bytes())
        config.unlink()
        config.symlink_to(config_copy)
        config_process, payload = self.run_cli("task", "list", str(self.root), check=False)
        self.assertEqual(config_process.returncode, 2)
        self.assertIsNone(payload)
        self.assertRegex(config_process.stderr.lower(), r"config.*symlink")

        # Use a second workspace so the config-link case and the control-dir
        # case remain independent.  Renaming the disposable control directory
        # avoids any case-only filename collision on macOS.
        second_root = self.root.parent / "control-link-workspace"
        self.run_cli("init", str(second_root), "--workspace-id", "control-link", "--yes")
        control = second_root / ".workspace-organizer"
        control_backup = second_root.parent / "control-backup"
        control.rename(control_backup)
        control_target = second_root.parent / "control-target"
        control_target.mkdir()
        control.symlink_to(control_target, target_is_directory=True)
        control_process, payload = self.run_cli("task", "list", str(second_root), check=False)
        self.assertEqual(control_process.returncode, 2)
        self.assertIsNone(payload)
        self.assertRegex(control_process.stderr.lower(), r"workspace-organizer.*real directory|symlink")

    def test_unknown_area_update_is_rejected_without_mutation(self) -> None:
        self.init_workspace()
        created = self.create_task("area-task", "Area task", status="active")
        note = self.root / created["record"]
        before = note.read_bytes()
        rejected, payload = self.run_cli(
            "task",
            "update",
            str(self.root),
            "--task-id",
            "area-task",
            "--area",
            "does-not-exist",
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIsNone(payload)
        self.assertIn("unknown area", rejected.stderr.lower())
        self.assertEqual(note.read_bytes(), before)

    def test_init_rejects_duplicate_normalized_archive_folders(self) -> None:
        duplicate_root = self.root.parent / "duplicate-area-workspace"
        rejected, payload = self.run_cli(
            "init",
            str(duplicate_root),
            "--workspace-id",
            "duplicate-area",
            "--area",
            "ops=Operations=shared",
            "--area",
            "finance=Finance=SHARED",
            "--yes",
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIsNone(payload)
        self.assertRegex(rejected.stderr.lower(), r"duplicate.*archive folder")
        self.assertFalse((duplicate_root / ".workspace-organizer/config.yaml").exists())

    def test_operation_id_traversal_cannot_write_outside_workspace(self) -> None:
        self.init_workspace()
        plan, _ = self.prepare_closed_task("operation-id-task")
        outside = self.root.parent / "operation-escape.result.json"
        self.assertFalse(outside.exists())

        self.rewrite_plan(plan, lambda value: value.update({"operation_id": "../../../operation-escape"}))
        tampered_approval = self.root / "operation-id-tampered.approval.json"
        approval_process, _ = self.run_cli(
            "approve",
            "--plan",
            str(plan),
            "--output",
            str(tampered_approval),
            "--yes",
            check=False,
        )
        if approval_process.returncode == 0:
            # Older implementations accepted the plan at approval time; the
            # apply boundary must still reject the unsafe filename component.
            applied, payload = self.run_cli(
                "archive",
                "apply",
                str(self.root),
                "--plan",
                str(plan),
                "--approval",
                str(tampered_approval),
                check=False,
            )
            self.assertEqual(applied.returncode, 2)
            self.assertIsNone(payload)
            self.assertRegex(applied.stderr.lower(), r"unsafe.*operation|operation.*identifier")
        else:
            self.assertRegex(approval_process.stderr.lower(), r"unsafe.*operation|operation.*identifier")
        self.assertFalse(outside.exists())
        self.assertTrue((self.root / "20_任务/operation-id-task/operation-id-task.md").exists())
        self.assertFalse((self.root / "90_归档/通用/2026/operation-id-task").exists())

    def test_self_consistent_artifact_update_tampering_is_rejected(self) -> None:
        self.init_workspace()
        plan, _, _, artifact = self.prepare_closed_task_with_artifact("artifact-tamper")
        self.assertTrue(artifact["record"].endswith(".artifact.md"))
        plan_data = self.rewrite_plan(
            plan,
            lambda value: value["artifact_updates"][0].update({"record_sha256": "0" * 64}),
        )
        self.assertEqual(plan_data["artifact_updates"][0]["record_sha256"], "0" * 64)
        tampered_approval = self.root / "artifact-tampered.approval.json"
        self.approve(plan, tampered_approval)
        rejected, payload = self.run_cli(
            "archive",
            "apply",
            str(self.root),
            "--plan",
            str(plan),
            "--approval",
            str(tampered_approval),
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIsNone(payload)
        self.assertRegex(rejected.stderr.lower(), r"artifact custody|artifact.*changed")
        self.assertTrue((self.root / "20_任务/artifact-tamper/artifact-tamper.md").exists())
        self.assertFalse((self.root / "90_归档/通用/2026/artifact-tamper").exists())

    def test_tampered_approval_schema_version_is_rejected(self) -> None:
        self.init_workspace()
        plan, approval = self.prepare_closed_task("approval-schema-task")
        tampered = json.loads(approval.read_text(encoding="utf-8"))
        tampered["schema_version"] = 1
        tampered_approval = self.root / "approval-schema-tampered.json"
        tampered_approval.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        rejected, payload = self.run_cli(
            "archive",
            "apply",
            str(self.root),
            "--plan",
            str(plan),
            "--approval",
            str(tampered_approval),
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIsNone(payload)
        self.assertIn("approval schema_version", rejected.stderr.lower())
        self.assertTrue((self.root / "20_任务/approval-schema-task/approval-schema-task.md").exists())
        self.assertFalse((self.root / "90_归档/通用/2026/approval-schema-task").exists())

    def test_stale_approval_and_symlink_or_traversal_inputs_fail_closed(self) -> None:
        self.init_workspace()

        # A root symlink is never accepted as a workspace target.
        root_link = self.root.parent / "workspace-link"
        root_link.symlink_to(self.root, target_is_directory=True)
        root_process, _ = self.run_cli("init", str(root_link), "--yes", check=False)
        self.assertEqual(root_process.returncode, 2)
        self.assertIn("symlink", root_process.stderr.lower())

        # IDs and capture source paths cannot escape the workspace or bypass
        # no-follow file handling.
        bad_task, _ = self.run_cli(
            "task",
            "create",
            str(self.root),
            "--id",
            "../escape",
            "--title",
            "Unsafe",
            "--yes",
            check=False,
        )
        self.assertEqual(bad_task.returncode, 2)
        self.assertFalse((self.root.parent / "escape").exists())
        source = self.root.parent / "real-source.txt"
        source.write_text("source", encoding="utf-8")
        source_link = self.root.parent / "source-link.txt"
        source_link.symlink_to(source)
        bad_capture, _ = self.run_cli(
            "capture",
            "create",
            str(self.root),
            "--id",
            "symlink-capture",
            "--file",
            str(source_link),
            "--yes",
            check=False,
        )
        self.assertEqual(bad_capture.returncode, 2)
        self.assertFalse((self.root / "10_收件箱/symlink-capture.md").exists())

        self.create_task("stale-task", "Stale approval", status="active")
        self.run_cli(
            "task",
            "complete",
            str(self.root),
            "--task-id",
            "stale-task",
            "--summary",
            "Close before planning.",
        )
        stale_plan = self.root / "stale.plan.json"
        self.run_cli(
            "archive",
            "plan",
            str(self.root),
            "--task-id",
            "stale-task",
            "--output",
            str(stale_plan),
        )
        stale_approval = self.root / "stale.approval.json"
        self.approve(stale_plan, stale_approval)
        stale_note = self.root / "20_任务/stale-task/stale-task.md"
        stale_note.write_bytes(stale_note.read_bytes() + b"\n")
        stale_apply, _ = self.run_cli(
            "archive",
            "apply",
            str(self.root),
            "--plan",
            str(stale_plan),
            "--approval",
            str(stale_approval),
            check=False,
        )
        self.assertEqual(stale_apply.returncode, 2)
        self.assertRegex(stale_apply.stderr.lower(), r"changed|digest|snapshot")
        self.assertTrue(stale_note.exists())
        self.assertFalse((self.root / "90_归档/通用/2026/stale-task").exists())

        # Even with a freshly approved plan, a symlink inserted into the
        # managed destination hierarchy is rejected before any publish.
        fresh_plan = self.root / "fresh.plan.json"
        self.run_cli(
            "archive",
            "plan",
            str(self.root),
            "--task-id",
            "stale-task",
            "--output",
            str(fresh_plan),
        )
        fresh_approval = self.root / "fresh.approval.json"
        self.approve(fresh_plan, fresh_approval)
        outside = self.root.parent / "outside-archive"
        outside.mkdir()
        archive_area = self.root / "90_归档/通用"
        archive_area.symlink_to(outside, target_is_directory=True)
        symlink_apply, _ = self.run_cli(
            "archive",
            "apply",
            str(self.root),
            "--plan",
            str(fresh_plan),
            "--approval",
            str(fresh_approval),
            check=False,
        )
        self.assertEqual(symlink_apply.returncode, 2)
        self.assertRegex(symlink_apply.stderr.lower(), r"symlink|unsafe|real directory")
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
