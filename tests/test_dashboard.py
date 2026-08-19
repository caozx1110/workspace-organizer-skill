from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skill" / "workspace-organizer"
SCRIPT_ROOT = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

import workspace_dashboard as dashboard  # noqa: E402
import workspace_organizer as core  # noqa: E402


def _task(
    task_id: str,
    *,
    title: str,
    status: str = "active",
    priority: str = "normal",
    due=None,
    sensitivity: str = "internal",
    next_action: str = "Advance the work",
) -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "title": title,
        "status": status,
        "area": "operations",
        "type": "delivery",
        "priority": priority,
        "due": due,
        "sensitivity": sensitivity,
        "next_action": next_action,
        "updated": "2026-08-19T12:00:00+08:00",
        "closed_at": None,
        "archived_at": None,
    }


class DashboardTests(unittest.TestCase):
    def make_workspace(self, base: Path, tasks: list[tuple[str, dict]], *, adopted=()) -> Path:
        root = base / "workspace"
        for relative in (
            "00_总览",
            "10_收件箱",
            "20_任务",
            "30_资料库",
            "90_归档",
            "99_待整理",
            ".workspace-organizer/verification",
        ):
            root.joinpath(*relative.split("/")).mkdir(parents=True, exist_ok=True)
        config = {
            "schema_version": 1,
            "workspace_id": "dashboard-tests",
            "default_sensitivity": "internal",
            "adopted_task_paths": list(adopted),
            "adopted_material_roots": [],
            "exclude_paths": [],
        }
        (root / ".workspace-organizer" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for relative, data in tasks:
            bundle = root.joinpath(*relative.split("/"))
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "TASK.md").write_bytes(
                core.serialize_task(data, "\nCanonical body must never reach the dashboard.\n")
            )
        core.generate_indexes(root)
        return root

    def output_bytes(self, root: Path) -> dict[str, bytes]:
        output = root / ".workspace-organizer" / "dashboard"
        return {name: (output / name).read_bytes() for name in dashboard.DASHBOARD_FILES}

    def test_repeated_generation_is_byte_identical_and_matches_golden_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_workspace(
                Path(temporary),
                [
                    ("20_任务/active-high", _task("active-high", title="Write the field brief", priority="high", due="2026-09-02")),
                    ("20_任务/blocked-urgent", _task("blocked-urgent", title="Clear the permit hold", status="blocked", priority="urgent", due="2026-08-28")),
                    ("20_任务/waiting-normal", _task("waiting-normal", title="Await the specimen", status="waiting")),
                    ("20_任务/planned-low", _task("planned-low", title="Plan the archive audit", status="planned", priority="low")),
                ],
            )
            first = dashboard.generate_dashboard(root)
            first_bytes = self.output_bytes(root)
            mtimes = {
                name: (root / ".workspace-organizer" / "dashboard" / name).stat().st_mtime_ns
                for name in dashboard.DASHBOARD_FILES
            }
            second = dashboard.generate_dashboard(root)
            self.assertEqual(first["status"], "generated")
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(first_bytes, self.output_bytes(root))
            self.assertEqual(
                mtimes,
                {
                    name: (root / ".workspace-organizer" / "dashboard" / name).stat().st_mtime_ns
                    for name in dashboard.DASHBOARD_FILES
                },
            )
            manifest = json.loads(first_bytes["manifest.json"])
            self.assertTrue(manifest["derived"])
            self.assertEqual(manifest["source_fingerprint"], first["source_fingerprint"])
            self.assertEqual(manifest["sources"]["schema_version"], 1)
            for key in (
                "todo_catalog_sha256",
                "todo_source_sha256",
                "timeline_catalog_sha256",
                "timeline_source_sha256",
            ):
                self.assertRegex(manifest["sources"][key], r"^[0-9a-f]{64}$")
            for name in ("app.js", "index.html", "styles.css"):
                self.assertEqual(manifest["outputs"][name], hashlib.sha256(first_bytes[name]).hexdigest())
            golden = json.loads(
                (REPO_ROOT / "tests" / "fixtures" / "dashboard" / "representative.sha256.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                golden,
                {
                    name: hashlib.sha256(payload).hexdigest()
                    for name, payload in sorted(first_bytes.items())
                },
            )

    def test_todo_groups_timeline_order_and_canonical_links_follow_v1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_workspace(
                Path(temporary),
                [
                    ("20_任务/planned", _task("planned", title="Plan", status="planned", priority="low")),
                    ("20_任务/active", _task("active", title="Act", priority="high", due="2026-09-03")),
                    ("20_任务/blocked", _task("blocked", title="Unblock", status="blocked", priority="urgent", due="2026-09-01")),
                    ("20_任务/waiting", _task("waiting", title="Wait", status="waiting", priority="normal", due="2026-09-03")),
                ],
            )
            dashboard.generate_dashboard(root)
            page = self.output_bytes(root)["index.html"].decode("utf-8")
            groups = re.findall(r'data-status-group="([a-z]+)"', page)
            self.assertEqual(groups, ["active", "blocked", "waiting", "planned"])
            dates = re.findall(r'class="timeline-date" datetime="([0-9-]+)"', page)
            self.assertEqual(dates, ["2026-09-01", "2026-09-03", "2026-09-03"])
            hrefs = re.findall(r'href="(\.\./\.\./[^"]+/TASK\.md)"', page)
            self.assertTrue(hrefs)
            self.assertEqual(
                {unquote(value.removeprefix("../../")) for value in hrefs},
                {
                    "20_任务/active/TASK.md",
                    "20_任务/blocked/TASK.md",
                    "20_任务/waiting/TASK.md",
                    "20_任务/planned/TASK.md",
                },
            )
            self.assertNotIn("Canonical body", page)

    def test_empty_unicode_spaces_and_missing_optional_values_render_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            empty = self.make_workspace(base / "empty", [])
            dashboard.generate_dashboard(empty)
            empty_page = self.output_bytes(empty)["index.html"].decode("utf-8")
            self.assertIn("The docket is clear.", empty_page)
            self.assertIn("No dated dispatches.", empty_page)

            adopted = "Legacy Tasks/研究 alpha"
            root = self.make_workspace(
                base / "unicode",
                [(adopted, _task("unicode-task", title="研究  café", due=None))],
                adopted=(adopted,),
            )
            dashboard.generate_dashboard(root)
            page = self.output_bytes(root)["index.html"].decode("utf-8")
            self.assertIn("研究  café", page)
            self.assertIn("No due date", page)
            self.assertIn("../../Legacy%20Tasks/%E7%A0%94%E7%A9%B6%20alpha/TASK.md", page)

    def test_sensitivity_is_filtered_before_counts_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_workspace(
                Path(temporary),
                [
                    ("20_任务/visible", _task("visible", title="Visible record")),
                    ("20_任务/secret", _task("secret", title="Hidden title", sensitivity="confidential")),
                    ("20_任务/restricted", _task("restricted", title="Restricted title", sensitivity="restricted")),
                ],
            )
            first = dashboard.generate_dashboard(root)
            first_page = self.output_bytes(root)["index.html"]
            self.assertIn(b"Visible record", first_page)
            self.assertNotIn(b"Hidden title", first_page)
            self.assertNotIn(b"Restricted title", first_page)
            hidden_path = root / "20_任务" / "secret" / "TASK.md"
            data, body = core.parse_task_bytes(hidden_path.read_bytes())
            data["title"] = "Changed but still hidden"
            data["updated"] = "2026-08-19T13:00:00+08:00"
            hidden_path.write_bytes(core.serialize_task(data, body))
            core.generate_indexes(root)
            second = dashboard.generate_dashboard(root)
            self.assertEqual(first["source_fingerprint"], second["source_fingerprint"])
            self.assertEqual(second["status"], "unchanged")

    def test_unknown_or_malformed_sensitivity_fails_without_touching_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_workspace(
                Path(temporary),
                [("20_任务/visible", _task("visible", title="Visible record"))],
            )
            dashboard.generate_dashboard(root)
            before = self.output_bytes(root)
            task_path = root / "20_任务" / "visible" / "TASK.md"
            task_path.write_text(
                task_path.read_text(encoding="utf-8").replace(
                    "sensitivity: internal", "sensitivity: mystery"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(core.WorkspaceError, "sensitivity"):
                dashboard.generate_dashboard(root)
            self.assertEqual(before, self.output_bytes(root))

    def test_html_script_bidi_and_url_injection_are_context_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adopted = "Legacy & Tasks/quotes ' and spaces"
            title = '</script><img src=x onerror="alert(1)"> \u202e & "docket"'
            root = self.make_workspace(
                Path(temporary),
                [
                    (
                        adopted,
                        _task(
                            "hostile-text",
                            title=title,
                            next_action="javascript:alert(1) <svg onload=alert(2)>",
                        ),
                    )
                ],
                adopted=(adopted,),
            )
            dashboard.generate_dashboard(root)
            page = self.output_bytes(root)["index.html"].decode("utf-8")
            self.assertNotIn("</script><img", page)
            self.assertNotIn("<svg onload", page)
            self.assertNotIn("\u202e", page)
            self.assertIn("&lt;/script&gt;&lt;img", page)
            self.assertIn("\ufffd", page)
            hrefs = re.findall(r'href="([^"]+)"', page)
            self.assertFalse(any(value.lower().startswith("javascript:") for value in hrefs))
            self.assertIn("Legacy%20%26%20Tasks/quotes%20%27%20and%20spaces/TASK.md", page)
            self.assertNotIn("Canonical body", page)

    def test_forged_catalog_traversal_and_symlink_targets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self.make_workspace(
                base / "forged",
                [("20_任务/visible", _task("visible", title="Visible record"))],
            )
            catalog_path = root / ".workspace-organizer" / "catalog" / "todo.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["items"][0]["record"] = "../../outside/TASK.md"
            catalog["source_sha256"] = hashlib.sha256(
                json.dumps(catalog["items"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(dashboard.StaleDashboard, "stale or not canonical"):
                dashboard.generate_dashboard(root)
            self.assertFalse((root / ".workspace-organizer" / "dashboard").exists())

            linked = self.make_workspace(
                base / "linked",
                [("20_任务/visible", _task("visible", title="Visible record"))],
            )
            outside = base / "outside"
            outside.mkdir()
            (linked / ".workspace-organizer" / "dashboard").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaisesRegex(dashboard.DashboardError, "no-follow directory"):
                dashboard.generate_dashboard(linked)
            self.assertEqual(list(outside.iterdir()), [])

            collision = self.make_workspace(
                base / "collision",
                [("20_任务/visible", _task("visible", title="Visible record"))],
            )
            output = collision / ".workspace-organizer" / "dashboard"
            output.mkdir()
            user_owned = output / "index.html"
            user_owned.write_text("user-owned dashboard\n", encoding="utf-8")
            with self.assertRaisesRegex(dashboard.DashboardError, "refusing to overwrite"):
                dashboard.generate_dashboard(collision)
            self.assertEqual(user_owned.read_text(encoding="utf-8"), "user-owned dashboard\n")

    def test_verify_reports_missing_catalog_and_changed_canonical_state_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_workspace(
                Path(temporary),
                [("20_任务/visible", _task("visible", title="Visible record"))],
            )
            self.assertEqual(dashboard.verify_dashboard(root)["status"], "stale")
            dashboard.generate_dashboard(root)
            self.assertEqual(dashboard.verify_dashboard(root)["status"], "current")
            task_path = root / "20_任务" / "visible" / "TASK.md"
            data, body = core.parse_task_bytes(task_path.read_bytes())
            data["title"] = "A newer canonical title"
            data["updated"] = "2026-08-19T13:00:00+08:00"
            task_path.write_bytes(core.serialize_task(data, body))
            self.assertEqual(dashboard.verify_dashboard(root)["status"], "stale")
            core.generate_indexes(root)
            stale = dashboard.verify_dashboard(root)
            self.assertEqual(stale["status"], "stale")
            self.assertIn("index.html", stale["mismatches"])
            dashboard.generate_dashboard(root)
            self.assertEqual(dashboard.verify_dashboard(root)["status"], "current")

    def test_static_application_has_no_network_persistence_or_mutation_surface(self) -> None:
        source = (SKILL_ROOT / "assets" / "dashboard" / "app.js").read_text(encoding="utf-8")
        for forbidden in (
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "sendBeacon",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "serviceWorker",
        ):
            self.assertNotIn(forbidden, source)
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_workspace(
                Path(temporary),
                [("20_任务/visible", _task("visible", title="Visible record"))],
            )
            task_before = (root / "20_任务" / "visible" / "TASK.md").read_bytes()
            dashboard.generate_dashboard(root)
            page = self.output_bytes(root)["index.html"].decode("utf-8")
            self.assertIn("Content-Security-Policy", page)
            self.assertIn("connect-src 'none'", page)
            for forbidden in ("<form", "contenteditable", "<textarea", "type=\"submit\""):
                self.assertNotIn(forbidden, page.lower())
            for forbidden in ("Approve", "Archive", "Update task"):
                self.assertNotIn(f">{forbidden}<", page)
            self.assertEqual(
                task_before, (root / "20_任务" / "visible" / "TASK.md").read_bytes()
            )

    def test_dashboard_absence_does_not_affect_v1_and_cli_exit_codes_are_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_workspace(
                Path(temporary),
                [("20_任务/visible", _task("visible", title="Visible record"))],
            )
            self.assertFalse((root / ".workspace-organizer" / "dashboard").exists())
            self.assertEqual(core.generate_indexes(root)["status"], "already_current")
            cli = SCRIPT_ROOT / "workspace_dashboard.py"
            missing = subprocess.run(
                [sys.executable, str(cli), "verify", str(root)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(missing.returncode, 1, missing.stderr)
            self.assertEqual(json.loads(missing.stdout)["status"], "stale")
            generated = subprocess.run(
                [sys.executable, str(cli), "generate", str(root)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            current = subprocess.run(
                [sys.executable, str(cli), "verify", str(root)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(current.returncode, 0, current.stderr)
            self.assertEqual(json.loads(current.stdout)["status"], "current")

    def test_installed_skill_package_runs_dashboard_generate_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            consumer = base / "consumer"
            consumer.mkdir()
            installed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "install_skill.py"),
                    "--target-root",
                    str(consumer),
                    "--yes",
                ],
                cwd=REPO_ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            root = self.make_workspace(
                base / "data",
                [("20_任务/visible", _task("visible", title="Installed package task"))],
            )
            cli = (
                consumer
                / ".agents"
                / "skills"
                / "workspace-organizer"
                / "scripts"
                / "workspace_dashboard.py"
            )
            for command, expected in (("generate", "generated"), ("verify", "current")):
                completed = subprocess.run(
                    [sys.executable, str(cli), command, str(root)],
                    cwd=consumer,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["status"], expected)

    def test_assets_cover_responsive_focus_contrast_and_reduced_motion(self) -> None:
        styles = (SKILL_ROOT / "assets" / "dashboard" / "styles.css").read_text(encoding="utf-8")
        for token in (
            "@media (max-width: 42rem)",
            "@media (prefers-reduced-motion: reduce)",
            "@media (prefers-contrast: more)",
            ":focus-visible",
            "overflow-wrap: anywhere",
        ):
            self.assertIn(token, styles)


if __name__ == "__main__":
    unittest.main()
