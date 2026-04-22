from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from common import (
    ALLOWED_PHASES,
    DEFAULT_COMPLETION_TOKEN,
    DEFAULT_MAX_ITERATIONS,
    LOCK_RELATIVE_PATH,
    LEDGER_PROGRESS_STATUSES,
    PROGRESS_RELATIVE_PATH,
    STATE_RELATIVE_PATH,
    progress_path,
    state_path,
    symlink_component_error,
    symlink_parent_error,
    workspace_path,
    workspace_root_error,
)


class LoopState(TypedDict):
    active: bool
    prompt: str
    iteration: int
    max_iterations: int
    completion_token: str
    claimed_session_id: str | None
    phase: str
    started_at: str | None
    updated_at: str | None
    last_message_fingerprint: str | None
    repeat_count: int


class ProgressEntry(TypedDict):
    ts: str
    iteration: int
    session_id: str | None
    status: str
    summary: str
    files: list[str]
    checks: list[str]
    message_fingerprint: str | None
    reason: str | None


class StorageError(RuntimeError):
    pass


ReadStatus = Literal['missing', 'invalid_json', 'invalid_schema', 'read_error', 'ok']


@dataclass(frozen=True)
class StateReadResult:
    status: ReadStatus
    value: LoopState | None = None
    errors: tuple[str, ...] = ()


LEGACY_STATE_DEFAULTS = {
    'phase': 'running',
    'started_at': None,
    'updated_at': None,
    'last_message_fingerprint': None,
    'repeat_count': 0,
}


def _storage_root(cwd: str | None) -> Path:
    return workspace_path(cwd)


def _managed_storage_error(cwd: str | None, relative_path: Path) -> str | None:
    return symlink_component_error(_storage_root(cwd), relative_path)


@contextmanager
def workspace_lock(cwd: str | None = None):
    root = _storage_root(cwd)
    workspace_error = workspace_root_error(root)
    if workspace_error is not None:
        raise StorageError(workspace_error)

    path = root / LOCK_RELATIVE_PATH
    symlink_error = _managed_storage_error(cwd, LOCK_RELATIVE_PATH)
    if symlink_error is not None:
        raise StorageError(f'unable to lock {path}: {symlink_error}')

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise StorageError(f'unable to lock {path}: {exc}') from exc

    try:
        with os.fdopen(fd, 'a+', encoding='utf-8') as handle:
            # Trade-off: one coarse per-workspace lock reduces Ralph's theoretical parallelism,
            # but it keeps state.json and progress.jsonl updates linearizable across stop/start/
            # resume/cancel flows. That is the right failure mode here: one workspace loop should
            # serialize control mutations rather than let concurrent sessions overwrite each other's
            # claims or silently drop ledger rows.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise StorageError(f'unable to lock {path}: {exc}') from exc


def _write_text_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f'.{path.name}.',
        suffix='.tmp',
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def default_state() -> LoopState:
    return {
        'active': False,
        'prompt': '',
        'iteration': 0,
        'max_iterations': DEFAULT_MAX_ITERATIONS,
        'completion_token': DEFAULT_COMPLETION_TOKEN,
        'claimed_session_id': None,
        'phase': 'running',
        'started_at': None,
        'updated_at': None,
        'last_message_fingerprint': None,
        'repeat_count': 0,
    }


def _validate_iso(value: Any, field: str, *, allow_null: bool) -> list[str]:
    if value is None:
        if allow_null:
            return []
        return [f'{field} must be a non-empty ISO8601 string']
    if not isinstance(value, str) or not value:
        suffix = ' or null' if allow_null else ''
        return [f'{field} must be a non-empty ISO8601 string{suffix}']
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return [f'{field} must be an ISO8601 string']
    return []


def validate_state_payload(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ['state must be a JSON object']

    errors: list[str] = []

    if type(data.get('active')) is not bool:
        errors.append('active must be a boolean')

    prompt = data.get('prompt')
    if not isinstance(prompt, str):
        errors.append('prompt must be a string')

    iteration = data.get('iteration')
    if type(iteration) is not int:
        errors.append('iteration must be an integer')
    elif iteration < 0:
        errors.append('iteration must be >= 0')

    max_iterations = data.get('max_iterations')
    if type(max_iterations) is not int:
        errors.append('max_iterations must be an integer')
    elif max_iterations < 1:
        errors.append('max_iterations must be >= 1')

    completion_token = data.get('completion_token')
    if not isinstance(completion_token, str) or not completion_token:
        errors.append('completion_token must be a non-empty string')

    claimed_session_id = data.get('claimed_session_id')
    if claimed_session_id is not None and not isinstance(claimed_session_id, str):
        errors.append('claimed_session_id must be a string or null')

    phase = data.get('phase')
    if not isinstance(phase, str) or phase not in ALLOWED_PHASES:
        errors.append(f'phase must be one of {sorted(ALLOWED_PHASES)}')

    errors.extend(_validate_iso(data.get('started_at'), 'started_at', allow_null=True))
    errors.extend(_validate_iso(data.get('updated_at'), 'updated_at', allow_null=True))

    last_message_fingerprint = data.get('last_message_fingerprint')
    if last_message_fingerprint is not None and not isinstance(last_message_fingerprint, str):
        errors.append('last_message_fingerprint must be a string or null')

    repeat_count = data.get('repeat_count')
    if type(repeat_count) is not int:
        errors.append('repeat_count must be an integer')
    elif repeat_count < 0:
        errors.append('repeat_count must be >= 0')

    return errors


def normalize_state_payload(data: Any) -> Any:
    if not isinstance(data, dict):
        return data

    normalized = data
    for field, value in LEGACY_STATE_DEFAULTS.items():
        if field in normalized:
            continue
        # Trade-off: accept the pre-upgrade state shape by filling only the fields that
        # were added after the original persisted schema. Other missing or malformed
        # fields still fail validation so we do not silently bless truncated or corrupted
        # state files as healthy.
        if normalized is data:
            normalized = dict(data)
        normalized[field] = value
    return normalized


def read_state(cwd: str | None = None) -> StateReadResult:
    path = state_path(cwd)
    symlink_error = _managed_storage_error(cwd, STATE_RELATIVE_PATH)
    if symlink_error is not None:
        return StateReadResult(
            status='read_error',
            errors=(f'unable to read {path}: {symlink_error}',),
        )
    if not path.exists():
        return StateReadResult(status='missing')

    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        return StateReadResult(
            status='invalid_json',
            errors=(f'invalid JSON: {exc.msg}',),
        )
    except (OSError, UnicodeDecodeError) as exc:
        return StateReadResult(
            status='read_error',
            errors=(f'unable to read {path}: {exc}',),
        )

    payload = normalize_state_payload(payload)
    errors = validate_state_payload(payload)
    if errors:
        return StateReadResult(status='invalid_schema', errors=tuple(errors))

    return StateReadResult(status='ok', value=cast(LoopState, payload))


def save_state(state: LoopState, cwd: str | None = None) -> Path:
    errors = validate_state_payload(state)
    if errors:
        raise ValueError('; '.join(errors))

    path = state_path(cwd)
    symlink_error = _managed_storage_error(cwd, STATE_RELATIVE_PATH)
    if symlink_error is not None:
        raise StorageError(f'unable to write {path}: {symlink_error}')
    try:
        _write_text_atomic(path, json.dumps(state, indent=2, ensure_ascii=True) + '\n')
    except OSError as exc:
        raise StorageError(f'unable to write {path}: {exc}') from exc
    return path


def clear_state(cwd: str | None = None) -> None:
    path = state_path(cwd)
    symlink_error = symlink_parent_error(_storage_root(cwd), STATE_RELATIVE_PATH)
    if symlink_error is not None:
        raise StorageError(f'unable to remove {path}: {symlink_error}')
    try:
        if path.exists() or path.is_symlink():
            path.unlink()
    except OSError as exc:
        raise StorageError(f'unable to remove {path}: {exc}') from exc


def validate_progress_entry(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ['progress entry must be a JSON object']

    errors: list[str] = []

    errors.extend(_validate_iso(data.get('ts'), 'ts', allow_null=False))

    iteration = data.get('iteration')
    if type(iteration) is not int:
        errors.append('iteration must be an integer')
    elif iteration < 0:
        errors.append('iteration must be >= 0')

    session_id = data.get('session_id')
    if session_id is not None and not isinstance(session_id, str):
        errors.append('session_id must be a string or null')

    status = data.get('status')
    if not isinstance(status, str) or status not in LEDGER_PROGRESS_STATUSES:
        errors.append(f'status must be one of {sorted(LEDGER_PROGRESS_STATUSES)}')

    if not isinstance(data.get('summary'), str):
        errors.append('summary must be a string')

    files = data.get('files')
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        errors.append('files must be a list of strings')

    checks = data.get('checks')
    if not isinstance(checks, list) or any(not isinstance(item, str) for item in checks):
        errors.append('checks must be a list of strings')

    message_fingerprint = data.get('message_fingerprint')
    if message_fingerprint is not None and not isinstance(message_fingerprint, str):
        errors.append('message_fingerprint must be a string or null')

    reason = data.get('reason')
    if reason is not None and not isinstance(reason, str):
        errors.append('reason must be a string or null')

    return errors


def append_progress_entry(entry: ProgressEntry, cwd: str | None = None) -> Path:
    errors = validate_progress_entry(entry)
    if errors:
        raise ValueError('; '.join(errors))

    path = progress_path(cwd)
    symlink_error = _managed_storage_error(cwd, PROGRESS_RELATIVE_PATH)
    if symlink_error is not None:
        raise StorageError(f'unable to write {path}: {symlink_error}')
    preserved_lines: list[str] = []
    if path.exists():
        preserved_lines, existing_errors = _read_progress_lines(path)
        if existing_errors:
            details = '; '.join(existing_errors)
            raise StorageError(
                f'progress ledger is invalid at {path}; repair it before continuing. Details: {details}'
            )
    try:
        preserved_lines.append(json.dumps(entry, ensure_ascii=True))
        _write_text_atomic(path, ''.join(f'{line}\n' for line in preserved_lines))
    except OSError as exc:
        raise StorageError(f'unable to write {path}: {exc}') from exc
    return path


def _read_progress_lines(path: Path) -> tuple[list[str], list[str]]:
    preserved_lines: list[str] = []
    errors: list[str] = []
    try:
        raw_text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        return preserved_lines, [f'unable to read {path}: {exc}']

    lines = raw_text.splitlines()
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            # Trade-off: atomic writes mean Ralph should never emit a half-written JSONL row.
            # Any malformed line now blocks validation and future appends, even if it is the
            # final line without a trailing newline, because silently dropping it would erase
            # evidence of ledger corruption from manual edits or external damage.
            errors.append(f'line {lineno}: invalid JSON ({exc.msg})')
            continue
        entry_errors = validate_progress_entry(payload)
        for error in entry_errors:
            errors.append(f'line {lineno}: {error}')
        if not entry_errors:
            preserved_lines.append(raw_line)
    return preserved_lines, errors


def validate_progress_file(path: Path, *, cwd: str | None = None) -> list[str]:
    if cwd is not None and path == progress_path(cwd):
        symlink_error = _managed_storage_error(cwd, PROGRESS_RELATIVE_PATH)
        if symlink_error is not None:
            # Trade-off: keep the JSONL validator reusable for arbitrary files, but when
            # callers inspect Ralph's managed ledger also apply the same fail-closed
            # symlink policy as runtime writes so doctor cannot bless an unusable path.
            return [f'unable to read {path}: {symlink_error}']
    _, errors = _read_progress_lines(path)
    return errors
