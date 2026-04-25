from __future__ import annotations

import os
import sys
from pathlib import Path

from ralph_core.storage import (
    progress_path,
    state_path,
    symlink_component_error,
    workspace_root_error,
    read_state,
    validate_progress_file,
)

from .hook_registry import (
    STOP_HOOK_TIMEOUT_SECONDS,
    build_stop_command,
    hook_registry_value_or_error,
    inspect_stop_hook_registration,
    read_hook_registry,
)
from .installer import directories_match
from .package_manifest import RUNTIME_PACKAGE_DIRS, SKILL_NAMES, STOP_HOOK_FILES
from .toml_feature_flag import codex_hooks_enabled

REPO_ROOT = Path(__file__).resolve().parents[1]


def add_check(
    checks: list[tuple[str, str, str]],
    status: str,
    name: str,
    message: str,
) -> None:
    checks.append((status, name, message))


def validate_installed_hook_files(codex_home: Path, hooks_dir: Path) -> list[str]:
    errors: list[str] = []
    hooks_dir_error = symlink_component_error(codex_home, Path('hooks') / 'ralph')
    if hooks_dir_error is not None:
        return [f'hook directory {hooks_dir_error}']
    for name in STOP_HOOK_FILES:
        installed_path = hooks_dir / name
        expected_path = REPO_ROOT / 'hooks' / name
        if installed_path.is_symlink():
            errors.append(f'{name}: installed hook must be a regular file, not a symlink')
            continue
        if not installed_path.exists():
            errors.append(f'missing {name}')
            continue
        if not installed_path.is_file():
            errors.append(f'{name}: expected a file')
            continue
        try:
            installed_bytes = installed_path.read_bytes()
            expected_bytes = expected_path.read_bytes()
        except OSError as exc:
            errors.append(f'{name}: unreadable ({exc})')
            continue
        if installed_bytes != expected_bytes:
            errors.append(f'{name}: content does not match packaged hook')
    for package_name in RUNTIME_PACKAGE_DIRS:
        installed_path = hooks_dir / package_name
        expected_path = REPO_ROOT / package_name
        if not directories_match(expected_path, installed_path):
            errors.append(f'{package_name}: runtime package does not match packaged source')
    return errors


def is_directory_writeable(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


def check_workspace_writeable(workspace_root: Path, ralph_root: Path) -> list[str]:
    symlink_error = symlink_component_error(workspace_root, ralph_root.relative_to(workspace_root))
    if symlink_error is not None:
        return [symlink_error]

    current = ralph_root
    while not current.exists():
        parent = current.parent
        if parent == current:
            return [f'unable to find an existing parent directory for {ralph_root}']
        current = parent

    if not current.is_dir():
        return [f'path component is not a directory: {current}']

    if current == ralph_root:
        if is_directory_writeable(ralph_root):
            return []
        return [f'directory is not writable: {ralph_root}']

    if is_directory_writeable(current):
        return []
    return [f'cannot create {ralph_root} because parent directory is not writable: {current}']


def validate_workspace_root(workspace_root: Path) -> list[str]:
    error = workspace_root_error(workspace_root)
    return [error] if error is not None else []


def normalize_input_path(path: str | Path) -> Path:
    # Normalize CLI/env paths so quoted "~" and relative inputs are handled consistently.
    return Path(path).expanduser().resolve(strict=False)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 3:
        print('usage: doctor.py [workspace-root] [codex-home] [agents-home]', file=sys.stderr)
        return 2

    # Trade-off: normalize only explicit workspace arguments. This fixes quoted "~"
    # and relative CLI inputs without changing the legacy no-arg PWD behavior.
    workspace_root = (
        normalize_input_path(args[0])
        if len(args) > 0
        else Path(os.environ.get('PWD') or os.getcwd())
    )
    codex_home_value = args[1] if len(args) > 1 else os.environ.get('CODEX_HOME') or (Path.home() / '.codex')
    agents_home_value = args[2] if len(args) > 2 else os.environ.get('AGENTS_HOME') or (Path.home() / '.agents')
    codex_home = normalize_input_path(codex_home_value)
    agents_home = normalize_input_path(agents_home_value)

    checks: list[tuple[str, str, str]] = []

    skills_dir = agents_home / 'skills'
    missing_skills = []
    wrong_skills = []
    for skill_name in SKILL_NAMES:
        skill_path = skills_dir / skill_name
        expected_target = (REPO_ROOT / 'skills' / skill_name).resolve()
        if not skill_path.exists():
            missing_skills.append(skill_name)
            continue
        if not skill_path.is_symlink():
            wrong_skills.append(f'{skill_name} is not a symlink')
            continue
        resolved = skill_path.resolve()
        if resolved != expected_target:
            wrong_skills.append(f'{skill_name} -> {resolved}')
    if missing_skills:
        add_check(checks, 'FAIL', 'Skills', f'missing skill links: {", ".join(missing_skills)}')
    elif wrong_skills:
        add_check(checks, 'FAIL', 'Skills', f'unexpected skill targets: {", ".join(wrong_skills)}')
    else:
        add_check(checks, 'OK', 'Skills', f'all {len(SKILL_NAMES)} Ralph skills are linked')

    hooks_dir = codex_home / 'hooks' / 'ralph'
    hook_errors = validate_installed_hook_files(codex_home, hooks_dir)
    if hook_errors:
        add_check(checks, 'FAIL', 'Hooks', '; '.join(hook_errors))
    else:
        add_check(checks, 'OK', 'Hooks', f'installed hooks present in {hooks_dir}')

    hooks_json = codex_home / 'hooks.json'
    stop_command = build_stop_command(hooks_dir / 'stop_continue.py')
    registry_result = read_hook_registry(hooks_json)
    if registry_result.status == 'missing':
        add_check(checks, 'FAIL', 'Hook Registry', f'missing {hooks_json}')
    elif registry_result.status == 'invalid_json':
        add_check(checks, 'FAIL', 'Hook Registry', registry_result.errors[0])
    elif registry_result.status == 'read_error':
        add_check(checks, 'FAIL', 'Hook Registry', '; '.join(registry_result.errors))
    elif registry_result.status == 'invalid_schema':
        add_check(checks, 'FAIL', 'Hook Registry', '; '.join(registry_result.errors))
    else:
        try:
            registry = hook_registry_value_or_error(registry_result, hooks_json)
        except ValueError as exc:
            add_check(checks, 'FAIL', 'Hook Registry', str(exc))
        else:
            inspection = inspect_stop_hook_registration(registry, stop_command)
            if inspection.equivalent_count == 0:
                add_check(checks, 'FAIL', 'Hook Registry', 'Ralph Stop hook is not registered')
            elif inspection.equivalent_count > 1:
                add_check(
                    checks,
                    'FAIL',
                    'Hook Registry',
                    (
                        'found '
                        f'{inspection.equivalent_count} equivalent Ralph Stop hook registrations; '
                        'expected exactly 1 shell-safe command'
                    ),
                )
            elif inspection.shell_safe_count == 1 and inspection.bounded_timeout_count == 1:
                add_check(checks, 'OK', 'Hook Registry', f'Stop hook registered as "{stop_command}"')
            elif inspection.shell_safe_count == 1:
                add_check(
                    checks,
                    'FAIL',
                    'Hook Registry',
                    f'Ralph Stop hook exists but must set timeout = {STOP_HOOK_TIMEOUT_SECONDS}',
                )
            else:
                add_check(
                    checks,
                    'FAIL',
                    'Hook Registry',
                    'Ralph Stop hook exists but the command is malformed and will not survive shell parsing',
                )

    config_toml = codex_home / 'config.toml'
    try:
        hooks_enabled = codex_hooks_enabled(config_toml)
    except (OSError, UnicodeDecodeError) as exc:
        add_check(checks, 'FAIL', 'Config', f'unable to read {config_toml}: {exc}')
    except ValueError as exc:
        add_check(checks, 'FAIL', 'Config', str(exc))
    else:
        if hooks_enabled:
            add_check(checks, 'OK', 'Config', 'codex_hooks = true is enabled')
        else:
            add_check(checks, 'FAIL', 'Config', f'codex_hooks = true is missing from {config_toml}')

    workspace_root_errors = validate_workspace_root(workspace_root)
    if workspace_root_errors:
        add_check(checks, 'FAIL', 'Workspace', workspace_root_errors[0])
    else:
        ralph_root = workspace_root / '.codex' / 'ralph'
        workspace_errors = check_workspace_writeable(workspace_root, ralph_root)
        if workspace_errors:
            add_check(checks, 'FAIL', 'Workspace', f'workspace Ralph dir is not writable: {workspace_errors[0]}')
        else:
            if ralph_root.exists():
                message = f'workspace Ralph dir is writable at {ralph_root}'
            else:
                message = f'workspace Ralph dir can be created at {ralph_root} without mutating the workspace'
            add_check(checks, 'OK', 'Workspace', message)

        state_file = state_path(str(workspace_root))
        state_result = read_state(str(workspace_root))
        if state_result.status == 'missing':
            add_check(checks, 'OK', 'State', 'no active state file present')
        elif state_result.status == 'invalid_json':
            add_check(checks, 'FAIL', 'State', state_result.errors[0])
        elif state_result.status == 'read_error':
            add_check(checks, 'FAIL', 'State', '; '.join(state_result.errors))
        elif state_result.status == 'invalid_schema':
            add_check(checks, 'FAIL', 'State', '; '.join(state_result.errors))
        else:
            add_check(checks, 'OK', 'State', f'valid state file at {state_file}')

        progress_file = progress_path(str(workspace_root))
        if not progress_file.exists() and not progress_file.is_symlink():
            add_check(checks, 'OK', 'Progress', 'no progress ledger present')
        else:
            errors = validate_progress_file(progress_file, cwd=str(workspace_root))
            if errors:
                add_check(checks, 'FAIL', 'Progress', '; '.join(errors))
            else:
                add_check(checks, 'OK', 'Progress', f'valid progress ledger at {progress_file}')

    fail_count = 0
    for status, name, message in checks:
        print(f'[{status}] {name}: {message}')
        if status == 'FAIL':
            fail_count += 1

    print(f'\nResults: {len(checks) - fail_count} passed, {fail_count} failed')
    return 1 if fail_count else 0


if __name__ == '__main__':
    raise SystemExit(main())
