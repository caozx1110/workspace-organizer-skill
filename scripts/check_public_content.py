#!/usr/bin/env python3
"""Fail when public repository content contains unsafe paths, secrets, or links."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.parse import unquote, urlsplit


PUBLIC_ENTRIES = (
    "README.md",
    "LICENSE",
    "contracts",
    "docs",
    "examples",
    "schemas",
    "scripts",
    "skill",
    "tests/fixtures",
)
MAX_PUBLIC_FILE_BYTES = 512 * 1024
TEXT_SUFFIXES = {".json", ".md", ".py", ".txt", ".yaml", ".yml"}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PRIVATE_PATTERNS = (
    ("macOS home path", re.compile(r"/(?:Users)/[^/\s]+/")),
    ("Linux home path", re.compile(r"/(?:home)/[^/\s]+/")),
    ("Windows home path", re.compile(r"[A-Za-z]:\\(?:Users)\\[^\\\s]+\\")),
    ("file URI", re.compile("file:" + "//", re.IGNORECASE)),
    ("GitHub token", re.compile(r"gh[opusr]_[A-Za-z0-9]{20,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("assigned password", re.compile(r"(?i)\bpassword\s*=\s*[^\s]+")),
    ("assigned token", re.compile(r"(?i)\btoken\s*=\s*[^\s]+")),
)


def public_files(repo_root: Path) -> Iterable[Path]:
    for entry in PUBLIC_ENTRIES:
        path = repo_root / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() or child.is_symlink():
                    yield child


def _link_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    return raw.split(maxsplit=1)[0]


def validate_public_content(repo_root: Path) -> list[str]:
    repo_root = repo_root.resolve(strict=True)
    errors: list[str] = []
    files = list(public_files(repo_root))
    if not files:
        return ["no public files found"]
    for path in files:
        relative = path.relative_to(repo_root).as_posix()
        if path.is_symlink():
            errors.append(f"{relative}: public content must not be a symlink")
            continue
        size = path.stat().st_size
        if size > MAX_PUBLIC_FILE_BYTES:
            errors.append(
                f"{relative}: {size} bytes exceeds public-file limit "
                f"{MAX_PUBLIC_FILE_BYTES}"
            )
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"{relative}: declared public text is not UTF-8")
            continue
        for label, pattern in PRIVATE_PATTERNS:
            if pattern.search(text):
                errors.append(f"{relative}: contains {label}")
        if path.suffix.lower() != ".md":
            continue
        for match in MARKDOWN_LINK.finditer(text):
            target = _link_target(match.group(1))
            split = urlsplit(target)
            if split.scheme in {"http", "https", "mailto"} or target.startswith("#"):
                continue
            if split.scheme or target.startswith("/"):
                errors.append(f"{relative}: unsupported public link target {target!r}")
                continue
            decoded = unquote(split.path)
            candidate = (path.parent / decoded).resolve()
            try:
                candidate.relative_to(repo_root)
            except ValueError:
                errors.append(f"{relative}: link escapes repository: {target!r}")
                continue
            if not candidate.exists():
                errors.append(f"{relative}: broken local link {target!r}")
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's checkout)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    errors = validate_public_content(arguments.repo_root)
    result = {
        "schema_version": 1,
        "operation": "check-public-content",
        "status": "failed" if errors else "passed",
        "roots": list(PUBLIC_ENTRIES),
        "errors": errors,
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
