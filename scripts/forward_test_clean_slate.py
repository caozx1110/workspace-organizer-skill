#!/usr/bin/env python3
"""Run a fresh-install smoke test for the clean-slate CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(cli: Path, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(cli), *args],
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "LANG": "C", "LC_ALL": "C"},
    )
    if completed.returncode:
        raise RuntimeError(f"clean-slate command failed: {args}: {completed.stderr.strip()}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("clean-slate command did not emit an object")
    return value


def run_forward(repo_root: Path) -> dict:
    source = (repo_root / "skill" / "workspace-organizer").resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="workspace-organizer-clean-slate-forward-") as raw:
        root = Path(raw)
        installed = root / ".agents" / "skills" / "workspace-organizer"
        installed.parent.mkdir(parents=True)
        shutil.copytree(source, installed, symlinks=False)
        cli = installed / "scripts" / "clean_slate.py"
        workspace = root / "vault"
        result = _run(cli, "init", str(workspace), "--yes")
        if result.get("status") != "initialized":
            raise RuntimeError("clean-slate init did not initialize")
        task_id = "20260824T000000-forward-smoke"
        created = _run(cli, "task", "create", str(workspace), "--id", task_id, "--title", "Forward smoke", "--outcome", "Smoke result", "--next-action", "Run smoke", "--yes")
        if created.get("task_id") != task_id:
            raise RuntimeError("clean-slate task create failed")
        generated = _run(cli, "views", "generate", str(workspace))
        if generated.get("status") not in {"generated", "unchanged"}:
            raise RuntimeError("clean-slate views failed")
        shown = _run(cli, "task", "show", str(workspace), "--task-id", task_id)
        if "body" in shown.get("task", {}):
            raise RuntimeError("task show leaked body without --include-body")
        return {"status": "passed", "operation": "clean-slate-forward-test", "installed": True, "views": True, "body_gated": True}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        print(json.dumps(run_forward(repo_root), ensure_ascii=False, sort_keys=True))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"clean-slate-forward-test: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
