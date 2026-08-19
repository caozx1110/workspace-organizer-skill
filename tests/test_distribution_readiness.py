from __future__ import annotations

import importlib.util
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENGLISH_GUIDE = REPO_ROOT / "docs" / "guide.en.md"
CHINESE_GUIDE = REPO_ROOT / "docs" / "guide.zh-CN.md"
INSTALLER_PATH = REPO_ROOT / "scripts" / "install_skill.py"
PUBLIC_CHECK_PATH = REPO_ROOT / "scripts" / "check_public_content.py"
FORWARD_TEST_PATH = REPO_ROOT / "scripts" / "forward_test_distribution.py"


def _load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    assert specification.loader is not None
    specification.loader.exec_module(module)
    return module


installer = _load_module("distribution_installer", INSTALLER_PATH)
public_check = _load_module("distribution_public_check", PUBLIC_CHECK_PATH)
forward_test = _load_module("distribution_forward_test", FORWARD_TEST_PATH)


COVERAGE_IDS = (
    "purpose",
    "prerequisites",
    "installation",
    "concepts",
    "safety",
    "initialize",
    "adoption",
    "task-updates",
    "views",
    "archive",
    "rollback",
    "limits",
    "validation",
)


class DistributionReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.english = ENGLISH_GUIDE.read_text(encoding="utf-8")
        cls.chinese = CHINESE_GUIDE.read_text(encoding="utf-8")

    def test_bilingual_guides_have_equivalent_auditable_coverage(self) -> None:
        for coverage_id in COVERAGE_IDS:
            marker = f"<!-- coverage:{coverage_id} -->"
            self.assertEqual(self.english.count(marker), 1, marker)
            self.assertEqual(self.chinese.count(marker), 1, marker)
        for token in (
            "dry-run",
            "exact approval",
            "no overwrite",
            "no deletion",
            "no publication",
            "Issue #7",
            "read-only",
            "second source of truth",
        ):
            self.assertIn(token, self.english)
        for token in (
            "试运行",
            "精确批准",
            "不覆盖",
            "不删除",
            "不发布",
            "Issue #7",
            "只读",
            "第二数据源",
        ):
            self.assertIn(token, self.chinese)

    def test_documented_validation_commands_and_prerequisites_are_exact(self) -> None:
        commands = (
            "python3 scripts/check_public_content.py",
            "python3 scripts/forward_test_distribution.py",
            "python3 scripts/run_release_gate.py",
            'python3 scripts/run_release_gate.py --skill-creator-root "$SKILL_CREATOR_ROOT"',
        )
        for guide in (self.english, self.chinese):
            self.assertIn("Python 3.9", guide)
            self.assertIn("SKILL_CREATOR_ROOT", guide)
            self.assertIn("scripts/quick_validate.py", guide)
            for command in commands:
                self.assertIn(command, guide)
        for script in (
            INSTALLER_PATH,
            PUBLIC_CHECK_PATH,
            FORWARD_TEST_PATH,
            REPO_ROOT / "scripts" / "run_release_gate.py",
        ):
            completed = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=REPO_ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_bilingual_operational_commands_use_the_real_cli_surface(self) -> None:
        def commands(guide: str) -> list[list[str]]:
            lines = re.findall(r'^python3 "\$WO_TOOL" .+$', guide, flags=re.MULTILINE)
            return [shlex.split(line) for line in lines]

        english_commands = commands(self.english)
        chinese_commands = commands(self.chinese)
        self.assertEqual(english_commands, chinese_commands)
        subcommands = [command[2] for command in english_commands]
        self.assertEqual(
            set(subcommands),
            {
                "inventory",
                "plan-init",
                "dry-run",
                "approve",
                "apply",
                "verify",
                "index",
                "plan-archive",
            },
        )
        cli = REPO_ROOT / "skill" / "workspace-organizer" / "scripts" / "workspace_organizer.py"
        for subcommand in sorted(set(subcommands)):
            completed = subprocess.run(
                [sys.executable, str(cli), subcommand, "--help"],
                cwd=REPO_ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_installer_is_discoverable_no_replace_and_no_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            source = REPO_ROOT / "skill" / "workspace-organizer"
            proposal = installer.install_skill(source, consumer, confirmed=False)
            destination = consumer / ".agents" / "skills" / "workspace-organizer"
            self.assertEqual(proposal["status"], "approval_required")
            self.assertFalse(destination.exists())
            installed = installer.install_skill(source, consumer, confirmed=True)
            self.assertEqual(installed["status"], "installed")
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue(
                (destination / "scripts" / "workspace_organizer.py").is_file()
            )
            self.assertFalse(any(destination.rglob("__pycache__")))
            self.assertFalse(any(destination.rglob("*.pyc")))
            self.assertFalse(installed["overwrite"])
            self.assertFalse(installed["delete"])
            with self.assertRaisesRegex(installer.InstallError, "refusing to overwrite"):
                installer.install_skill(source, consumer, confirmed=True)
            self.assertTrue((destination / "SKILL.md").is_file())

            outside = Path(temporary) / "outside"
            outside.mkdir()
            linked_consumer = Path(temporary) / "linked-consumer"
            linked_consumer.mkdir()
            linked_consumer.joinpath(".agents").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(installer.InstallError, "must not be a symlink"):
                installer.install_skill(source, linked_consumer, confirmed=True)
            self.assertEqual(list(outside.iterdir()), [])

    def test_public_docs_examples_skill_and_fixtures_are_safe_and_linked(self) -> None:
        self.assertEqual(public_check.validate_public_content(REPO_ROOT), [])

    def test_isolated_installed_package_runs_representative_flows(self) -> None:
        result = forward_test.forward_test(REPO_ROOT)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["skill_discovered"])
        self.assertTrue(result["isolated_home"])
        self.assertFalse(result["private_machine_state_used"])
        self.assertFalse(result["dashboard_required"])
        self.assertEqual(
            result["new_workspace"], ["initialize", "task-record", "index"]
        )
        self.assertEqual(
            result["adopted_workspace"], ["adopt-in-place", "index", "archive"]
        )

    def test_gate_has_portable_optional_official_validator_and_no_release_action(self) -> None:
        gate = (REPO_ROOT / "scripts" / "run_release_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("--skill-creator-root", gate)
        self.assertIn("SKILL_CREATOR_ROOT", gate)
        self.assertIn('"scripts" / "quick_validate.py"', gate)
        for forbidden in (
            "/.codex/",
            "/" + "Users/",
            "/" + "home/",
            "git tag",
            "gh release",
        ):
            self.assertNotIn(forbidden, gate)


if __name__ == "__main__":
    unittest.main()
