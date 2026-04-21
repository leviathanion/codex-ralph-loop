from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from common import resolve_atomic_write_target

class HookDefinition(TypedDict, total=False):
    type: str
    command: str


class HookEntry(TypedDict, total=False):
    hooks: list[HookDefinition]


class HookRegistry(TypedDict, total=False):
    hooks: dict[str, list[HookEntry]]


ReadStatus = Literal['missing', 'invalid_json', 'invalid_schema', 'read_error', 'ok']


@dataclass(frozen=True)
class HookRegistryReadResult:
    status: ReadStatus
    value: HookRegistry | None = None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedStopCommand:
    script_path: Path
    shell_safe: bool


@dataclass(frozen=True)
class StopHookMatch:
    entry_index: int
    hook_index: int
    command: str
    shell_safe: bool


@dataclass(frozen=True)
class StopHookInspection:
    matches: tuple[StopHookMatch, ...]

    @property
    def equivalent_count(self) -> int:
        return len(self.matches)

    @property
    def shell_safe_count(self) -> int:
        return sum(1 for match in self.matches if match.shell_safe)


def empty_hook_registry() -> HookRegistry:
    return {'hooks': {}}


def validate_hook_registry_payload(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ['hook registry must be a JSON object']

    hooks = data.get('hooks')
    if not isinstance(hooks, dict):
        return ['hooks must be a JSON object']

    errors: list[str] = []
    for event_name, entries in hooks.items():
        if not isinstance(event_name, str):
            errors.append('hooks keys must be strings')
            continue
        if not isinstance(entries, list):
            errors.append(f'hooks.{event_name} must be a list')
            continue
        for entry_index, entry in enumerate(entries):
            entry_path = f'hooks.{event_name}[{entry_index}]'
            if not isinstance(entry, dict):
                errors.append(f'{entry_path} must be an object')
                continue
            entry_hooks = entry.get('hooks')
            if not isinstance(entry_hooks, list):
                errors.append(f'{entry_path}.hooks must be a list')
                continue
            for hook_index, hook in enumerate(entry_hooks):
                hook_path = f'{entry_path}.hooks[{hook_index}]'
                if not isinstance(hook, dict):
                    errors.append(f'{hook_path} must be an object')
                    continue
                hook_type = hook.get('type')
                if not isinstance(hook_type, str) or not hook_type:
                    errors.append(f'{hook_path}.type must be a non-empty string')
                if hook_type == 'command':
                    command = hook.get('command')
                    if not isinstance(command, str) or not command:
                        errors.append(f'{hook_path}.command must be a non-empty string')
    return errors


def normalize_hook_registry_payload(data: Any) -> Any:
    if not isinstance(data, dict) or 'hooks' in data:
        return data

    # Trade-off: treat a top-level object without "hooks" as an empty registry so
    # install can repair or populate a parseable hooks.json instead of failing after
    # partially mutating the profile. Structural errors under "hooks" still fail.
    normalized = dict(data)
    normalized['hooks'] = {}
    return normalized


def read_hook_registry(path: Path) -> HookRegistryReadResult:
    if not path.exists():
        return HookRegistryReadResult(status='missing')

    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        return HookRegistryReadResult(
            status='invalid_json',
            errors=(f'invalid JSON: {exc.msg}',),
        )
    except (OSError, UnicodeDecodeError) as exc:
        return HookRegistryReadResult(
            status='read_error',
            errors=(f'unable to read {path}: {exc}',),
        )

    payload = normalize_hook_registry_payload(payload)
    errors = validate_hook_registry_payload(payload)
    if errors:
        return HookRegistryReadResult(status='invalid_schema', errors=tuple(errors))

    return HookRegistryReadResult(status='ok', value=cast(HookRegistry, payload))


def write_hook_registry(path: Path, registry: HookRegistry) -> None:
    errors = validate_hook_registry_payload(registry)
    if errors:
        raise ValueError('; '.join(errors))

    try:
        target = resolve_atomic_write_target(path, preserve_leaf_symlink=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(registry, indent=2, ensure_ascii=True) + '\n'
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f'.{target.name}.',
            suffix='.tmp',
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
    except OSError as exc:
        raise ValueError(f'unable to write {path}: {exc}') from exc


def build_stop_command(script_path: str | Path) -> str:
    resolved_path = Path(script_path).expanduser().resolve(strict=False)
    return shlex.join(['python3', str(resolved_path)])


def parse_stop_command(stop_command: str) -> ParsedStopCommand | None:
    trimmed = stop_command.strip()
    if not trimmed:
        return None

    try:
        parts = shlex.split(trimmed)
    except ValueError:
        parts = None

    if parts is not None and len(parts) == 2 and parts[0] == 'python3':
        return ParsedStopCommand(
            script_path=Path(parts[1]).expanduser().resolve(strict=False),
            shell_safe=True,
        )

    legacy_match = re.fullmatch(r'python3\s+(.+)', trimmed)
    if legacy_match is None:
        return None

    # Trade-off: keep recognizing Ralph's legacy unquoted "python3 <path>" form so
    # reinstall and uninstall can repair/remove a bad registration created by older
    # versions, but mark it non-shell-safe so doctor still fails until it is fixed.
    return ParsedStopCommand(
        script_path=Path(legacy_match.group(1)).expanduser().resolve(strict=False),
        shell_safe=False,
    )


def canonicalize_stop_command(stop_command: str) -> str:
    parsed = parse_stop_command(stop_command)
    if parsed is None:
        return stop_command.strip()
    return build_stop_command(parsed.script_path)


def stop_commands_match(
    actual_command: str,
    expected_command: str,
    *,
    require_shell_safe: bool = False,
) -> bool:
    actual = parse_stop_command(actual_command)
    expected = parse_stop_command(expected_command)
    if actual is not None and expected is not None and actual.script_path == expected.script_path:
        return (not require_shell_safe) or actual.shell_safe
    return canonicalize_stop_command(actual_command) == canonicalize_stop_command(expected_command)


def inspect_stop_hook_registration(
    registry: HookRegistry,
    stop_command: str,
) -> StopHookInspection:
    matches: list[StopHookMatch] = []
    for entry_index, entry in enumerate(registry.get('hooks', {}).get('Stop', [])):
        for hook_index, hook in enumerate(entry.get('hooks', [])):
            command = hook.get('command')
            if hook.get('type') != 'command' or not isinstance(command, str):
                continue
            if not stop_commands_match(command, stop_command):
                continue
            parsed = parse_stop_command(command)
            matches.append(StopHookMatch(
                entry_index=entry_index,
                hook_index=hook_index,
                command=command,
                shell_safe=parsed.shell_safe if parsed is not None else False,
            ))
    return StopHookInspection(matches=tuple(matches))


def stop_hook_registered(
    registry: HookRegistry,
    stop_command: str,
    *,
    require_shell_safe: bool = False,
) -> bool:
    inspection = inspect_stop_hook_registration(registry, stop_command)
    if require_shell_safe:
        return inspection.shell_safe_count > 0
    return inspection.equivalent_count > 0


def _preferred_stop_hook_match(inspection: StopHookInspection, stop_command: str) -> StopHookMatch:
    for match in inspection.matches:
        if match.command == stop_command:
            return match
    for match in inspection.matches:
        if match.shell_safe:
            return match
    return inspection.matches[0]


def register_stop_hook(path: Path, stop_command: str) -> str:
    stop_command = canonicalize_stop_command(stop_command)
    result = read_hook_registry(path)
    if result.status == 'missing':
        registry = empty_hook_registry()
    elif result.status == 'ok':
        registry = result.value
        assert registry is not None
    else:
        raise ValueError('; '.join(result.errors))

    assert registry is not None
    inspection = inspect_stop_hook_registration(registry, stop_command)
    if inspection.equivalent_count == 1 and inspection.shell_safe_count == 1:
        return 'unchanged'

    if inspection.equivalent_count:
        preferred = _preferred_stop_hook_match(inspection, stop_command)
        filtered_entries: list[HookEntry] = []
        for entry_index, entry in enumerate(registry.get('hooks', {}).get('Stop', [])):
            entry_hooks = entry.get('hooks', [])
            remaining_hooks: list[HookDefinition] = []
            entry_changed = False
            for hook_index, hook in enumerate(entry_hooks):
                command = hook.get('command')
                if not (
                    hook.get('type') == 'command'
                    and isinstance(command, str)
                    and stop_commands_match(command, stop_command)
                ):
                    remaining_hooks.append(hook)
                    continue

                if entry_index == preferred.entry_index and hook_index == preferred.hook_index:
                    if preferred.shell_safe:
                        remaining_hooks.append(hook)
                    else:
                        updated_hook = dict(hook)
                        updated_hook['command'] = stop_command
                        remaining_hooks.append(cast(HookDefinition, updated_hook))
                        entry_changed = True
                    continue

                entry_changed = True

            if remaining_hooks:
                if entry_changed or len(remaining_hooks) != len(entry_hooks):
                    updated_entry = dict(entry)
                    updated_entry['hooks'] = remaining_hooks
                    filtered_entries.append(cast(HookEntry, updated_entry))
                else:
                    filtered_entries.append(entry)

        registry.setdefault('hooks', {})['Stop'] = filtered_entries
        write_hook_registry(path, registry)
        return 'updated'

    stop_entries = registry.setdefault('hooks', {}).setdefault('Stop', [])
    stop_entries.append({
        'hooks': [
            {
                'type': 'command',
                'command': stop_command,
            },
        ],
    })
    write_hook_registry(path, registry)
    return 'added'


def unregister_stop_hook(path: Path, stop_command: str) -> str:
    stop_command = canonicalize_stop_command(stop_command)
    result = read_hook_registry(path)
    if result.status == 'missing':
        return 'unchanged'
    if result.status != 'ok':
        raise ValueError('; '.join(result.errors))

    registry = result.value
    assert registry is not None
    if not stop_hook_registered(registry, stop_command):
        return 'unchanged'

    registry_hooks = registry.get('hooks', {})
    stop_entries = registry_hooks.get('Stop', [])
    filtered_entries: list[HookEntry] = []

    for entry in stop_entries:
        entry_hooks = entry.get('hooks', [])
        remaining_hooks = [
            hook
            for hook in entry_hooks
            if not (
                hook.get('type') == 'command'
                and isinstance(command := hook.get('command'), str)
                and stop_commands_match(command, stop_command)
            )
        ]
        if remaining_hooks:
            updated_entry = dict(entry)
            updated_entry['hooks'] = remaining_hooks
            filtered_entries.append(cast(HookEntry, updated_entry))

    if filtered_entries:
        registry_hooks['Stop'] = filtered_entries
    else:
        registry_hooks.pop('Stop', None)

    write_hook_registry(path, registry)
    return 'removed'


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 3 or args[0] not in {'contains', 'register', 'unregister'}:
        print('usage: hook_registry.py contains|register|unregister <hooks.json> <stop-command>', file=sys.stderr)
        return 2

    command, hooks_json, stop_command = args
    path = Path(hooks_json)
    try:
        if command == 'contains':
            result = read_hook_registry(path)
            if result.status == 'missing':
                print('false')
            elif result.status == 'ok':
                registry = result.value
                assert registry is not None
                print('true' if stop_hook_registered(registry, stop_command, require_shell_safe=True) else 'false')
            else:
                raise ValueError('; '.join(result.errors))
        elif command == 'register':
            print(register_stop_hook(path, stop_command))
        else:
            print(unregister_stop_hook(path, stop_command))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
