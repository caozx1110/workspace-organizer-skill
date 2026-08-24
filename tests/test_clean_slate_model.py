from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill" / "workspace-organizer" / "scripts"))

import clean_slate_model as model  # noqa: E402


def task_fields(**overrides):
    value = {
        "kind": "task",
        "schema_version": 2,
        "id": "20260824T135501-renew-passport",
        "title": "更新护照",
        "outcome": "拿到申请受理凭证",
        "status": "active",
        "storage_state": "active",
        "area": "personal-admin",
        "type": "administration",
        "priority": "high",
        "scheduled_on": "2026-08-25",
        "due_on": "2026-08-29",
        "next_action": "准备并扫描身份证复印件",
        "waiting_on": None,
        "follow_up_on": None,
        "sensitivity": "internal",
        "created_at": "2026-08-24T13:55:01+08:00",
        "updated_at": "2026-08-24T14:02:10+08:00",
        "started_at": None,
        "closed_at": None,
        "archived_at": None,
        "closure_summary": None,
        "tags": ["task/personal-admin"],
        "aliases": ["护照更新"],
        "x_user_property": {"keep": True},
    }
    value.update(overrides)
    return value


class CleanSlateModelTests(unittest.TestCase):
    def test_task_round_trip_preserves_unknown_property_and_body(self):
        original = model.render_frontmatter(task_fields(), "\n# 工作记录\n\n正文不应被重排。\n").encode()
        record = model.parse_note_bytes(original)
        self.assertIsInstance(record, model.Task)
        self.assertEqual(record.fields["x_user_property"], {"keep": True})
        self.assertEqual(record.body, "\n# 工作记录\n\n正文不应被重排。\n")
        self.assertEqual(record.to_bytes(), original)

    def test_parser_rejects_duplicate_keys_and_yaml_dangerous_features(self):
        duplicate = b"---\nkind: task\nkind: task\n---\n"
        with self.assertRaises(model.FrontmatterError):
            model.parse_note_bytes(duplicate)
        anchored = b"---\nkind: task\ntitle: &secret value\n---\n"
        with self.assertRaises(model.FrontmatterError):
            model.parse_note_bytes(anchored)

    def test_capture_and_artifact_validate(self):
        capture = {
            "kind": "capture",
            "schema_version": 2,
            "capture_id": "cap-contract-pdf",
            "title": "供应商合同 PDF",
            "status": "inbox",
            "capture_type": "file",
            "payload_path": "10_收件箱/供应商合同.pdf",
            "source": {"channel": "downloads", "original_name": "供应商合同.pdf"},
            "sensitivity": "internal",
            "captured_at": "2026-08-24T09:00:00+08:00",
            "updated_at": "2026-08-24T09:00:00+08:00",
            "triaged_at": None,
            "disposition": None,
            "target_task": None,
            "tags": [],
        }
        capture_note = model.Capture.from_fields(capture)
        self.assertEqual(capture_note.record_id, "cap-contract-pdf")
        artifact = {
            "kind": "artifact",
            "schema_version": 2,
            "artifact_id": "artifact-contract-pdf",
            "payload_path": "20_任务/20260824T135501-renew-passport/01_输入/合同.pdf",
            "owner_task": "20260824T135501-renew-passport",
            "role": "input",
            "sensitivity": "internal",
            "provenance": {"source": "capture", "tool": "copy"},
            "sha256": "0" * 64,
            "derived_from": None,
            "created_at": "2026-08-24T09:01:00+08:00",
        }
        artifact_note = model.Artifact.from_fields(artifact)
        self.assertEqual(artifact_note.record_id, "artifact-contract-pdf")

    def test_transition_close_requires_summary_and_reopen_requires_action(self):
        with self.assertRaises(model.StateTransitionError):
            model.transition_task(task_fields(), "completed", now="2026-08-24T14:03:10+08:00")
        closed = model.transition_task(
            task_fields(),
            "completed",
            now="2026-08-24T14:03:10+08:00",
            closure_summary="申请已提交，收到受理凭证。",
        )
        self.assertEqual(closed["status"], "completed")
        self.assertIsNone(closed["next_action"])
        with self.assertRaises(model.StateTransitionError):
            model.transition_task(closed, "active", now="2026-08-24T14:04:10+08:00")
        reopened = model.transition_task(
            closed,
            "active",
            now="2026-08-24T14:04:10+08:00",
            next_action="保存受理凭证扫描件",
        )
        self.assertEqual(reopened["status"], "active")
        self.assertIsNone(reopened["closed_at"])
        self.assertEqual(reopened["next_action"], "保存受理凭证扫描件")

    def test_archive_separates_storage_state_and_restore_does_not_change_business_status(self):
        closed = model.transition_task(
            task_fields(),
            "completed",
            now="2026-08-24T14:03:10+08:00",
            closure_summary="完成",
        )
        archived = model.archive_task(closed, now="2026-08-24T14:04:10+08:00")
        self.assertEqual(archived["status"], "completed")
        self.assertEqual(archived["storage_state"], "archived")
        restored = model.restore_task(archived, now="2026-08-24T14:05:10+08:00")
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["storage_state"], "active")
        self.assertIsNone(restored["archived_at"])

    def test_cas_rejects_stale_digest_and_preserves_unknown_and_body(self):
        original = model.render_frontmatter(task_fields(), "\n用户正文\n").encode()
        digest = model.sha256_bytes(original)
        result = model.cas_update(
            original,
            digest,
            {"title": "更新护照（材料已齐）"},
            now="2026-08-24T14:03:10+08:00",
        )
        self.assertEqual(result.changed_fields, ("title", "updated_at"))
        self.assertIn("用户正文", result.content.decode())
        self.assertIn('x_user_property: {"keep":true}', result.content.decode())
        self.assertNotEqual(result.before_sha256, result.after_sha256)
        with self.assertRaises(model.CASConflict) as caught:
            model.cas_update(original, "f" * 64, {"title": "冲突"})
        self.assertEqual(caught.exception.actual, digest)

    def test_cas_keeps_unknown_block_property_byte_exact(self):
        original = model.render_frontmatter(task_fields(), "\n正文\n")
        original = original.replace(
            'x_user_property: {"keep":true}',
            "x_user_property:\n  keep: true",
        ).encode()
        result = model.cas_update(
            original,
            model.sha256_bytes(original),
            {"priority": "urgent"},
            now="2026-08-24T14:03:10+08:00",
        )
        self.assertIn(b"x_user_property:\n  keep: true\n", result.content)
        self.assertIsInstance(result.record, Mapping)

    def test_receipt_is_append_only_jsonl(self):
        receipt = model.EventReceipt.create(
            event_type="task.update",
            entity_kind="task",
            entity_id="20260824T135501-renew-passport",
            occurred_at="2026-08-24T14:03:10+08:00",
            changed_fields=["title", "updated_at"],
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".workspace-organizer" / "events.jsonl"
            model.append_event(path, receipt)
            model.append_event(path, receipt)
            rows = model.read_events(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_type"], "task.update")
        self.assertEqual(rows[0]["changed_fields"], ["title", "updated_at"])

    def test_load_and_save_task_note_require_expected_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "20_任务" / task_fields()["id"] / f"{task_fields()['id']}.md"
            created = model.save_task_note(path, model.Task.from_fields(task_fields(), "\n初始正文\n"))
            self.assertIsNone(created.before_sha256)
            loaded = model.load_task_note(path)
            self.assertEqual(loaded.source_digest, created.after_sha256)
            changed = dict(loaded.fields)
            changed["priority"] = "urgent"
            changed["updated_at"] = "2026-08-24T14:03:10+08:00"
            updated = model.save_task_note(
                path,
                model.Task.from_fields(changed, "\n修改后的正文\n"),
                expected_sha256=loaded.source_digest,
                now="2026-08-24T14:03:10+08:00",
            )
            self.assertIn("body", updated.changed_fields)
            self.assertEqual(model.load_task_note(path).body, "\n修改后的正文\n")
            with self.assertRaises(model.CASConflict):
                model.save_task_note(
                    path,
                    model.Task.from_fields(changed),
                    expected_sha256=loaded.source_digest,
                )


if __name__ == "__main__":
    unittest.main()
