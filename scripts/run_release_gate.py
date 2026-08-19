#!/usr/bin/env python3
"""Run the complete, dependency-free repository distribution gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


def _run(name: str, command: Sequence[str], repo_root: Path) -> dict:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        list(command),
        cwd=repo_root,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gate {name!r} failed with exit status {completed.returncode}")
    return {"name": name, "status": "passed"}


def _skill_creator_root(arguments: argparse.Namespace) -> Optional[Path]:
    raw = arguments.skill_creator_root or os.environ.get("SKILL_CREATOR_ROOT")
    if not raw:
        return None
    root = Path(raw)
    if root.is_symlink():
        raise RuntimeError("skill-creator root must not be a symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("skill-creator root is not available") from exc
    validator = root / "scripts" / "quick_validate.py"
    if not validator.is_file() or validator.is_symlink():
        raise RuntimeError("skill-creator root does not contain scripts/quick_validate.py")
    return root


def run_gate(repo_root: Path, skill_creator_root: Optional[Path]) -> dict:
    python = sys.executable
    steps = []
    if skill_creator_root is not None:
        steps.append(
            (
                "official-skill-quick-validator",
                [
                    python,
                    str(skill_creator_root / "scripts" / "quick_validate.py"),
                    "skill/workspace-organizer",
                ],
            )
        )
    steps.extend(
        [
            (
                "workspace-model-validator",
                [python, "scripts/validate_workspace_model.py", "examples/workspace"],
            ),
            (
                "focused-contract-and-tooling-tests",
                [
                    python,
                    "-m",
                    "unittest",
                    "-v",
                    "tests/test_skill_package.py",
                    "tests/test_workspace_model.py",
                    "tests/test_workspace_tooling.py",
                ],
            ),
            (
                "scenario-matrix-tests",
                [python, "-m", "unittest", "-v", "tests/test_workspace_scenarios.py"],
            ),
            (
                "distribution-readiness-tests",
                [python, "-m", "unittest", "-v", "tests/test_distribution_readiness.py"],
            ),
            (
                "dashboard-v2-tests",
                [python, "-m", "unittest", "-v", "tests/test_dashboard.py"],
            ),
            (
                "full-repository-tests",
                [python, "-m", "unittest", "discover", "-s", "tests", "-v"],
            ),
            ("public-content-hygiene", [python, "scripts/check_public_content.py"]),
            (
                "isolated-distribution-forward-test",
                [python, "scripts/forward_test_distribution.py"],
            ),
        ]
    )
    results = [_run(name, command, repo_root) for name, command in steps]
    return {
        "schema_version": 1,
        "operation": "repository-release-gate",
        "status": "passed",
        "official_skill_validator": (
            "passed" if skill_creator_root is not None else "not_requested"
        ),
        "steps": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository checkout to validate",
    )
    parser.add_argument(
        "--skill-creator-root",
        type=Path,
        help=(
            "portable skill-creator package root; may instead be supplied through "
            "SKILL_CREATOR_ROOT"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        repo_root = arguments.repo_root.resolve(strict=True)
        if not repo_root.is_dir():
            raise RuntimeError("repository root is not a directory")
        result = run_gate(repo_root, _skill_creator_root(arguments))
    except (OSError, RuntimeError) as exc:
        sys.stderr.write(f"release-gate: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
