import hashlib
import json
import re
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skill" / "workspace-organizer"
VALIDATOR_ROOT = REPO_ROOT / "scripts"
sys.path.insert(0, str(VALIDATOR_ROOT))

from validate_workspace_model import (  # noqa: E402
    parse_task,
    validate_config,
    validate_generated_view,
    validate_task,
)


class SkillPackageTests(unittest.TestCase):
    def test_official_package_shape_is_focused(self) -> None:
        expected = {
            "SKILL.md",
            "agents/openai.yaml",
            "references/initialization-and-adoption.md",
            "references/task-contract.md",
            "references/views-and-archive.md",
            "assets/workspace-config.json",
            "assets/TASK.md",
            "assets/empty-generated-views/todo.json",
            "assets/empty-generated-views/timeline.json",
            "assets/empty-generated-views/materials.json",
            "assets/empty-generated-views/TODO.md",
            "assets/empty-generated-views/TIMELINE.md",
            "assets/empty-generated-views/MATERIALS.md",
        }
        actual = {
            path.relative_to(SKILL_ROOT).as_posix()
            for path in SKILL_ROOT.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, expected)
        self.assertFalse((SKILL_ROOT / "scripts").exists())

    def test_skill_metadata_and_progressive_disclosure(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertLess(len(text.splitlines()), 500)
        front_matter = text.split("---", 2)[1]
        self.assertRegex(front_matter, r"(?m)^name: workspace-organizer$")
        description = re.search(r"(?m)^description: (.+)$", front_matter).group(1).lower()
        for trigger in ("initialize", "adopt", "TASK.md", "archive"):
            self.assertIn(trigger.lower(), description)

        for reference in (
            "references/initialization-and-adoption.md",
            "references/task-contract.md",
            "references/views-and-archive.md",
        ):
            self.assertIn(reference, text)
            self.assertTrue((SKILL_ROOT / reference).is_file())

    def test_openai_yaml_uses_exact_interface_values(self) -> None:
        text = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertEqual(
            text,
            'interface:\n'
            '  display_name: "Workspace Organizer"\n'
            '  short_description: "Organize durable tasks and workspace materials"\n'
            '  default_prompt: "Use $workspace-organizer to initialize or safely organize this workspace."\n',
        )
        self.assertTrue(25 <= len("Organize durable tasks and workspace materials") <= 64)

    def test_canonical_templates_conform_to_v1_contract(self) -> None:
        config_path = SKILL_ROOT / "assets" / "workspace-config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        validate_config(config, str(config_path))

        task_path = SKILL_ROOT / "assets" / "TASK.md"
        task = parse_task(task_path)
        validate_task(task, str(task_path))
        self.assertEqual(task["sensitivity"], "internal")
        self.assertEqual(task["status"], "planned")

    def test_empty_generated_view_fixtures_are_consistent(self) -> None:
        fixtures = SKILL_ROOT / "assets" / "empty-generated-views"
        expected_digest = hashlib.sha256(b"[]").hexdigest()
        for view in ("todo", "timeline", "materials"):
            catalog = json.loads((fixtures / f"{view}.json").read_text(encoding="utf-8"))
            validate_generated_view(catalog, view)
            self.assertEqual(catalog["source_sha256"], expected_digest)
            markdown_name = {
                "todo": "TODO.md",
                "timeline": "TIMELINE.md",
                "materials": "MATERIALS.md",
            }[view]
            first_line = (fixtures / markdown_name).read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(
                first_line,
                f"<!-- workspace-organizer:generated view={view} schema=1 "
                f"source_sha256={expected_digest} -->",
            )

    def test_package_has_no_scaffold_or_private_machine_content(self) -> None:
        forbidden = (
            "[TODO",
            "PLACEHOLDER",
            "FIXME",
            "/Users/",
            "/home/",
            "file://",
            "gho_",
        )
        for path in SKILL_ROOT.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{token!r} found in {path}")

    def test_skill_routes_mutations_and_preserves_contract_guardrails(self) -> None:
        package_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in SKILL_ROOT.rglob("*.md")
        ).replace("\n", " ")
        for token in (
            "scan -> proposal -> dry-run -> approval -> apply",
            "deterministic scripts",
            "Never automatically overwrite, delete, publish",
            "public < internal < confidential < restricted",
            "90_归档/<closed-year>/<area>/<task-id>/",
            "leave the previous set unchanged",
        ):
            self.assertIn(token, package_text)


if __name__ == "__main__":
    unittest.main()
