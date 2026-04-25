from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from ralph_core.errors import StorageError
from ralph_core.model import (
    ALLOWED_PHASES,
    DEFAULT_COMPLETION_TOKEN,
    DEFAULT_MAX_ITERATIONS,
    LOCK_RELATIVE_PATH,
    LEDGER_PROGRESS_STATUSES,
    PROGRESS_RELATIVE_PATH,
    SCHEMA_VERSION,
    STATE_RELATIVE_PATH,
    LoopState,
    ProgressEntry,
)


ReadStatus = Literal['missing', 'invalid_json', 'invalid_schema', 'read_error', 'ok']


@dataclass(frozen=True)
class StateReadResult:
    status: ReadStatus
    value: LoopState | None = None
    errors: tuple[str, ...] = ()


LOCK_POLL_SECONDS = 0.05
STATE_FIELDS = frozenset(LoopState.__annotations__)
PROGRESS_ENTRY_FIELDS = frozenset(ProgressEntry.__annotations__)


def workspace_root() -> Path:
    return Path(os.environ.get('PWD') or os.getcwd())


def workspace_path(cwd: str | None = None) -> Path:
    return Path(cwd) if cwd else workspace_root()


def workspace_root_error(root: Path) -> str | None:
    try:
        if not root.exists():
            return f'workspace path does not exist: {root}'
        if not root.is_dir():
            return f'workspace path is not a directory: {root}'
    except OSError as exc:
        return f'unable to access workspace path {root}: {exc}'
    return None


def state_path(cwd: str | None = None) -> Path:
    return workspace_path(cwd) / STATE_RELATIVE_PATH


def progress_path(cwd: str | None = None) -> Path:
    return workspace_path(cwd) / PROGRESS_RELATIVE_PATH


def symlink_component_error(root: Path, relative_path: Path) -> str | None:
    current = root
    for part in relative_path.parts:
        current = current / part
        if not current.is_symlink():
            continue
        # Trade-off: Ralph's managed paths are internal control surfaces. Reject all symlink
        # components so continuation state cannot be redirected by path aliasing.
        if current.exists():
            return f'path component is a symlink: {current}'
        return f'path component is a dangling symlink: {current}'
    return None


def symlink_parent_error(root: Path, relative_path: Path) -> str | None:
    parent = relative_path.parent
    if parent == Path('.'):
        return None
    return symlink_component_error(root, parent)


def resolve_atomic_write_target(path: Path, *, preserve_leaf_symlink: bool) -> Path:
    if not preserve_leaf_symlink or not path.is_symlink():
        return path
    try:
        return path.resolve(strict=False)
    except RuntimeError as exc:
        raise OSError(f'unable to resolve symlink target for {path}: {exc}') from exc


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(
    path: Path,
    contents: str,
    *,
    preserve_leaf_symlink: bool = False,
) -> Path:
    target = resolve_atomic_write_target(path, preserve_leaf_symlink=preserve_leaf_symlink)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f'.{target.name}.',
        suffix='.tmp',
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        try:
            fsync_directory(target.parent)
        except OSError:
            pass
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return target


def atomic_write_bytes(
    path: Path,
    contents: bytes,
    *,
    preserve_leaf_symlink: bool = False,
) -> Path:
    target = resolve_atomic_write_target(path, preserve_leaf_symlink=preserve_leaf_symlink)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f'.{target.name}.',
        suffix='.tmp',
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        try:
            fsync_directory(target.parent)
        except OSError:
            pass
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return target


def _storage_root(cwd: str | None) -> Path:
    return workspace_path(cwd)


def _managed_storage_error(cwd: str | None, relative_path: Path) -> str | None:
    return symlink_component_error(_storage_root(cwd), relative_path)


def acquire_workspace_lock(handle, path: Path, timeout_seconds: float | None) -> None:
    if timeout_seconds is None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise StorageError(f'timed out waiting for Ralph control lock at {path}')
            remaining = deadline - time.monotonic()
            time.sleep(min(LOCK_POLL_SECONDS, max(0.0, remaining)))


@contextmanager
def workspace_lock(cwd: str | None = None, *, timeout_seconds: float | None = None):
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
            acquire_workspace_lock(handle, path, timeout_seconds)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise StorageError(f'unable to lock {path}: {exc}') from exc


def _write_text_atomic(path: Path, contents: str) -> None:
    atomic_write_text(path, contents)


def default_state() -> LoopState:
    return {
        'schema_version': SCHEMA_VERSION,
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


def unknown_field_error(prefix: str, data: dict[Any, Any], allowed_fields: frozenset[str]) -> str | None:
    unknown_fields = set(data) - allowed_fields
    if not unknown_fields:
        return None
    field_names = ', '.join(sorted(str(field) for field in unknown_fields))
    return f'unknown {prefix} field(s): {field_names}'


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
    unknown_fields_error = unknown_field_error('state', data, STATE_FIELDS)
    if unknown_fields_error is not None:
        # Trade-off: rejecting unknown state fields means future schema additions must bump
        # schema_version instead of being silently ignored by older runtime code. That is safer
        # for a file that decides whether Codex should keep executing.
        errors.append(unknown_fields_error)

    schema_version = data.get('schema_version')
    if schema_version != SCHEMA_VERSION:
        errors.append(f'schema_version must be {SCHEMA_VERSION}')

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
    if not isinstance(completion_token, str) or not completion_token.strip():
        errors.append('completion_token must be a non-empty string')
    elif completion_token != completion_token.strip() or len(completion_token.splitlines()) != 1:
        # Trade-off: Ralph allows custom completion tokens, including internal spaces, but the
        # Stop hook compares one trimmed final line. Reject surrounding whitespace and line
        # separators at the schema boundary so users cannot start a loop that can never complete.
        errors.append('completion_token must be a single-line string without leading or trailing whitespace')

    claimed_session_id = data.get('claimed_session_id')
    if claimed_session_id is not None and (not isinstance(claimed_session_id, str) or not claimed_session_id):
        # Trade-off: reject empty persisted claims even though they are strings. Stop-hook payload
        # validation requires a non-empty session_id, so an empty claim can never match a live
        # session and would otherwise make an active running loop silently ignore every Stop hook.
        errors.append('claimed_session_id must be a non-empty string or null')

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


def read_state(cwd: str | None = None) -> StateReadResult:
    path = state_path(cwd)
    if not path.exists() and not path.is_symlink():
        # Trade-off: the Stop hook is installed globally, so absence of Ralph state must stay
        # side-effect-free even in repositories whose unrelated .codex path is a symlink.
        # Once a state leaf exists, reads still apply the strict managed-path symlink policy below.
        return StateReadResult(status='missing')

    symlink_error = _managed_storage_error(cwd, STATE_RELATIVE_PATH)
    if symlink_error is not None:
        return StateReadResult(
            status='read_error',
            errors=(f'unable to read {path}: {symlink_error}',),
        )

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
    unknown_fields_error = unknown_field_error('progress', data, PROGRESS_ENTRY_FIELDS)
    if unknown_fields_error is not None:
        errors.append(unknown_fields_error)

    errors.extend(_validate_iso(data.get('ts'), 'ts', allow_null=False))

    iteration = data.get('iteration')
    if type(iteration) is not int:
        errors.append('iteration must be an integer')
    elif iteration < 0:
        errors.append('iteration must be >= 0')

    session_id = data.get('session_id')
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        errors.append('session_id must be a non-empty string or null')

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
