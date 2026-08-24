import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "workspace-organizer" / "scripts" / "workspace_views.py"
SPEC = importlib.util.spec_from_file_location("workspace_views", SCRIPT)
assert SPEC and SPEC.loader
views = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(views)


def task(task_id, *, title=None, status="active", storage_state="active", sensitivity="internal", priority="normal", area="ops", scheduled_on=None, due_on=None, follow_up_on=None, next_action="Do the next thing", waiting_on=None, closed_at=None, closure_summary=None, record_path=None):
    return {
        "kind": "task",
        "schema_version": 2,
        "id": task_id,
        "title": title or task_id.title(),
        "outcome": "A useful outcome",
        "status": status,
        "storage_state": storage_state,
        "area": area,
        "type": "administration",
        "priority": priority,
        "scheduled_on": scheduled_on,
        "due_on": due_on,
        "follow_up_on": follow_up_on,
        "next_action": next_action,
        "waiting_on": waiting_on,
        "sensitivity": sensitivity,
        "closed_at": closed_at,
        "closure_summary": closure_summary,
        "record_path": record_path,
    }


def capture(capture_id, *, title=None, sensitivity="internal", state="inbox", path=None):
    return {
        "kind": "capture",
        "id": capture_id,
        "title": title or capture_id.title(),
        "path": path or "10_收件箱/" + capture_id + ".md",
        "captured_at": "2026-08-24T09:00:00+08:00",
        "triage_state": state,
        "sensitivity": sensitivity,
    }


class ViewRenderingTests(unittest.TestCase):
    def test_deterministic_order_and_hidden_records_are_not_read_or_counted(self):
        visible_a = task("a", title="Alpha", priority="high", due_on="2026-08-26")
        visible_b = task("b", title="Beta", priority="urgent", due_on="2026-08-25")
        hidden = {"sensitivity": "confidential", "id": "hidden", "title": object()}  # malformed after the filter
        first = views.build_views([visible_a, hidden, visible_b], [capture("inbox")], now="2026-08-24")
        second = views.build_views([visible_b, visible_a, hidden], [capture("inbox")], now="2026-08-24")
        self.assertEqual(first["source_sha256"], second["source_sha256"])
        today = first["files"]["00_总览/TODAY.md"].decode("utf-8")
        self.assertIn("Beta", today)
        self.assertIn("Alpha", today)
        self.assertNotIn("hidden", today)
        self.assertNotIn("object at", today)

    def test_focus_does_not_duplicate_scheduled_task_and_hidden_focus_has_no_effect(self):
        focused = task("focus", title="Focused", scheduled_on="2026-08-24")
        hidden = task("secret", title="Secret", sensitivity="restricted", scheduled_on="2026-08-24")
        first = views.build_views([focused, hidden], now="2026-08-24", focus_ids=["focus", "secret"])
        second = views.build_views([focused, hidden], now="2026-08-24", focus_ids=["focus"])
        self.assertEqual(first["source_sha256"], second["source_sha256"])
        today = first["files"]["00_总览/TODAY.md"].decode("utf-8")
        self.assertEqual(today.count("[Focused]"), 1)
        self.assertNotIn("Secret", today)

    def test_all_pages_have_marker_and_expected_projections(self):
        tasks = [
            task("wait", status="waiting", follow_up_on="2026-08-24", waiting_on="Vendor", next_action=None),
            task("blocked", status="blocked", priority="urgent", waiting_on="Approval", next_action="Ask approver"),
            task("done", status="completed", storage_state="archived", closed_at="2026-08-20T12:00:00+08:00", closure_summary="Submitted", next_action=None, record_path="90_归档/ops/2026/done/done.md"),
        ]
        bundle = views.build_views(tasks, [capture("c")], now="2026-08-24")
        for relative, payload in bundle["files"].items():
            text = payload.decode("utf-8")
            self.assertTrue(text.startswith("<!-- workspace-organizer:generated"))
            self.assertIn("source_sha256=" + bundle["source_sha256"], text.splitlines()[0])
        self.assertIn("Vendor", bundle["files"]["00_总览/WAITING.md"].decode("utf-8"))
        self.assertIn("Submitted", bundle["files"]["00_总览/ARCHIVE_INDEX.md"].decode("utf-8"))
        self.assertNotIn("done", bundle["files"]["00_总览/NEXT.md"].decode("utf-8"))
        self.assertIn("c", bundle["files"]["00_总览/INBOX.md"].decode("utf-8"))

    def test_write_is_idempotent_and_refuses_unmarked_user_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "00_总览").mkdir(parents=True)
            bundle = views.build_views([task("one")], now="2026-08-24")
            receipt = views.write_views(root, bundle)
            self.assertEqual(receipt["status"], "generated")
            before = {p: p.read_bytes() for p in (root / "00_总览").glob("*.md")}
            second = views.write_views(root, bundle)
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(before, {p: p.read_bytes() for p in (root / "00_总览").glob("*.md")})
            target = root / "00_总览/TODAY.md"
            target.write_text("# My own Today\n", encoding="utf-8")
            with self.assertRaises(views.UserOwnedViewError):
                views.write_views(root, bundle)
            self.assertEqual(target.read_text(encoding="utf-8"), "# My own Today\n")

    def test_commit_rolls_back_all_replacements_if_one_replace_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "00_总览").mkdir(parents=True)
            old = views.build_views([task("one", title="Old")], now="2026-08-24")
            views.write_views(root, old)
            before = {p.name: p.read_bytes() for p in (root / "00_总览").glob("*.md")}
            new = views.build_views([task("one", title="New")], now="2026-08-24")
            original_replace = views.os.replace
            calls = {"count": 0}

            def fail_once(source, destination):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("synthetic replace failure")
                return original_replace(source, destination)

            with mock.patch.object(views.os, "replace", side_effect=fail_once):
                with self.assertRaises(views.ViewCommitError):
                    views.write_views(root, new)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (root / "00_总览").glob("*.md")})

    def test_collect_records_reads_clean_slate_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_path = root / "20_任务/abc/abc.md"
            task_path.parent.mkdir(parents=True)
            task_path.write_text(
                "---\nkind: task\nschema_version: 2\nid: abc\ntitle: \"Read note\"\nstatus: active\nstorage_state: active\narea: ops\ntype: admin\npriority: normal\nscheduled_on: null\ndue_on: 2026-08-25\nnext_action: \"Read it\"\nsensitivity: internal\n---\nBody\n",
                encoding="utf-8",
            )
            capture_path = root / "10_收件箱/mail.md"
            capture_path.parent.mkdir(parents=True)
            capture_path.write_text(
                "---\nkind: capture\nid: mail\ntitle: Mail\ntriage_state: inbox\nsensitivity: internal\n---\n",
                encoding="utf-8",
            )
            records = views.collect_records(root)
            self.assertEqual([item["id"] for item in records["tasks"]], ["abc"])
            self.assertEqual([item["id"] for item in records["captures"]], ["mail"])
            self.assertEqual(records["tasks"][0]["record_path"], "20_任务/abc/abc.md")
            self.assertEqual(records["captures"][0]["path"], "10_收件箱/mail.md")

    def test_generate_views_end_to_end_and_dangling_symlink_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "00_总览").mkdir(parents=True)
            task_path = root / "20_任务/a/a.md"
            task_path.parent.mkdir(parents=True)
            task_path.write_text(
                "---\nkind: task\nid: a\ntitle: A\nstatus: active\nstorage_state: active\narea: ops\ntype: admin\npriority: high\ndue_on: 2026-08-24\nnext_action: Work\nsensitivity: internal\n---\n",
                encoding="utf-8",
            )
            receipt = views.generate_views(root, now="2026-08-24")
            self.assertEqual(receipt["status"], "generated")
            self.assertTrue((root / "00_总览/TODAY.md").is_file())
            target = root / "00_总览/TODAY.md"
            target.unlink()
            outside = root / "outside.md"
            outside.write_text("do not touch", encoding="utf-8")
            target.symlink_to(outside)
            with self.assertRaises(views.UserOwnedViewError):
                views.generate_views(root, now="2026-08-24")
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not touch")

    def test_config_profile_area_labels_and_nested_git_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "00_总览").mkdir(parents=True)
            control = root / ".workspace-organizer"
            control.mkdir()
            (control / "config.yaml").write_text(
                "kind: workspace-config\n"
                "schema_version: 2\n"
                "timezone: Asia/Shanghai\n"
                "default_sensitivity: internal\n"
                "view_max_sensitivity: internal\n"
                'areas: [{"key":"ops","label":"运营","archive_folder":"运营"}]\n',
                encoding="utf-8",
            )
            note = root / "20_任务/one/one.md"
            note.parent.mkdir(parents=True)
            note.write_text(
                "---\nkind: task\nid: one\ntitle: One\nstatus: active\nstorage_state: active\narea: ops\ntype: admin\npriority: normal\nnext_action: Work\nsensitivity: internal\n---\n",
                encoding="utf-8",
            )
            hidden = root / "20_任务/two/two.md"
            hidden.parent.mkdir(parents=True)
            hidden.write_text(
                "---\nkind: task\nid: two\ntitle: Confidential\nstatus: active\nstorage_state: active\narea: ops\ntype: admin\npriority: normal\nnext_action: Work\nsensitivity: confidential\n---\n",
                encoding="utf-8",
            )
            nested = root / "20_任务/vendor/.git/secret.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("---\nkind: task\nid: secret\n---\n", encoding="utf-8")
            (nested.parent.parent / "visible-looking.md").write_text(
                "---\nkind: task\nid: vendor\ntitle: Vendor secret\nstatus: active\n"
                "storage_state: active\narea: ops\ntype: admin\npriority: normal\n"
                "next_action: Read\nsensitivity: internal\n---\n",
                encoding="utf-8",
            )
            receipt = views.generate_views(root, now="2026-08-24", profile=None)
            self.assertEqual(receipt["profile"], "internal")
            next_page = (root / "00_总览/NEXT.md").read_text(encoding="utf-8")
            self.assertIn("运营", next_page)
            self.assertNotIn("Confidential", next_page)
            self.assertNotIn("secret", next_page)
            self.assertNotIn("Vendor secret", next_page)

    def test_frontmatter_collection_does_not_decode_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "20_任务/one/one.md"
            note.parent.mkdir(parents=True)
            prefix = (
                b"---\nkind: task\nid: one\ntitle: One\nstatus: active\n"
                b"storage_state: active\narea: ops\ntype: admin\npriority: normal\n"
                b"next_action: Work\nsensitivity: internal\n---\n"
            )
            note.write_bytes(prefix + b"\xff" * (2 * 1024 * 1024))
            records = views.collect_records(root)
            self.assertEqual([item["id"] for item in records["tasks"]], ["one"])

    def test_marker_metadata_is_bound_to_bundle(self):
        bundle = views.build_views([task("one")], now="2026-08-24")
        tampered = dict(bundle)
        tampered["profile"] = "public"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(views.ViewError):
                views.write_views(Path(temporary), tampered)


if __name__ == "__main__":
    unittest.main()
