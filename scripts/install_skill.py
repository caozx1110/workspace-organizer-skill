#!/usr/bin/env python3
"""Install the public skill package into one repository without overwriting it."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence


SKILL_NAME = "workspace-organizer"


class InstallError(ValueError):
    """Raised when a safe, no-replace installation cannot continue."""


def _resolved_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise InstallError(f"{label} must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InstallError(f"{label} is not an existing directory: {path}") from exc
    if not resolved.is_dir():
        raise InstallError(f"{label} is not a directory: {path}")
    return resolved


def _validate_source(source: Path) -> Path:
    resolved = _resolved_directory(source, "skill source")
    skill_file = resolved / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise InstallError("skill source must contain a regular SKILL.md")
    front_matter = skill_file.read_text(encoding="utf-8").split("---", 2)
    if len(front_matter) < 3 or "\nname: workspace-organizer\n" not in (
        "\n" + front_matter[1].strip() + "\n"
    ):
        raise InstallError("SKILL.md does not declare name: workspace-organizer")
    for path in resolved.rglob("*"):
        if path.is_symlink():
            raise InstallError(f"skill source contains a symlink: {path.relative_to(resolved)}")
    return resolved


def install_skill(source: Path, target_root: Path, *, confirmed: bool) -> dict:
    source = _validate_source(source)
    target_root = _resolved_directory(target_root, "target repository root")
    agents_parent = target_root / ".agents"
    skills_parent = agents_parent / "skills"
    destination = skills_parent / SKILL_NAME
    result = {
        "schema_version": 1,
        "operation": "install-skill",
        "skill": SKILL_NAME,
        "source": str(source),
        "destination": str(destination),
        "overwrite": False,
        "delete": False,
    }
    if destination.exists() or destination.is_symlink():
        raise InstallError(f"destination already exists; refusing to overwrite: {destination}")
    if not confirmed:
        return {**result, "status": "approval_required"}

    for path, label in (
        (agents_parent, "target .agents"),
        (skills_parent, "target .agents/skills"),
    ):
        if path.is_symlink():
            raise InstallError(f"{label} must not be a symlink")
        if path.exists() and not path.is_dir():
            raise InstallError(f"{label} must be a directory")
        path.mkdir(exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise InstallError(f"{label} must remain a real directory")
        try:
            path.resolve(strict=True).relative_to(target_root)
        except ValueError as exc:
            raise InstallError(f"{label} resolves outside the target repository") from exc
    if destination.exists() or destination.is_symlink():
        raise InstallError(f"destination appeared before install: {destination}")
    try:
        shutil.copytree(
            source,
            destination,
            symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
        )
    except FileExistsError as exc:
        raise InstallError(f"destination appeared during install: {destination}") from exc
    return {**result, "status": "installed"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("skill/workspace-organizer"),
        help="public workspace-organizer skill directory",
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        required=True,
        help="repository root that will receive .agents/skills/workspace-organizer",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the exact no-replace copy; omit for a read-only proposal",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = install_skill(
            arguments.source,
            arguments.target_root,
            confirmed=arguments.yes,
        )
    except (InstallError, OSError, UnicodeError) as exc:
        sys.stderr.write(f"install-skill: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
