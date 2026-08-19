#!/usr/bin/env python3
"""Forward-test the public skill from an isolated repository installation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        rendered = " ".join(command[:3])
        raise RuntimeError(
            f"isolated command failed ({completed.returncode}): {rendered}: "
            f"{completed.stderr.strip()}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("isolated command did not emit JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("isolated command JSON must be an object")
    return value


def _task_text(
    *,
    task_id: str,
    title: str,
    status: str,
    area: str,
    next_action: Optional[str],
    updated: str,
    closed_at: Optional[str],
) -> str:
    fields = {
        "schema_version": 1,
        "id": task_id,
        "title": title,
        "status": status,
        "area": area,
        "type": "research" if area == "research" else "contract",
        "priority": "normal",
        "due": None,
        "sensitivity": "internal",
        "next_action": next_action,
        "updated": updated,
        "closed_at": closed_at,
        "archived_at": None,
        "tags": ["synthetic"],
    }
    bare = {"schema_version", "status", "area", "type", "priority", "sensitivity"}
    lines = ["---"]
    for key, value in fields.items():
        encoded = str(value) if key in bare else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {encoded}")
    lines.extend(["---", "", f"# {title}", "", "Public synthetic forward-test record.", ""])
    return "\n".join(lines)


def _approved_operation(
    cli: Path,
    root: Path,
    plan: Path,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    _run([sys.executable, str(cli), *command, "--output", str(plan)], cwd=cwd, environment=environment)
    dry = _run(
        [sys.executable, str(cli), "dry-run", str(root), "--plan", str(plan)],
        cwd=cwd,
        environment=environment,
    )
    if dry.get("status") != "ready":
        raise RuntimeError("isolated dry-run was not ready")
    approval = plan.with_suffix(".approval.json")
    _run(
        [
            sys.executable,
            str(cli),
            "approve",
            "--plan",
            str(plan),
            "--output",
            str(approval),
            "--yes",
        ],
        cwd=cwd,
        environment=environment,
    )
    applied = _run(
        [
            sys.executable,
            str(cli),
            "apply",
            str(root),
            "--plan",
            str(plan),
            "--approval",
            str(approval),
        ],
        cwd=cwd,
        environment=environment,
    )
    verified = _run(
        [sys.executable, str(cli), "verify", str(root), "--plan", str(plan)],
        cwd=cwd,
        environment=environment,
    )
    if verified.get("status") != "verified":
        raise RuntimeError("isolated apply did not verify")
    return applied


def forward_test(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve(strict=True)
    installer = repo_root / "scripts" / "install_skill.py"
    source = repo_root / "skill" / "workspace-organizer"
    with tempfile.TemporaryDirectory(prefix="workspace-organizer-forward-") as temporary:
        isolated = Path(temporary)
        consumer = isolated / "consumer-repository"
        consumer.mkdir()
        isolated_home = isolated / "home"
        isolated_codex = isolated / "codex-home"
        isolated_home.mkdir()
        isolated_codex.mkdir()
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(isolated_home),
                "CODEX_HOME": str(isolated_codex),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        environment.pop("PYTHONPATH", None)

        proposal = _run(
            [
                sys.executable,
                str(installer),
                "--source",
                str(source),
                "--target-root",
                str(consumer),
            ],
            cwd=repo_root,
            environment=environment,
        )
        if proposal.get("status") != "approval_required":
            raise RuntimeError("installer did not default to a read-only proposal")
        installed = _run(
            [
                sys.executable,
                str(installer),
                "--source",
                str(source),
                "--target-root",
                str(consumer),
                "--yes",
            ],
            cwd=repo_root,
            environment=environment,
        )
        if installed.get("status") != "installed":
            raise RuntimeError("skill was not installed")

        discovered = sorted((consumer / ".agents" / "skills").glob("*/SKILL.md"))
        if len(discovered) != 1 or discovered[0].parent.name != "workspace-organizer":
            raise RuntimeError("installed skill was not discoverable from public layout")
        skill_root = discovered[0].parent
        cli = skill_root / "scripts" / "workspace_organizer.py"
        if not cli.is_file():
            raise RuntimeError("installed skill is missing its deterministic CLI")

        plan_root = isolated / "plans"
        plan_root.mkdir()
        new_root = isolated / "new workspace"
        new_root.mkdir()
        _approved_operation(
            cli,
            new_root,
            plan_root / "new-init.json",
            ["plan-init", str(new_root), "--workspace-id", "forward-new"],
            cwd=consumer,
            environment=environment,
        )
        active_bundle = new_root / "20_任务" / "forward-research"
        active_bundle.mkdir()
        active_record = active_bundle / "TASK.md"
        active_record.write_text(
            _task_text(
                task_id="forward-research",
                title="Synthetic forward research",
                status="active",
                area="research",
                next_action="Review the isolated result",
                updated="2026-08-19T12:00:00+08:00",
                closed_at=None,
            ),
            encoding="utf-8",
        )
        first_index = _run(
            [sys.executable, str(cli), "index", str(new_root)],
            cwd=consumer,
            environment=environment,
        )
        if first_index.get("status") != "verified":
            raise RuntimeError("new-workspace views did not verify")
        todo = json.loads(
            (new_root / ".workspace-organizer" / "catalog" / "todo.json").read_text(
                encoding="utf-8"
            )
        )
        if [item["id"] for item in todo["items"]] != ["forward-research"]:
            raise RuntimeError("new-workspace TODO view did not use canonical task data")

        adopt_root = isolated / "adopt workspace"
        adopted_relative = "Existing Projects/研究 α"
        adopted_bundle = adopt_root / adopted_relative
        adopted_bundle.mkdir(parents=True)
        adopted_bundle.joinpath("TASK.md").write_text(
            _task_text(
                task_id="forward-adopted",
                title="Synthetic adopted contract",
                status="completed",
                area="operations",
                next_action=None,
                updated="2026-08-19T13:00:00+08:00",
                closed_at="2026-08-19T13:00:00+08:00",
            ),
            encoding="utf-8",
        )
        adopted_bundle.joinpath("records").mkdir()
        adopted_bundle.joinpath("records", "decision.md").write_text(
            "Public synthetic decision.\n", encoding="utf-8"
        )
        material_relative = "Legacy Library/资料 with spaces"
        material = adopt_root / material_relative
        material.mkdir(parents=True)
        material.joinpath("reference.txt").write_text(
            "Public synthetic reference.\n", encoding="utf-8"
        )
        adopt_root.joinpath("10_收件箱").mkdir()
        untouched = adopt_root / "Unmanaged notes" / "keep.txt"
        untouched.parent.mkdir()
        untouched.write_text("Public synthetic unmanaged note.\n", encoding="utf-8")
        adopted_record_before = adopted_bundle.joinpath("TASK.md").read_bytes()
        _approved_operation(
            cli,
            adopt_root,
            plan_root / "adopt-init.json",
            [
                "plan-init",
                str(adopt_root),
                "--workspace-id",
                "forward-adopt",
                "--adopt-task",
                adopted_relative,
                "--adopt-material",
                f"{material_relative}=internal",
                "--accept-existing-managed",
                "10_收件箱",
            ],
            cwd=consumer,
            environment=environment,
        )
        if adopted_bundle.joinpath("TASK.md").read_bytes() != adopted_record_before:
            raise RuntimeError("adoption rewrote the existing task")
        if not untouched.is_file():
            raise RuntimeError("adoption changed unrelated content")
        adopted_index = _run(
            [sys.executable, str(cli), "index", str(adopt_root)],
            cwd=consumer,
            environment=environment,
        )
        if adopted_index.get("status") != "verified":
            raise RuntimeError("adopted-workspace views did not verify")
        materials = json.loads(
            (adopt_root / ".workspace-organizer" / "catalog" / "materials.json").read_text(
                encoding="utf-8"
            )
        )
        expected_material = f"{material_relative}/reference.txt"
        if expected_material not in {item["path"] for item in materials["items"]}:
            raise RuntimeError("adopted material was not represented in generated views")

        _approved_operation(
            cli,
            adopt_root,
            plan_root / "adopt-archive.json",
            [
                "plan-archive",
                str(adopt_root),
                "--task-id",
                "forward-adopted",
                "--archived-at",
                "2026-08-19T14:00:00+08:00",
            ],
            cwd=consumer,
            environment=environment,
        )
        archive_destination = (
            adopt_root / "90_归档" / "2026" / "operations" / "forward-adopted"
        )
        config = json.loads(
            (adopt_root / ".workspace-organizer" / "config.json").read_text(encoding="utf-8")
        )
        if not archive_destination.is_dir() or adopted_bundle.exists():
            raise RuntimeError("adopted task did not archive exactly once")
        if config["adopted_task_paths"]:
            raise RuntimeError("archived adopted task registration was not removed")
        if not untouched.is_file():
            raise RuntimeError("archive changed unrelated content")

        return {
            "schema_version": 1,
            "operation": "distribution-forward-test",
            "status": "passed",
            "skill_discovered": True,
            "isolated_home": True,
            "new_workspace": ["initialize", "task-record", "index"],
            "adopted_workspace": ["adopt-in-place", "index", "archive"],
            "private_machine_state_used": False,
            "dashboard_required": False,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="public repository checkout to forward-test",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = forward_test(arguments.repo_root)
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"distribution-forward-test: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
