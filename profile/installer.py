from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ralph_core.storage import fsync_directory, resolve_atomic_write_target, symlink_component_error

from .hook_registry import (
    build_stop_command,
    hook_registry_value_or_error,
    read_hook_registry,
    register_stop_hook,
    stop_hook_registered,
    STOP_HOOK_TIMEOUT_SECONDS,
    unregister_stop_hook,
)
from .package_manifest import RUNTIME_PACKAGE_DIRS, STOP_HOOK_FILE, STOP_HOOK_FILES, SKILL_NAMES

Mode = Literal['all', 'skills-only', 'hooks-only']
SnapshotKind = Literal['missing', 'file', 'directory', 'symlink']


@dataclass(frozen=True)
class Snapshot:
    path: Path
    kind: SnapshotKind
    backup_path: Path | None = None
    symlink_target: str | None = None


@dataclass(frozen=True)
class InstallPaths:
    root_dir: Path
    codex_home: Path
    agents_home: Path

    @property
    def skills_source(self) -> Path:
        return self.root_dir / 'skills'

    @property
    def hooks_source(self) -> Path:
        return self.root_dir / 'hooks'

    def runtime_package_source(self, package_name: str) -> Path:
        return self.root_dir / package_name

    @property
    def user_skills(self) -> Path:
        return self.agents_home / 'skills'

    @property
    def target_hooks(self) -> Path:
        return self.codex_home / 'hooks' / 'ralph'

    @property
    def hooks_json(self) -> Path:
        return self.codex_home / 'hooks.json'


class InstallTransaction:
    def __init__(self) -> None:
        self._snapshots: dict[Path, Snapshot] = {}
        self._snapshot_order: list[Path] = []
        self._created_dirs: list[Path] = []
        self._created_dir_set: set[Path] = set()
        self._backup_root = Path(tempfile.mkdtemp(prefix='ralph-install-'))
        self._committed = False

    def commit(self) -> None:
        self._committed = True

    def close(self) -> None:
        shutil.rmtree(self._backup_root, ignore_errors=True)

    def snapshot_path(self, path: Path) -> None:
        if path in self._snapshots:
            return

        if path.is_symlink():
            snapshot = Snapshot(
                path=path,
                kind='symlink',
                symlink_target=os.readlink(path),
            )
        elif path.exists():
            backup_path = self._backup_root / f'snapshot_{len(self._snapshot_order)}'
            if path.is_dir():
                special_files = directory_tree_special_file_errors(path)
                if special_files:
                    details = ', '.join(special_files)
                    # Trade-off: snapshot rollback intentionally preserves nested symlinks, but
                    # refuses sockets/FIFOs/devices before shutil.copytree can block on them.
                    raise ValueError(
                        f'unsupported special file(s) inside directory snapshot {path}: {details}'
                    )
                shutil.copytree(path, backup_path, symlinks=True)
                snapshot = Snapshot(path=path, kind='directory', backup_path=backup_path)
            elif path.is_file():
                shutil.copy2(path, backup_path)
                snapshot = Snapshot(path=path, kind='file', backup_path=backup_path)
            else:
                # Trade-off: profile paths should contain regular files, directories, or
                # symlinks. Refuse device/socket/FIFO nodes instead of trying to back them up;
                # copying a FIFO can block the installer indefinitely.
                raise ValueError(f'unsupported special file at {path}')
        else:
            snapshot = Snapshot(path=path, kind='missing')

        self._snapshots[path] = snapshot
        self._snapshot_order.append(path)

    def snapshot_atomic_write_path(self, path: Path, *, preserve_leaf_symlink: bool) -> Path:
        self.snapshot_path(path)
        target = resolve_atomic_write_target(path, preserve_leaf_symlink=preserve_leaf_symlink)
        if target != path:
            self.snapshot_path(target)
        # Trade-off: when an atomic write follows a profile symlink into a dotfiles tree, the
        # write helper may need to mkdir the target parent on the far side of that symlink. Track
        # those directories inside the transaction too so a later failure does not leave "rolled
        # back" installs with newly created target-side parent directories behind.
        self.ensure_dir(target.parent)
        return target

    def ensure_dir(self, path: Path) -> None:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent

        path.mkdir(parents=True, exist_ok=True)
        for created_dir in reversed(missing):
            if created_dir in self._created_dir_set:
                continue
            self._created_dir_set.add(created_dir)
            self._created_dirs.append(created_dir)

    def rollback(self) -> None:
        for path in reversed(self._snapshot_order):
            self._restore_snapshot(self._snapshots[path])
        for created_dir in reversed(self._created_dirs):
            try:
                created_dir.rmdir()
            except OSError:
                pass

    def __enter__(self) -> InstallTransaction:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            self.close()
            return False

        if not self._committed:
            try:
                self.rollback()
            finally:
                self.close()
        else:
            self.close()
        return False

    def _restore_snapshot(self, snapshot: Snapshot) -> None:
        if snapshot.kind == 'missing':
            self._remove_path(snapshot.path)
            return

        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.kind == 'file':
            if snapshot.backup_path is None:
                raise RuntimeError(f'internal install snapshot for {snapshot.path} is missing its file backup')
            if snapshot.path.is_file() and files_match(snapshot.backup_path, snapshot.path):
                return
            if snapshot.path.is_symlink() or snapshot.path.is_dir():
                self._remove_path(snapshot.path)
            copy_file_atomic(snapshot.backup_path, snapshot.path)
            return
        if snapshot.kind == 'symlink':
            if snapshot.path.is_symlink() and os.readlink(snapshot.path) == snapshot.symlink_target:
                return
            self._remove_path(snapshot.path)
            if snapshot.symlink_target is None:
                raise RuntimeError(f'internal install snapshot for {snapshot.path} is missing its symlink target')
            os.symlink(snapshot.symlink_target, snapshot.path)
            return

        self._remove_path(snapshot.path)
        if snapshot.backup_path is None:
            raise RuntimeError(f'internal install snapshot for {snapshot.path} is missing its directory backup')
        shutil.copytree(snapshot.backup_path, snapshot.path, symlinks=True)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink():
            path.unlink(missing_ok=True)
            return
        if path.is_dir():
            shutil.rmtree(path)
            return
        if path.exists():
            path.unlink(missing_ok=True)


def normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def fsync_file(path: Path) -> None:
    with path.open('rb') as handle:
        os.fsync(handle.fileno())


def validate_source_file(source: Path) -> None:
    # Trade-off: hook files are executable profile code. Refuse source symlinks and special
    # files just like runtime package trees so install never follows a package-local redirect
    # into unrelated filesystem content.
    if source.is_symlink():
        raise ValueError(f'source file contains unsupported symlink: {source}')
    if not source.exists():
        raise ValueError(f'missing source file: {source}')
    if not source.is_file():
        raise ValueError(f'source path is not a regular file: {source}')


def copy_file_atomic(source: Path, destination: Path) -> None:
    validate_source_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f'.{destination.name}.',
        suffix='.tmp',
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        shutil.copy2(source, tmp_path)
        fsync_file(tmp_path)
        os.replace(tmp_path, destination)
        try:
            fsync_directory(destination.parent)
        except OSError:
            # Trade-off: the file bytes are already flushed; directory fsync is best effort
            # because some filesystems reject it even after a successful atomic replace.
            pass
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def files_match(source: Path, destination: Path) -> bool:
    # Trade-off: treating matching symlink targets as "installed" is cheaper on reinstall, but
    # it lets hook execution escape the managed $CODEX_HOME tree. Require a real file here so
    # reinstall repairs any symlink drift even when the target bytes happen to match.
    if destination.is_symlink():
        return False
    if not destination.is_file():
        return False
    try:
        return filecmp.cmp(source, destination, shallow=False)
    except OSError:
        return False


def iter_directory_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise OSError(f'unexpected symlink in directory tree: {root}')

    files: list[Path] = []
    for path in root.rglob('*'):
        # Ignore normal bytecode-cache churn, but reject symlinks before that filter so
        # an ignored cache path cannot hide a runtime package redirect.
        if path.is_symlink():
            raise OSError(f'unexpected symlink in directory tree: {path}')
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        if path.is_dir():
            continue
        if path.is_file():
            files.append(path.relative_to(root))
            continue
        raise OSError(f'unexpected special file in directory tree: {path}')
    return sorted(files)


def directory_tree_symlink_errors(root: Path) -> list[str]:
    errors: list[str] = []
    if root.is_symlink():
        return ['.']
    for path in root.rglob('*'):
        if path.is_symlink():
            errors.append(str(path.relative_to(root)))
            continue
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        if path.is_dir() or path.is_file():
            continue
        errors.append(str(path.relative_to(root)))
    return errors


def directory_tree_special_file_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for path in root.rglob('*'):
        if path.is_symlink():
            continue
        if path.is_dir() or path.is_file():
            continue
        errors.append(str(path.relative_to(root)))
    return errors


def directories_match(source: Path, destination: Path) -> bool:
    if destination.is_symlink() or not destination.is_dir():
        return False
    try:
        source_files = iter_directory_files(source)
        destination_files = iter_directory_files(destination)
    except OSError:
        return False
    if source_files != destination_files:
        return False
    return all(files_match(source / relative, destination / relative) for relative in source_files)


def copy_directory(source: Path, destination: Path) -> None:
    source_errors = directory_tree_symlink_errors(source)
    if source_errors:
        details = ', '.join(source_errors)
        raise ValueError(f'source directory contains unsupported symlink or special file(s): {details}')

    if destination.exists() or destination.is_symlink():
        InstallTransaction._remove_path(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
    )
    try:
        fsync_directory(destination.parent)
    except OSError:
        # Trade-off: directory copies do not have a single atomic replace boundary, so this
        # best-effort parent fsync mostly protects the final directory entry metadata. If the
        # platform rejects directory fsync, keep install behavior aligned with file writes and
        # let byte-for-byte verification catch bad copies instead of failing an otherwise valid
        # install on filesystems that do not support it.
        pass


def validate_mode(mode: str) -> None:
    if mode not in {'all', 'skills-only', 'hooks-only'}:
        raise ValueError(f'mode must be one of all, skills-only, hooks-only: {mode!r}')


def validate_managed_hook_directory(paths: InstallPaths) -> None:
    hooks_dir_error = symlink_component_error(paths.codex_home, Path('hooks') / 'ralph')
    if hooks_dir_error is not None:
        raise ValueError(f'hook directory {hooks_dir_error}')


def validate_stop_hook_registration(hooks_json: Path, stop_command: str) -> None:
    result = read_hook_registry(hooks_json)
    if result.status != 'ok':
        details = '; '.join(result.errors) if result.errors else f'failed to read {hooks_json}'
        raise ValueError(details)

    registry = hook_registry_value_or_error(result, hooks_json)
    if not stop_hook_registered(
        registry,
        stop_command,
        require_shell_safe=True,
        require_bounded_timeout=True,
    ):
        raise ValueError(f'failed to register Stop hook with timeout={STOP_HOOK_TIMEOUT_SECONDS}')


def install_profile(
    *,
    root_dir: str | Path,
    codex_home: str | Path,
    agents_home: str | Path,
    mode: Mode = 'all',
) -> list[str]:
    validate_mode(mode)
    # Trade-off: install/uninstall aim for transactional single-caller behavior, not cross-process
    # coordination. Ralph's runtime loop state needs concurrency control; profile bootstrap does not.
    # Keep this path simpler and document that users should not run multiple install/uninstall
    # commands in parallel against the same CODEX_HOME / AGENTS_HOME pair.
    paths = InstallPaths(
        root_dir=normalize_path(root_dir),
        codex_home=normalize_path(codex_home),
        agents_home=normalize_path(agents_home),
    )
    changes: list[str] = []
    with InstallTransaction() as transaction:
        if mode in {'all', 'skills-only'}:
            install_skills(paths, transaction, changes)
        if mode in {'all', 'hooks-only'}:
            install_hooks(paths, transaction, changes)
        transaction.commit()
    return changes


def uninstall_profile(
    *,
    root_dir: str | Path,
    codex_home: str | Path,
    agents_home: str | Path,
    mode: Mode = 'all',
) -> list[str]:
    validate_mode(mode)
    paths = InstallPaths(
        root_dir=normalize_path(root_dir),
        codex_home=normalize_path(codex_home),
        agents_home=normalize_path(agents_home),
    )
    changes: list[str] = []
    with InstallTransaction() as transaction:
        if mode in {'all', 'skills-only'}:
            uninstall_skills(paths, transaction, changes)
        if mode in {'all', 'hooks-only'}:
            uninstall_hooks(paths, transaction, changes)
        transaction.commit()
    return changes


def skill_link_points_to_source(target: Path, source: Path) -> bool:
    return normalize_path(target) == normalize_path(source)


def install_skills(paths: InstallPaths, transaction: InstallTransaction, changes: list[str]) -> None:
    for skill_name in SKILL_NAMES:
        source = paths.skills_source / skill_name
        if not source.is_dir():
            raise ValueError(f'missing skill source: {source}')

    transaction.ensure_dir(paths.user_skills)
    for skill_name in SKILL_NAMES:
        source = paths.skills_source / skill_name
        target = paths.user_skills / skill_name
        if target.is_symlink():
            if normalize_path(target) == normalize_path(source):
                continue
            # Trade-off: automatically repair only broken symlinks so upgrades recover
            # from stale paths, but still fail closed on live links to some other
            # package to avoid silently stealing an existing skill name.
            if not target.exists():
                transaction.snapshot_path(target)
                target.unlink()
                os.symlink(source, target)
                changes.append(f'relinked broken skill {skill_name} -> {target}')
                continue
            raise ValueError(f'skill target already exists and is not the expected symlink: {target}')
        if target.exists():
            raise ValueError(f'skill target already exists and is not the expected symlink: {target}')

        transaction.snapshot_path(target)
        os.symlink(source, target)
        changes.append(f'linked skill {skill_name} -> {target}')


def uninstall_skills(paths: InstallPaths, transaction: InstallTransaction, changes: list[str]) -> None:
    for skill_name in SKILL_NAMES:
        source = paths.skills_source / skill_name
        target = paths.user_skills / skill_name
        if not target.is_symlink():
            continue
        if not skill_link_points_to_source(target, source):
            changes.append(f'left skill link unchanged (unexpected target {target})')
            continue

        transaction.snapshot_path(target)
        target.unlink()
        changes.append(f'removed skill link {target}')


def install_hooks(paths: InstallPaths, transaction: InstallTransaction, changes: list[str]) -> None:
    validate_managed_hook_directory(paths)
    transaction.ensure_dir(paths.target_hooks)
    for hook_name in STOP_HOOK_FILES:
        source = paths.hooks_source / hook_name
        target = paths.target_hooks / hook_name
        if target.exists() and target.is_dir() and not target.is_symlink():
            raise ValueError(f'hook target already exists and is not a file: {target}')
        if files_match(source, target):
            continue

        transaction.snapshot_path(target)
        copy_file_atomic(source, target)
        changes.append(f'copied hook {hook_name}')

    for package_name in RUNTIME_PACKAGE_DIRS:
        source = paths.runtime_package_source(package_name)
        target = paths.target_hooks / package_name
        if not source.is_dir():
            raise ValueError(f'missing runtime package source: {source}')
        if directories_match(source, target):
            continue
        transaction.snapshot_path(target)
        copy_directory(source, target)
        changes.append(f'copied runtime package {package_name}')

    transaction.ensure_dir(paths.hooks_json.parent)
    transaction.snapshot_atomic_write_path(paths.hooks_json, preserve_leaf_symlink=True)
    stop_hook_script = paths.target_hooks / STOP_HOOK_FILE
    stop_command = build_stop_command(stop_hook_script)
    register_status = register_stop_hook(paths.hooks_json, stop_command)
    validate_stop_hook_registration(paths.hooks_json, stop_command)

    if register_status == 'added':
        changes.append('registered Stop hook')
    elif register_status == 'updated':
        changes.append('repaired Stop hook registration')


def uninstall_hooks(paths: InstallPaths, transaction: InstallTransaction, changes: list[str]) -> None:
    hook_directory_error: str | None = None
    try:
        validate_managed_hook_directory(paths)
    except ValueError as exc:
        hook_directory_error = str(exc)

    stop_hook_script = paths.target_hooks / STOP_HOOK_FILE
    stop_command = build_stop_command(stop_hook_script)
    hooks_json_error: str | None = None

    if paths.hooks_json.exists() or paths.hooks_json.is_symlink():
        registry_result = read_hook_registry(paths.hooks_json)
        if registry_result.status == 'ok':
            transaction.ensure_dir(paths.hooks_json.parent)
            transaction.snapshot_atomic_write_path(paths.hooks_json, preserve_leaf_symlink=True)
            unregister_status = unregister_stop_hook(paths.hooks_json, stop_command)
            if unregister_status == 'removed':
                changes.append('removed Stop hook registration')
        elif registry_result.status == 'read_error':
            details = '; '.join(registry_result.errors) if registry_result.errors else f'failed to read {paths.hooks_json}'
            # Trade-off: a readable-but-invalid registry can be left untouched while Ralph removes
            # its local hook files, because we can already prove the file is broken and cannot
            # trust any targeted rewrite. An unreadable registry is different: Ralph cannot verify
            # whether a live Stop-hook registration still points at this script, so abort before
            # deleting files and let the caller repair access first.
            raise ValueError(
                'unable to verify Ralph Stop hook registration before removing hook files: '
                f'{details}'
            )
        elif registry_result.status != 'missing':
            details = '; '.join(registry_result.errors) if registry_result.errors else f'failed to read {paths.hooks_json}'
            # Trade-off: if hooks.json is readable but already invalid, uninstall should still
            # remove Ralph's local hook files instead of rewriting a malformed profile-wide
            # registry. Doctor will keep surfacing the broken file, but uninstall still succeeds
            # in removing Ralph-managed artifacts from disk.
            hooks_json_error = details

    if hook_directory_error is not None:
        # Trade-off: even with an unsafe hook directory shape, a valid profile registry can still
        # be cleaned so Codex stops executing Ralph's global Stop hook. Do not follow the symlinked
        # hook directory to delete files; leave that path for the user to inspect explicitly.
        changes.append(f'left hook files unchanged ({hook_directory_error})')
        if hooks_json_error is not None:
            changes.append(f'left hooks.json unchanged ({hooks_json_error})')
        return

    for hook_name in STOP_HOOK_FILES:
        target = paths.target_hooks / hook_name
        if not (target.is_file() or target.is_symlink()):
            continue
        transaction.snapshot_path(target)
        target.unlink()
        changes.append(f'removed hook file {target}')

    for package_name in RUNTIME_PACKAGE_DIRS:
        target = paths.target_hooks / package_name
        if not (target.exists() or target.is_symlink()):
            continue
        transaction.snapshot_path(target)
        InstallTransaction._remove_path(target)
        changes.append(f'removed runtime package {target}')

    if hooks_json_error is not None:
        changes.append(f'left hooks.json unchanged ({hooks_json_error})')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='profile_installer.py')
    subparsers = parser.add_subparsers(dest='command', required=True)

    install_parser = subparsers.add_parser('install')
    install_parser.add_argument('--root-dir', required=True)
    install_parser.add_argument('--codex-home', required=True)
    install_parser.add_argument('--agents-home', required=True)
    install_parser.add_argument(
        '--mode',
        choices=('all', 'skills-only', 'hooks-only'),
        default='all',
    )

    uninstall_parser = subparsers.add_parser('uninstall')
    uninstall_parser.add_argument('--root-dir', required=True)
    uninstall_parser.add_argument('--codex-home', required=True)
    uninstall_parser.add_argument('--agents-home', required=True)
    uninstall_parser.add_argument(
        '--mode',
        choices=('all', 'skills-only', 'hooks-only'),
        default='all',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == 'install':
            changes = install_profile(
                root_dir=args.root_dir,
                codex_home=args.codex_home,
                agents_home=args.agents_home,
                mode=args.mode,
            )
        else:
            changes = uninstall_profile(
                root_dir=args.root_dir,
                codex_home=args.codex_home,
                agents_home=args.agents_home,
                mode=args.mode,
            )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not changes:
        if args.command == 'install':
            print('Codex Ralph is already installed.')
        else:
            print('Nothing to uninstall.')
        return 0

    if args.command == 'install':
        print('Installed Codex Ralph:')
    else:
        print('Uninstalled Codex Ralph:')
    for change in changes:
        print(f'- {change}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
