#!/usr/bin/env python3
"""Install the public skill package with descriptor-anchored no-replace copy."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, NoReturn, Optional, Sequence, Tuple


SKILL_NAME = "workspace-organizer"
IGNORED_NAMES = {"__pycache__", ".DS_Store"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_NEW_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_Identity = Tuple[int, int, int]
_OwnedManifest = Dict[str, _Identity]
_TestHook = Optional[Callable[[str], None]]


class InstallError(ValueError):
    """Raised when a safe, no-replace installation cannot continue."""


def _require_platform_guards() -> None:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise InstallError("installation requires POSIX O_DIRECTORY and O_NOFOLLOW")


def _identity(value: os.stat_result) -> _Identity:
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        _identity(before) == _identity(after)
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def _open_directory_path(path: Path, label: str) -> Tuple[Path, int, _Identity]:
    if path.is_symlink():
        raise InstallError(f"{label} must not be a symlink")
    lexical = Path(os.path.realpath(os.path.abspath(path)))
    if lexical.anchor != os.sep:
        raise InstallError(f"{label} must resolve from a POSIX root")
    try:
        descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise InstallError(f"{label} root cannot be opened safely") from exc
    try:
        for component in lexical.parts[1:]:
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise InstallError(f"{label} contains a symlink component")
            if not stat.S_ISDIR(before.st_mode):
                raise InstallError(f"{label} is not a directory")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            opened = os.fstat(child)
            if _identity(before) != _identity(opened):
                os.close(child)
                raise InstallError(f"{label} changed while it was opened")
            os.close(descriptor)
            descriptor = child
        opened = os.fstat(descriptor)
        return lexical, descriptor, _identity(opened)
    except (InstallError, OSError):
        os.close(descriptor)
        raise


def _open_or_create_directory_at(parent_fd: int, name: str, label: str) -> Tuple[int, _Identity]:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent_fd)
            os.fsync(parent_fd)
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileExistsError:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise InstallError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(before.st_mode):
        raise InstallError(f"{label} must be a real directory")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise InstallError(f"{label} cannot be opened without following links") from exc
    opened = os.fstat(descriptor)
    if _identity(before) != _identity(opened):
        os.close(descriptor)
        raise InstallError(f"{label} changed while it was opened")
    return descriptor, _identity(opened)


def _assert_directory_at(parent_fd: int, name: str, expected: _Identity, label: str) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise InstallError(f"{label} disappeared before publish") from exc
    if _identity(current) != expected or not stat.S_ISDIR(current.st_mode):
        raise InstallError(f"{label} identity changed before publish")


def _assert_target_chain(
    target_fd: int,
    agents_fd: int,
    agents_identity: _Identity,
    skills_identity: _Identity,
) -> None:
    _assert_directory_at(target_fd, ".agents", agents_identity, "target .agents")
    _assert_directory_at(agents_fd, "skills", skills_identity, "target .agents/skills")


def _ignored(name: str) -> bool:
    return name in IGNORED_NAMES or PurePosixPath(name).suffix in IGNORED_SUFFIXES


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while staging skill")
        view = view[written:]


def _copy_regular_file(
    source_parent_fd: int,
    destination_parent_fd: int,
    name: str,
    relative: str,
    before: os.stat_result,
    owned: _OwnedManifest,
    test_hook: _TestHook,
) -> None:
    if test_hook is not None:
        test_hook(f"source-entry-statted:{relative}")
    try:
        source_fd = os.open(name, _READ_FLAGS, dir_fd=source_parent_fd)
    except OSError as exc:
        raise InstallError(f"source entry changed before open: {relative}") from exc
    destination_fd: Optional[int] = None
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            raise InstallError(f"source entry changed before copy: {relative}")
        destination_fd = os.open(
            name,
            _WRITE_NEW_FLAGS,
            stat.S_IMODE(opened.st_mode) & 0o777,
            dir_fd=destination_parent_fd,
        )
        created = os.fstat(destination_fd)
        if not stat.S_ISREG(created.st_mode):
            raise InstallError(f"staged entry is not a regular file: {relative}")
        owned[relative] = _identity(created)
        while True:
            payload = os.read(source_fd, 1024 * 1024)
            if not payload:
                break
            _write_all(destination_fd, payload)
        os.fsync(destination_fd)
        os.fchmod(destination_fd, stat.S_IMODE(opened.st_mode) & 0o777)
        after = os.fstat(source_fd)
        if not _same_file_snapshot(opened, after):
            raise InstallError(f"source file changed during copy: {relative}")
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _copy_directory_tree(
    source_fd: int,
    destination_fd: int,
    relative_parent: str,
    owned: _OwnedManifest,
    test_hook: _TestHook,
) -> None:
    initial_names = sorted(name for name in os.listdir(source_fd) if not _ignored(name))
    for name in initial_names:
        relative = f"{relative_parent}/{name}" if relative_parent else name
        try:
            before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except OSError as exc:
            raise InstallError(f"source entry disappeared: {relative}") from exc
        if stat.S_ISLNK(before.st_mode):
            raise InstallError(f"source contains a symlink: {relative}")
        if stat.S_ISDIR(before.st_mode):
            if test_hook is not None:
                test_hook(f"source-entry-statted:{relative}")
            try:
                child_source_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=source_fd)
            except OSError as exc:
                raise InstallError(f"source directory changed before open: {relative}") from exc
            opened = os.fstat(child_source_fd)
            if _identity(before) != _identity(opened):
                os.close(child_source_fd)
                raise InstallError(f"source directory changed before copy: {relative}")
            try:
                os.mkdir(name, mode=0o755, dir_fd=destination_fd)
                if test_hook is not None:
                    test_hook(f"staged-directory-created:{relative}")
                child_destination_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=destination_fd)
                try:
                    created_identity = _identity(os.fstat(child_destination_fd))
                    current = os.stat(
                        name,
                        dir_fd=destination_fd,
                        follow_symlinks=False,
                    )
                    if _identity(current) != created_identity:
                        raise InstallError(f"staged directory changed before copy: {relative}")
                    owned[relative] = created_identity
                    _copy_directory_tree(
                        child_source_fd,
                        child_destination_fd,
                        relative,
                        owned,
                        test_hook,
                    )
                    os.fsync(child_destination_fd)
                finally:
                    os.close(child_destination_fd)
            finally:
                os.close(child_source_fd)
        elif stat.S_ISREG(before.st_mode):
            _copy_regular_file(
                source_fd,
                destination_fd,
                name,
                relative,
                before,
                owned,
                test_hook,
            )
        else:
            raise InstallError(f"source contains a non-regular entry: {relative}")
    final_names = sorted(name for name in os.listdir(source_fd) if not _ignored(name))
    if final_names != initial_names:
        raise InstallError(f"source directory changed during copy: {relative_parent or '.'}")


def _read_regular_file_at(
    parent_fd: int,
    name: str,
    label: str,
    expected: _Identity,
) -> bytes:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise InstallError(f"staged package is missing {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != expected:
            raise InstallError(f"staged {label} identity changed before validation")
        chunks = []
        while True:
            payload = os.read(descriptor, 1024 * 1024)
            if not payload:
                break
            chunks.append(payload)
        after = os.fstat(descriptor)
        if not _same_file_snapshot(opened, after):
            raise InstallError(f"staged {label} changed during validation")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_staged_skill(staging_fd: int, expected: _Identity) -> None:
    try:
        text = _read_regular_file_at(
            staging_fd,
            "SKILL.md",
            "SKILL.md",
            expected,
        ).decode("utf-8")
    except UnicodeError as exc:
        raise InstallError("staged SKILL.md is not UTF-8") from exc
    front_matter = text.split("---", 2)
    if len(front_matter) < 3 or "\nname: workspace-organizer\n" not in (
        "\n" + front_matter[1].strip() + "\n"
    ):
        raise InstallError("SKILL.md does not declare name: workspace-organizer")


def _rename_noreplace_at(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    result = -1
    if sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        function = library.renameatx_np
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_parent_fd,
            source,
            destination_parent_fd,
            destination,
            0x00000004 | 0x00000010,
        )
    elif hasattr(library, "renameat2"):
        function = library.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_parent_fd,
            source,
            destination_parent_fd,
            destination,
            0x00000001,
        )
    else:
        raise InstallError("platform lacks atomic no-replace directory publish")
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise InstallError("destination appeared during atomic publish")
        raise InstallError("atomic no-replace publish failed") from OSError(
            error_number, os.strerror(error_number)
        )


def _verify_owned_contents(
    directory_fd: int,
    relative_parent: str,
    owned: _OwnedManifest,
) -> None:
    """Require the staged tree to match the descriptor-recorded manifest exactly."""
    prefix = f"{relative_parent}/" if relative_parent else ""
    expected_names = {
        remainder
        for relative in owned
        if relative.startswith(prefix)
        for remainder in [relative[len(prefix) :]]
        if remainder and "/" not in remainder
    }
    actual_names = set(os.listdir(directory_fd))
    if actual_names != expected_names:
        raise InstallError(
            f"staged directory contents changed: {relative_parent or '.'}"
        )
    for name in sorted(actual_names):
        relative = f"{relative_parent}/{name}" if relative_parent else name
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        expected = owned.get(relative)
        if expected is None or _identity(current) != expected:
            raise InstallError(f"staged entry is no longer operation-owned: {relative}")
        if stat.S_ISDIR(current.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                if _identity(os.fstat(child_fd)) != expected:
                    raise InstallError(f"staged directory changed during verification: {relative}")
                _verify_owned_contents(child_fd, relative, owned)
            finally:
                os.close(child_fd)
        elif not stat.S_ISREG(current.st_mode):
            raise InstallError(f"staged entry has unsafe type: {relative}")


def _published_install_visible(
    target_fd: int,
    agents_fd: int,
    agents_identity: _Identity,
    skills_fd: int,
    skills_identity: _Identity,
    published_identity: _Identity,
) -> bool:
    try:
        _assert_target_chain(target_fd, agents_fd, agents_identity, skills_identity)
        _assert_directory_at(skills_fd, SKILL_NAME, published_identity, "published skill")
    except (InstallError, OSError):
        return False
    return True


def _entry_identity_at(parent_fd: int, name: str) -> Optional[_Identity]:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _identity(current)


def _agents_directory_visible(
    target_fd: int,
    agents_identity: _Identity,
) -> bool:
    try:
        _assert_directory_at(target_fd, ".agents", agents_identity, "target .agents")
    except (InstallError, OSError):
        return False
    return True


def _installation_state(
    target_fd: int,
    agents_fd: int,
    agents_identity: _Identity,
    skills_fd: int,
    skills_identity: _Identity,
    staging_name: str,
    staging_identity: _Identity,
) -> str:
    """Reconstruct commit state from current paths; never trust control flow."""
    try:
        staged = _entry_identity_at(agents_fd, staging_name)
        canonical = _entry_identity_at(skills_fd, SKILL_NAME)
    except OSError:
        return "unknown"
    if (
        staged == staging_identity
        and canonical != staging_identity
        and _agents_directory_visible(target_fd, agents_identity)
    ):
        return "quarantined"
    if (
        staged is None
        and canonical == staging_identity
        and _published_install_visible(
            target_fd,
            agents_fd,
            agents_identity,
            skills_fd,
            skills_identity,
            staging_identity,
        )
    ):
        return "installed"
    return "unknown"


def _quarantine_relative(staging_name: str) -> str:
    return f".agents/{staging_name}"


def _raise_quarantined(
    cause: BaseException,
    staging_name: str,
    *,
    after_publish: bool,
) -> NoReturn:
    phase = "publish was reverted" if after_publish else "nothing was published"
    detail = str(cause).strip() or cause.__class__.__name__
    message = (
        f"{detail}; {phase}; evidence remains quarantined outside the skill scan at "
        f"{_quarantine_relative(staging_name)}; inspect it manually before removal"
    )
    if isinstance(cause, KeyboardInterrupt):
        raise KeyboardInterrupt(message) from cause
    raise InstallError(message) from cause


def _installed_warning(result: Dict[str, object]) -> Dict[str, object]:
    return {
        **result,
        "status": "installed-with-durability-warning",
        "durability": "uncertain",
        "warning": (
            "the exact canonical install is visible, but operation completion or "
            "parent durability could not be confirmed"
        ),
    }


def _quarantine_published_skill(
    agents_fd: int,
    skills_fd: int,
    staging_name: str,
    published_identity: _Identity,
) -> None:
    """Atomically move the current canonical entry out of the scan path."""
    _rename_noreplace_at(
        skills_fd,
        SKILL_NAME,
        agents_fd,
        staging_name,
    )
    moved = os.stat(staging_name, dir_fd=agents_fd, follow_symlinks=False)
    try:
        os.stat(SKILL_NAME, dir_fd=skills_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise InstallError(
            "publish state is unknown because the canonical entry remained after reconcile; "
            "no content was deleted"
        )
    if _identity(moved) != published_identity or not stat.S_ISDIR(moved.st_mode):
        raise InstallError(
            "publish state is unknown because a changed canonical entry was moved to "
            f"quarantine at {_quarantine_relative(staging_name)}; no content was deleted"
        )


def install_skill(
    source: Path,
    target_root: Path,
    *,
    confirmed: bool,
    _test_hook: _TestHook = None,
) -> Dict[str, object]:
    _require_platform_guards()
    source_lexical, source_fd, _ = _open_directory_path(source, "skill source")
    try:
        target_lexical, target_fd, _ = _open_directory_path(
            target_root, "target repository root"
        )
    except BaseException:
        os.close(source_fd)
        raise
    agents_fd: Optional[int] = None
    skills_fd: Optional[int] = None
    staging_fd: Optional[int] = None
    staging_name: Optional[str] = None
    staging_identity: Optional[_Identity] = None
    agents_identity: Optional[_Identity] = None
    skills_identity: Optional[_Identity] = None
    owned: _OwnedManifest = {}
    observed_commit = False
    try:
        result: Dict[str, object] = {
            "schema_version": 1,
            "operation": "install-skill",
            "skill": SKILL_NAME,
            "source": str(source_lexical),
            "destination": str(target_lexical / ".agents" / "skills" / SKILL_NAME),
            "overwrite": False,
            "delete": False,
        }
        try:
            existing = os.stat(
                f".agents/skills/{SKILL_NAME}",
                dir_fd=target_fd,
                follow_symlinks=False,
            )
        except (FileNotFoundError, NotADirectoryError):
            existing = None
        if existing is not None:
            raise InstallError("destination already exists; refusing to overwrite")
        if not confirmed:
            return {**result, "status": "approval_required"}

        agents_fd, agents_identity = _open_or_create_directory_at(
            target_fd, ".agents", "target .agents"
        )
        skills_fd, skills_identity = _open_or_create_directory_at(
            agents_fd, "skills", "target .agents/skills"
        )
        if _test_hook is not None:
            _test_hook("target-parents-opened")
        _assert_target_chain(target_fd, agents_fd, agents_identity, skills_identity)
        try:
            os.stat(SKILL_NAME, dir_fd=skills_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise InstallError("destination already exists; refusing to overwrite")

        for _ in range(16):
            candidate = f".{SKILL_NAME}.install-{secrets.token_hex(12)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=agents_fd)
            except FileExistsError:
                continue
            staging_name = candidate
            break
        if staging_name is None:
            raise InstallError("could not allocate a unique installation quarantine")
        if _test_hook is not None:
            _test_hook("staging-root-created")
        staging_fd = os.open(staging_name, _DIRECTORY_FLAGS, dir_fd=agents_fd)
        staging_identity = _identity(os.fstat(staging_fd))
        staging_current = os.stat(
            staging_name,
            dir_fd=agents_fd,
            follow_symlinks=False,
        )
        if _identity(staging_current) != staging_identity:
            raise InstallError("installation staging directory changed before copy")
        _copy_directory_tree(source_fd, staging_fd, "", owned, _test_hook)
        _verify_owned_contents(staging_fd, "", owned)
        skill_identity = owned.get("SKILL.md")
        if skill_identity is None:
            raise InstallError("staged package is missing operation-owned SKILL.md")
        _validate_staged_skill(staging_fd, skill_identity)
        os.fsync(staging_fd)
        if _test_hook is not None:
            _test_hook("staging-complete")
        _verify_owned_contents(staging_fd, "", owned)
        _assert_target_chain(target_fd, agents_fd, agents_identity, skills_identity)
        _assert_directory_at(
            agents_fd,
            staging_name,
            staging_identity,
            "installation staging directory",
        )
        _rename_noreplace_at(
            agents_fd,
            staging_name,
            skills_fd,
            SKILL_NAME,
        )
        if (
            _installation_state(
                target_fd,
                agents_fd,
                agents_identity,
                skills_fd,
                skills_identity,
                staging_name,
                staging_identity,
            )
            != "installed"
        ):
            raise InstallError("published skill is not visible through the target path")
        observed_commit = True
        if _test_hook is not None:
            _test_hook("published")
        if _test_hook is not None:
            _test_hook("before-publish-fsync")
        os.fsync(agents_fd)
        os.fsync(skills_fd)
        if (
            _installation_state(
                target_fd,
                agents_fd,
                agents_identity,
                skills_fd,
                skills_identity,
                staging_name,
                staging_identity,
            )
            != "installed"
        ):
            raise InstallError("published skill changed after durability sync")
        return {**result, "status": "installed"}
    except BaseException as install_error:
        if (
            staging_name is not None
            and staging_identity is not None
            and agents_fd is not None
            and agents_identity is not None
            and skills_fd is not None
            and skills_identity is not None
        ):
            actual_state = _installation_state(
                target_fd,
                agents_fd,
                agents_identity,
                skills_fd,
                skills_identity,
                staging_name,
                staging_identity,
            )
            if actual_state == "installed":
                return _installed_warning(result)
            if actual_state == "quarantined":
                _raise_quarantined(
                    install_error,
                    staging_name,
                    after_publish=observed_commit,
                )
            if not observed_commit:
                raise InstallError(
                    "installation state is unknown; no content was deleted; inspect "
                    "the target .agents and .agents/skills entries manually"
                ) from install_error
            try:
                _quarantine_published_skill(
                    agents_fd,
                    skills_fd,
                    staging_name,
                    staging_identity,
                )
            except BaseException as reconcile_error:
                raise InstallError(
                    "installation publish state is unknown after reconcile: "
                    f"{reconcile_error}; no content was deleted"
                ) from reconcile_error
            _raise_quarantined(
                install_error,
                staging_name,
                after_publish=True,
            )
        raise
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        if skills_fd is not None:
            os.close(skills_fd)
        if agents_fd is not None:
            os.close(agents_fd)
        os.close(target_fd)
        os.close(source_fd)


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
