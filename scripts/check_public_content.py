#!/usr/bin/env python3
"""Fail when public repository content contains unsafe paths, secrets, or links."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple
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
_FileSnapshot = Tuple[int, int, int, int, int, int]
_TestHook = Optional[Callable[[str], None]]


def _file_snapshot(value: os.stat_result) -> _FileSnapshot:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def public_files(repo_root: Path) -> Iterable[Path]:
    for entry in PUBLIC_ENTRIES:
        path = repo_root / entry
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                try:
                    mode = child.lstat().st_mode
                except OSError:
                    yield child
                    continue
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    yield child


def _link_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    return raw.split(maxsplit=1)[0]


def validate_public_content(
    repo_root: Path,
    *,
    _test_hook: _TestHook = None,
) -> list[str]:
    repo_root = repo_root.resolve(strict=True)
    errors: list[str] = []
    files = list(public_files(repo_root))
    if not files:
        return ["no public files found"]
    for path in files:
        relative = path.relative_to(repo_root).as_posix()
        try:
            before = path.lstat()
        except OSError as exc:
            errors.append(f"{relative}: cannot stat public entry: {exc}")
            continue
        if stat.S_ISLNK(before.st_mode):
            errors.append(f"{relative}: public content must not be a symlink")
            continue
        if not stat.S_ISREG(before.st_mode):
            errors.append(f"{relative}: public content must be a regular file")
            continue
        size = before.st_size
        if size > MAX_PUBLIC_FILE_BYTES:
            errors.append(
                f"{relative}: {size} bytes exceeds public-file limit "
                f"{MAX_PUBLIC_FILE_BYTES}"
            )
            continue
        if _test_hook is not None:
            _test_hook(f"after-lstat:{relative}")
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            errors.append(f"{relative}: cannot read public file: {exc}")
            continue
        try:
            opened = os.fstat(descriptor)
            if _file_snapshot(opened) != _file_snapshot(before):
                if opened.st_size > MAX_PUBLIC_FILE_BYTES:
                    errors.append(
                        f"{relative}: {opened.st_size} bytes exceeds public-file limit "
                        f"{MAX_PUBLIC_FILE_BYTES}"
                    )
                else:
                    errors.append(f"{relative}: public file changed before read")
                continue
            if not stat.S_ISREG(opened.st_mode):
                errors.append(f"{relative}: public file changed before read")
                continue
            if opened.st_size > MAX_PUBLIC_FILE_BYTES:
                errors.append(
                    f"{relative}: {opened.st_size} bytes exceeds public-file limit "
                    f"{MAX_PUBLIC_FILE_BYTES}"
                )
                continue
            if _test_hook is not None:
                _test_hook(f"after-open:{relative}")
            chunks = []
            total = 0
            exceeded_limit = False
            while True:
                chunk = os.read(
                    descriptor,
                    min(1024 * 1024, MAX_PUBLIC_FILE_BYTES + 1 - total),
                )
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PUBLIC_FILE_BYTES:
                    exceeded_limit = True
                    break
                chunks.append(chunk)
            if _test_hook is not None:
                _test_hook(f"after-read:{relative}")
            after = os.fstat(descriptor)
            if exceeded_limit:
                errors.append(
                    f"{relative}: content exceeds public-file limit "
                    f"{MAX_PUBLIC_FILE_BYTES} while reading"
                )
            changed_during_read = _file_snapshot(opened) != _file_snapshot(after)
            if changed_during_read:
                errors.append(f"{relative}: public file changed during read")
            if exceeded_limit or changed_during_read:
                continue
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        if b"\x00" in payload:
            errors.append(f"{relative}: binary content is not allowed in public roots")
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeError:
            errors.append(f"{relative}: public file is not UTF-8 text")
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
