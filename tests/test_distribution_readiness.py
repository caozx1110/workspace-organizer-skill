from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

    def assert_no_installer_artifacts(self, consumer: Path) -> None:
        skills = consumer / ".agents" / "skills"
        if not skills.exists():
            return
        self.assertFalse(list(skills.glob(".workspace-organizer.install-*")))
        self.assertFalse(list(skills.glob("*.failed")))
        unexpected_packages = [
            child
            for child in skills.iterdir()
            if child.name != "workspace-organizer"
            and child.is_dir()
            and (child / "SKILL.md").is_file()
        ]
        self.assertEqual(unexpected_packages, [])

    def installer_quarantines(self, consumer: Path) -> list[Path]:
        agents = consumer / ".agents"
        if not agents.exists():
            return []
        return sorted(agents.glob(".workspace-organizer.install-*"))

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
        self.assertIn("Task IDs are stable lowercase ASCII slugs.", self.english)
        self.assertIn("任务 ID 是稳定的小写 ASCII slug。", self.chinese)
        for guide in (self.english, self.chinese):
            self.assertIn("installed-with-durability-warning", guide)
        self.assertIn("quarantined outside", self.english)
        self.assertIn("非扫描位置隔离", self.chinese)

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

    def test_installer_fails_closed_when_target_parent_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            consumer = base / "consumer"
            skills = consumer / ".agents" / "skills"
            skills.mkdir(parents=True)
            moved_skills = consumer / ".agents" / "skills-original"
            outside = base / "outside"
            existing = outside / "workspace-organizer"
            existing.mkdir(parents=True)
            user_file = existing / "user-owned.txt"
            user_file.write_text("preserve me\n", encoding="utf-8")

            def swap_parent(stage: str) -> None:
                if stage == "target-parents-opened":
                    skills.rename(moved_skills)
                    skills.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(installer.InstallError, "identity changed"):
                installer.install_skill(
                    REPO_ROOT / "skill" / "workspace-organizer",
                    consumer,
                    confirmed=True,
                    _test_hook=swap_parent,
                )
            self.assertEqual(user_file.read_text(encoding="utf-8"), "preserve me\n")
            self.assertFalse((moved_skills / "workspace-organizer").exists())
            self.assertFalse(any(moved_skills.glob(".workspace-organizer.install-*")))

    def test_installer_never_follows_source_file_swapped_to_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            shutil.copytree(REPO_ROOT / "skill" / "workspace-organizer", source)
            consumer = base / "consumer"
            consumer.mkdir()
            outside = base / "outside-secret.txt"
            outside.write_text("must never be copied\n", encoding="utf-8")
            swapped = {"done": False}

            def swap_source(stage: str) -> None:
                if stage == "source-entry-statted:SKILL.md" and not swapped["done"]:
                    source.joinpath("SKILL.md").rename(source / "SKILL.original")
                    source.joinpath("SKILL.md").symlink_to(outside)
                    swapped["done"] = True

            with self.assertRaisesRegex(installer.InstallError, "changed before open"):
                installer.install_skill(
                    source,
                    consumer,
                    confirmed=True,
                    _test_hook=swap_source,
                )
            destination = consumer / ".agents" / "skills" / "workspace-organizer"
            self.assertFalse(destination.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "must never be copied\n")

    def test_installer_partial_failure_and_publish_collision_expose_no_partial_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = REPO_ROOT / "skill" / "workspace-organizer"
            interrupted = base / "interrupted"
            interrupted.mkdir()

            def interrupt_after_staging(stage: str) -> None:
                if stage == "staging-complete":
                    raise RuntimeError("injected before publish")

            with self.assertRaisesRegex(
                installer.InstallError,
                "injected before publish.*quarantined outside",
            ):
                installer.install_skill(
                    source,
                    interrupted,
                    confirmed=True,
                    _test_hook=interrupt_after_staging,
                )
            self.assertFalse(
                (interrupted / ".agents" / "skills" / "workspace-organizer").exists()
            )
            self.assert_no_installer_artifacts(interrupted)
            self.assertEqual(len(self.installer_quarantines(interrupted)), 1)

            collided = base / "collided"
            collided.mkdir()
            user_bytes = b"user-owned destination\n"

            def create_collision(stage: str) -> None:
                if stage == "staging-complete":
                    destination = collided / ".agents" / "skills" / "workspace-organizer"
                    destination.mkdir()
                    destination.joinpath("keep.txt").write_bytes(user_bytes)

            with self.assertRaisesRegex(installer.InstallError, "destination appeared"):
                installer.install_skill(
                    source,
                    collided,
                    confirmed=True,
                    _test_hook=create_collision,
                )
            self.assertEqual(
                (collided / ".agents" / "skills" / "workspace-organizer" / "keep.txt").read_bytes(),
                user_bytes,
            )
            self.assert_no_installer_artifacts(collided)
            self.assertEqual(len(self.installer_quarantines(collided)), 1)

    def test_installer_quarantines_mid_copy_and_base_exception_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = REPO_ROOT / "skill" / "workspace-organizer"
            mid_copy = base / "mid-copy"
            mid_copy.mkdir()

            def fail_mid_copy(stage: str) -> None:
                if stage == "source-entry-statted:references/tooling.md":
                    raise RuntimeError("injected mid-copy failure")

            with self.assertRaisesRegex(
                installer.InstallError,
                "injected mid-copy failure.*quarantined outside",
            ):
                installer.install_skill(
                    source,
                    mid_copy,
                    confirmed=True,
                    _test_hook=fail_mid_copy,
                )
            self.assert_no_installer_artifacts(mid_copy)
            self.assertFalse(
                (mid_copy / ".agents" / "skills" / "workspace-organizer").exists()
            )
            self.assertEqual(len(self.installer_quarantines(mid_copy)), 1)
            retry = installer.install_skill(source, mid_copy, confirmed=True)
            self.assertEqual(retry["status"], "installed")
            self.assertTrue(
                (
                    mid_copy
                    / ".agents"
                    / "skills"
                    / "workspace-organizer"
                    / "SKILL.md"
                ).is_file()
            )
            self.assertEqual(len(self.installer_quarantines(mid_copy)), 1)

            interrupted = base / "base-exception"
            interrupted.mkdir()

            def interrupt_after_staging(stage: str) -> None:
                if stage == "staging-complete":
                    raise KeyboardInterrupt()

            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "quarantined outside",
            ):
                installer.install_skill(
                    source,
                    interrupted,
                    confirmed=True,
                    _test_hook=interrupt_after_staging,
                )
            self.assert_no_installer_artifacts(interrupted)
            self.assertFalse(
                (interrupted / ".agents" / "skills" / "workspace-organizer").exists()
            )
            self.assertEqual(len(self.installer_quarantines(interrupted)), 1)

    def test_installer_never_unlinks_same_name_user_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            user_bytes = b"user replacement must survive\n"

            def replace_staged_file(stage: str) -> None:
                if stage != "staging-complete":
                    return
                quarantine = self.installer_quarantines(consumer)
                self.assertEqual(len(quarantine), 1)
                staged_skill = quarantine[0] / "SKILL.md"
                staged_skill.rename(quarantine[0] / "SKILL.operation-owned")
                staged_skill.write_bytes(user_bytes)
                raise RuntimeError("injected after same-name replacement")

            with mock.patch.object(
                installer.os,
                "unlink",
                side_effect=AssertionError("installer must not unlink"),
            ), mock.patch.object(
                installer.os,
                "rmdir",
                side_effect=AssertionError("installer must not rmdir"),
            ):
                with self.assertRaisesRegex(
                    installer.InstallError,
                    "same-name replacement.*quarantined outside",
                ):
                    installer.install_skill(
                        REPO_ROOT / "skill" / "workspace-organizer",
                        consumer,
                        confirmed=True,
                        _test_hook=replace_staged_file,
                    )
            quarantine = self.installer_quarantines(consumer)
            self.assertEqual(len(quarantine), 1)
            self.assertEqual((quarantine[0] / "SKILL.md").read_bytes(), user_bytes)
            self.assertTrue((quarantine[0] / "SKILL.operation-owned").is_file())
            self.assert_no_installer_artifacts(consumer)
            self.assertFalse(
                (consumer / ".agents" / "skills" / "workspace-organizer").exists()
            )

    def test_installer_never_deletes_directory_swapped_after_mkdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            consumer = base / "consumer"
            consumer.mkdir()
            replacement = base / "user-directory"
            replacement.mkdir()
            user_bytes = b"user directory bytes must survive\n"
            replacement.joinpath("keep.txt").write_bytes(user_bytes)

            def replace_created_root(stage: str) -> None:
                if stage != "staging-root-created":
                    return
                quarantine = self.installer_quarantines(consumer)
                self.assertEqual(len(quarantine), 1)
                quarantine[0].rename(consumer / ".agents" / "operation-created-root")
                replacement.rename(quarantine[0])

            with mock.patch.object(
                installer.os,
                "unlink",
                side_effect=AssertionError("installer must not unlink"),
            ), mock.patch.object(
                installer.os,
                "rmdir",
                side_effect=AssertionError("installer must not rmdir"),
            ):
                with self.assertRaisesRegex(
                    installer.InstallError,
                    "staged directory contents changed.*quarantined outside",
                ):
                    installer.install_skill(
                        REPO_ROOT / "skill" / "workspace-organizer",
                        consumer,
                        confirmed=True,
                        _test_hook=replace_created_root,
                    )
            quarantine = self.installer_quarantines(consumer)
            self.assertEqual(len(quarantine), 1)
            self.assertEqual((quarantine[0] / "keep.txt").read_bytes(), user_bytes)
            self.assertTrue((consumer / ".agents" / "operation-created-root").is_dir())
            self.assert_no_installer_artifacts(consumer)
            self.assertFalse(
                (consumer / ".agents" / "skills" / "workspace-organizer").exists()
            )

    def test_installer_reconciles_post_rename_parent_fsync_failure_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            source = REPO_ROOT / "skill" / "workspace-organizer"
            armed = {"value": False}
            real_fsync = installer.os.fsync

            def arm_failure(stage: str) -> None:
                if stage == "before-publish-fsync":
                    armed["value"] = True

            def fail_committed_parent_fsync(descriptor: int) -> None:
                if armed["value"]:
                    raise OSError("injected parent fsync failure")
                real_fsync(descriptor)

            with mock.patch.object(
                installer.os,
                "fsync",
                side_effect=fail_committed_parent_fsync,
            ):
                result = installer.install_skill(
                    source,
                    consumer,
                    confirmed=True,
                    _test_hook=arm_failure,
                )
            self.assertEqual(result["status"], "installed-with-durability-warning")
            self.assertEqual(result["durability"], "uncertain")
            destination = consumer / ".agents" / "skills" / "workspace-organizer"
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assert_no_installer_artifacts(consumer)
            with self.assertRaisesRegex(installer.InstallError, "refusing to overwrite"):
                installer.install_skill(source, consumer, confirmed=True)

    def test_installer_moves_changed_post_publish_entry_out_of_skill_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            user_bytes = b"post-publish user replacement must survive\n"

            def replace_published_entry(stage: str) -> None:
                if stage != "published":
                    return
                agents = consumer / ".agents"
                canonical = agents / "skills" / "workspace-organizer"
                canonical.rename(agents / "operation-published-root")
                canonical.mkdir()
                canonical.joinpath("keep.txt").write_bytes(user_bytes)

            with mock.patch.object(
                installer.os,
                "unlink",
                side_effect=AssertionError("installer must not unlink"),
            ), mock.patch.object(
                installer.os,
                "rmdir",
                side_effect=AssertionError("installer must not rmdir"),
            ):
                with self.assertRaisesRegex(
                    installer.InstallError,
                    "publish state is unknown.*no content was deleted",
                ):
                    installer.install_skill(
                        REPO_ROOT / "skill" / "workspace-organizer",
                        consumer,
                        confirmed=True,
                        _test_hook=replace_published_entry,
                    )
            quarantines = self.installer_quarantines(consumer)
            self.assertEqual(len(quarantines), 1)
            self.assertEqual((quarantines[0] / "keep.txt").read_bytes(), user_bytes)
            self.assertTrue(
                (consumer / ".agents" / "operation-published-root" / "SKILL.md").is_file()
            )
            self.assert_no_installer_artifacts(consumer)
            self.assertFalse(
                (consumer / ".agents" / "skills" / "workspace-organizer").exists()
            )

    def test_public_docs_examples_skill_and_fixtures_are_safe_and_linked(self) -> None:
        self.assertEqual(public_check.validate_public_content(REPO_ROOT), [])

    def test_public_scan_fails_closed_for_every_regular_file_and_link_boundary(self) -> None:
        cases = {
            "dot-env secret": ("docs/.env", ("GH_TOKEN=gh" + "p_" + "a" * 24).encode(), "GitHub token"),
            "rst private path": ("docs/notes.rst", ("See /" + "Users/example/private.txt\n").encode(), "macOS home path"),
            "extensionless secret": ("docs/credentials", b"password=synthetic-value\n", "assigned password"),
            "invalid UTF-8": ("docs/invalid.data", b"\xff\xfe", "not UTF-8 text"),
            "binary": ("docs/blob.bin", b"public\x00binary", "binary content"),
            "large": ("docs/large.rst", b"L" * (public_check.MAX_PUBLIC_FILE_BYTES + 1), "exceeds public-file limit"),
            "broken link": ("docs/broken.md", b"[broken](missing.md)\n", "broken local link"),
            "escape link": ("docs/escape.md", b"[escape](../../outside.txt)\n", "link escapes repository"),
        }
        for name, (relative, payload, expected) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / relative
                target.parent.mkdir(parents=True)
                target.write_bytes(payload)
                errors = public_check.validate_public_content(root)
                self.assertTrue(any(expected in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docs = root / "docs"
            docs.mkdir()
            outside = root / "outside.txt"
            outside.write_text("public synthetic\n", encoding="utf-8")
            docs.joinpath("linked.txt").symlink_to(outside)
            errors = public_check.validate_public_content(root)
            self.assertTrue(any("must not be a symlink" in error for error in errors), errors)

    def test_public_scan_rejects_same_inode_growth_shrink_and_replacement_races(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "docs" / "growing.txt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")
            original_inode = target.stat().st_ino

            def grow_after_open(stage: str) -> None:
                if stage == "after-open:docs/growing.txt":
                    target.write_bytes(b"x" * (public_check.MAX_PUBLIC_FILE_BYTES + 1))
                    self.assertEqual(target.stat().st_ino, original_inode)

            errors = public_check.validate_public_content(root, _test_hook=grow_after_open)
            self.assertTrue(any("exceeds public-file limit" in error for error in errors), errors)
            self.assertTrue(any("changed during read" in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "docs" / "shrinking.txt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"synthetic public text\n")

            def shrink_after_open(stage: str) -> None:
                if stage == "after-open:docs/shrinking.txt":
                    target.write_bytes(b"")

            errors = public_check.validate_public_content(root, _test_hook=shrink_after_open)
            self.assertTrue(any("changed during read" in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "docs" / "replaced.txt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"original public text\n")

            def replace_after_lstat(stage: str) -> None:
                if stage == "after-lstat:docs/replaced.txt":
                    target.rename(target.with_suffix(".original"))
                    target.write_bytes(b"replacement public text\n")

            errors = public_check.validate_public_content(root, _test_hook=replace_after_lstat)
            self.assertTrue(any("changed before read" in error for error in errors), errors)

    def test_isolated_installed_package_runs_representative_flows(self) -> None:
        result = forward_test.forward_test(REPO_ROOT)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["skill_discovered"])
        self.assertTrue(result["isolated_home"])
        self.assertFalse(result["private_machine_state_used"])
        self.assertFalse(result["environment_probe"]["sentinel_propagated"])
        self.assertEqual(result["environment_probe"]["sensitive_names_present"], [])
        self.assertEqual(result["environment_probe"]["unexpected_names_present"], [])
        self.assertTrue(
            set(result["environment_probe"]["platform_injected_names"])
            <= forward_test.PLATFORM_CHILD_ENV_NAMES
        )
        self.assertFalse(result["dashboard_required"])
        self.assertEqual(
            result["new_workspace"], ["initialize", "task-record", "index"]
        )
        self.assertEqual(
            result["adopted_workspace"], ["adopt-in-place", "index", "archive"]
        )

    def test_forward_environment_is_minimal_and_child_probe_filters_sensitive_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ambient = {
                name: "synthetic-sensitive-value"
                for name in forward_test.FORBIDDEN_ENV_NAMES
            }
            ambient.update({"PATH": "/developer/bin", "LANG": "C"})
            environment = forward_test._minimal_environment(Path(temporary), ambient)
            self.assertEqual(environment["PATH"], os.defpath)
            self.assertTrue(forward_test.FORBIDDEN_ENV_NAMES.isdisjoint(environment))
            self.assertNotIn("USER", environment)
            self.assertNotIn("LOGNAME", environment)
            probe = forward_test._probe_environment(environment)
            self.assertFalse(probe["sentinel_propagated"])
            self.assertEqual(probe["sensitive_names_present"], [])
            self.assertEqual(probe["unexpected_names_present"], [])
            self.assertTrue(
                set(probe["platform_injected_names"])
                <= forward_test.PLATFORM_CHILD_ENV_NAMES
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
