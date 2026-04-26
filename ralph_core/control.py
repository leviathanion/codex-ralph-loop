from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ralph_core.errors import StorageError
from ralph_core.model import (
    ASSISTANT_PROGRESS_STATUSES,
    DEFAULT_MAX_ITERATIONS,
    LoopState,
    PendingUpdate,
    STATE_RELATIVE_PATH,
    now_iso,
)
from ralph_core.protocol import normalize_text, truncate_summary
from ralph_core.storage import (
    StateReadResult,
    append_progress_entry,
    atomic_write_bytes,
    clear_state,
    default_state,
    read_state,
    save_state,
    state_path,
    symlink_component_error,
    validate_pending_update,
    validate_state_payload,
    workspace_lock,
    workspace_path,
    workspace_root,
    workspace_root_error,
)


SnapshotKind = Literal['missing', 'file']


@dataclass(frozen=True)
class StateSnapshot:
    kind: SnapshotKind
    contents: bytes | None = None


def _state_storage_error(cwd: str | None) -> str | None:
    root = Path(cwd) if cwd else workspace_root()
    return symlink_component_error(root, STATE_RELATIVE_PATH)


def _validate_workspace_root(cwd: str | None) -> None:
    error = workspace_root_error(workspace_path(cwd))
    if error is not None:
        raise StorageError(error)


def _validate_start_request(prompt: str, max_iterations: int) -> None:
    if not prompt.strip():
        raise ValueError('prompt must not be empty')

    candidate_state = default_state()
    candidate_state.update({
        'prompt': prompt,
        'max_iterations': max_iterations,
        'started_at': now_iso(),
        'updated_at': now_iso(),
    })
    errors = validate_state_payload(candidate_state)
    if errors:
        raise ValueError('; '.join(errors))


def _normalize_files(items: list[str] | None) -> list[str]:
    if items is None:
        return []
    return [item.strip() for item in items if item.strip()]


def _normalize_checks(items: list[str] | None) -> list[str]:
    if items is None:
        return []
    return [item.strip() for item in items if item.strip()]


def build_pending_update(
    *,
    iteration: int,
    session_id: str,
    status: str,
    summary: str,
    files: list[str] | None = None,
    checks: list[str] | None = None,
    reason: str | None = None,
) -> PendingUpdate:
    normalized_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
    payload: PendingUpdate = {
        'iteration': iteration,
        'session_id': session_id,
        'status': status,
        'summary': truncate_summary(summary),
        'files': _normalize_files(files),
        'checks': _normalize_checks(checks),
        'reason': normalized_reason,
        'updated_at': now_iso(),
    }
    errors = validate_pending_update(payload)
    if errors:
        raise ValueError('; '.join(errors))
    return payload


def progress_entry(
    *,
    iteration: int,
    status: str,
    summary: str,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        'ts': now_iso(),
        'iteration': iteration,
        'session_id': None,
        'status': status,
        'summary': summary,
        'files': [],
        'checks': [],
        'message_fingerprint': None,
        'reason': reason,
    }


def emit_result(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload))
    return 0


def state_value_or_storage_error(result: StateReadResult, cwd: str | None) -> LoopState:
    if result.value is None:
        raise StorageError(f'unable to read {state_path(cwd)}: internal state read returned no payload')
    return result.value


def snapshot_state(cwd: str | None) -> StateSnapshot:
    path = state_path(cwd)
    symlink_error = _state_storage_error(cwd)
    if symlink_error is not None:
        raise StorageError(f'unable to snapshot {path} before starting Ralph: {symlink_error}')
    if not path.exists():
        return StateSnapshot(kind='missing')
    if path.is_dir():
        raise StorageError(f'unable to snapshot {path} before starting Ralph: expected a file, found directory')
    try:
        return StateSnapshot(kind='file', contents=path.read_bytes())
    except OSError as exc:
        raise StorageError(f'unable to snapshot {path} before starting Ralph: {exc}') from exc


def restore_state(snapshot: StateSnapshot, cwd: str | None) -> None:
    path = state_path(cwd)
    symlink_error = _state_storage_error(cwd)
    if symlink_error is not None:
        raise StorageError(f'unable to restore {path}: {symlink_error}')
    if snapshot.kind == 'missing':
        clear_state(cwd)
        return
    if snapshot.kind == 'file':
        if snapshot.contents is None:
            raise StorageError(f'unable to restore {path}: internal file snapshot is missing contents')
        write_bytes_atomic(path, snapshot.contents)
        return
    raise StorageError(f'unable to restore {path}: unsupported state snapshot kind {snapshot.kind!r}')


def write_bytes_atomic(path: Path, contents: bytes) -> None:
    try:
        atomic_write_bytes(path, contents)
    except OSError as exc:
        raise StorageError(f'unable to restore {path}: {exc}') from exc


def _ensure_startable_state(result: StateReadResult, cwd: str | None) -> None:
    if result.status == 'invalid_json':
        raise ValueError(
            'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json before starting a new loop.'
        )
    if result.status == 'invalid_schema':
        raise ValueError(
            'Ralph state is incompatible with the current schema. Run $cancel-ralph before starting a new loop.'
        )
    if result.status == 'read_error':
        raise StorageError('; '.join(result.errors))
    if result.status == 'ok':
        raise ValueError(
            'A Ralph loop state already exists in this workspace. Use $continue-ralph-loop to resume it, or '
            '$cancel-ralph before starting a new loop.'
        )


def start_loop(
    *,
    cwd: str | None,
    prompt: str,
    max_iterations: int,
) -> dict[str, Any]:
    _validate_start_request(prompt, max_iterations)
    _validate_workspace_root(cwd)
    _ensure_startable_state(read_state(cwd), cwd)

    with workspace_lock(cwd):
        return _start_loop_locked(
            cwd=cwd,
            prompt=prompt,
            max_iterations=max_iterations,
        )


def _start_loop_locked(
    *,
    cwd: str | None,
    prompt: str,
    max_iterations: int,
) -> dict[str, Any]:
    _ensure_startable_state(read_state(cwd), cwd)

    timestamp = now_iso()
    state = default_state()
    state.update({
        'prompt': prompt,
        'iteration': 0,
        'max_iterations': max_iterations,
        'claimed_session_id': None,
        'phase': 'running',
        'pending_update': None,
        'last_status': None,
        'started_at': timestamp,
        'updated_at': timestamp,
        'last_message_fingerprint': None,
        'repeat_count': 0,
    })

    prior_state = snapshot_state(cwd)
    save_state(state, cwd)
    try:
        append_progress_entry(
            progress_entry(
                iteration=0,
                status='started',
                summary='Ralph loop started',
            ),
            cwd,
        )
    except StorageError as exc:
        try:
            restore_state(prior_state, cwd)
        except StorageError as rollback_exc:
            rollback_action = 'restore the prior state' if prior_state.kind != 'missing' else 'remove the new state'
            raise StorageError(
                f'{exc}; also failed to {rollback_action} after the start ledger write failed: {rollback_exc}'
            ) from exc
        raise

    return {
        'status': 'started',
        'prompt': prompt,
        'iteration': 0,
        'max_iterations': max_iterations,
    }


def resume_loop(*, cwd: str | None) -> dict[str, Any]:
    _validate_workspace_root(cwd)
    result = read_state(cwd)
    if result.status == 'missing':
        return {
            'status': 'missing',
            'message': 'No active Ralph loop state exists in this workspace.',
        }
    if result.status == 'invalid_json':
        return {
            'status': 'invalid_json',
            'message': 'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json.',
            'errors': list(result.errors),
        }
    if result.status == 'invalid_schema':
        return {
            'status': 'invalid_schema',
            'message': 'Ralph state is incompatible with the current schema. Run $cancel-ralph before continuing.',
            'errors': list(result.errors),
        }
    if result.status == 'read_error':
        raise StorageError('; '.join(result.errors))

    with workspace_lock(cwd):
        return _resume_loop_locked(cwd=cwd)


def _resume_loop_locked(*, cwd: str | None) -> dict[str, Any]:
    result = read_state(cwd)
    if result.status == 'missing':
        return {
            'status': 'missing',
            'message': 'No active Ralph loop state exists in this workspace.',
        }
    if result.status == 'invalid_json':
        return {
            'status': 'invalid_json',
            'message': 'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json.',
            'errors': list(result.errors),
        }
    if result.status == 'invalid_schema':
        return {
            'status': 'invalid_schema',
            'message': 'Ralph state is incompatible with the current schema. Run $cancel-ralph before continuing.',
            'errors': list(result.errors),
        }
    if result.status == 'read_error':
        raise StorageError('; '.join(result.errors))

    state = state_value_or_storage_error(result, cwd)
    resumed_state = dict(state)
    resumed_state['claimed_session_id'] = None
    resumed_state['phase'] = 'running'
    resumed_state['pending_update'] = None
    resumed_state['updated_at'] = now_iso()
    resumed_state['last_message_fingerprint'] = None
    resumed_state['repeat_count'] = 0

    save_state(resumed_state, cwd)
    try:
        append_progress_entry(
            progress_entry(
                iteration=state['iteration'],
                status='resumed',
                summary='Ralph loop resumed',
                reason='manual_resume',
            ),
            cwd,
        )
    except StorageError as exc:
        try:
            save_state(state, cwd)
        except StorageError as rollback_exc:
            raise StorageError(
                f'{exc}; also failed to restore the prior state after the resume ledger write failed: {rollback_exc}'
            ) from exc
        raise

    return {
        'status': 'resumed',
        'prompt': resumed_state['prompt'],
        'iteration': resumed_state['iteration'],
        'max_iterations': resumed_state['max_iterations'],
    }


def report_loop(
    *,
    cwd: str | None,
    session_id: str,
    status: str,
    summary: str,
    files: list[str] | None = None,
    checks: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    _validate_workspace_root(cwd)
    if status not in ASSISTANT_PROGRESS_STATUSES:
        raise ValueError(f'status must be one of {sorted(ASSISTANT_PROGRESS_STATUSES)}')
    if status in {'blocked', 'failed'} and not normalize_text(reason or ''):
        raise ValueError('reason is required for blocked and failed status updates')

    with workspace_lock(cwd):
        return _report_loop_locked(
            cwd=cwd,
            session_id=session_id,
            status=status,
            summary=summary,
            files=files,
            checks=checks,
            reason=reason,
        )


def _report_loop_locked(
    *,
    cwd: str | None,
    session_id: str,
    status: str,
    summary: str,
    files: list[str] | None,
    checks: list[str] | None,
    reason: str | None,
) -> dict[str, Any]:
    result = read_state(cwd)
    if result.status == 'missing':
        raise ValueError('No Ralph loop state exists in this workspace.')
    if result.status == 'invalid_json':
        raise ValueError('Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json.')
    if result.status == 'invalid_schema':
        raise ValueError('Ralph state is incompatible with the current schema. Run $cancel-ralph before continuing.')
    if result.status == 'read_error':
        raise StorageError('; '.join(result.errors))

    state = state_value_or_storage_error(result, cwd)
    if state['phase'] != 'running':
        raise ValueError(f'Ralph loop is not currently running; current phase is {state["phase"]}.')
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('session_id must be a non-empty string')
    claimed_session_id = state['claimed_session_id']
    if claimed_session_id is not None and claimed_session_id != session_id:
        raise ValueError('Ralph loop is claimed by a different Codex session.')

    pending_update = build_pending_update(
        iteration=state['iteration'],
        session_id=session_id,
        status=status,
        summary=summary,
        files=files,
        checks=checks,
        reason=reason,
    )
    next_state = dict(state)
    # Trade-off: first reporter wins while a newly started loop is still unclaimed.
    # There is no trusted owner before the first Stop payload, so binding the report
    # and claim atomically is safer than leaving a terminal update consumable by any
    # later session.
    if claimed_session_id is None:
        next_state['claimed_session_id'] = session_id
    next_state['pending_update'] = pending_update
    next_state['updated_at'] = pending_update['updated_at']
    save_state(next_state, cwd)
    return {
        'status': 'reported',
        'iteration': state['iteration'],
        'reported_status': status,
    }


def session_id_from_environment() -> str | None:
    for name in ('RALPH_SESSION_ID', 'CODEX_SESSION_ID', 'CODEX_THREAD_ID'):
        value = os.environ.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def cancel_loop(*, cwd: str | None) -> dict[str, Any]:
    _validate_workspace_root(cwd)
    result = read_state(cwd)
    if result.status == 'missing':
        return {
            'status': 'missing',
            'message': 'No Ralph loop state was present.',
        }
    if result.status == 'read_error':
        state_file = state_path(cwd)
        if not state_file.is_symlink():
            raise StorageError('; '.join(result.errors))

    with workspace_lock(cwd):
        return _cancel_loop_locked(cwd=cwd)


def _cancel_loop_locked(*, cwd: str | None) -> dict[str, Any]:
    state_file = state_path(cwd)
    state_existed = state_file.exists() or state_file.is_symlink()
    result = read_state(cwd)
    valid_state = result.status == 'ok' and result.value is not None
    state = result.value if valid_state else None

    if state_existed:
        clear_state(cwd)

    if state is not None:
        try:
            append_progress_entry(
                progress_entry(
                    iteration=state['iteration'],
                    status='cancelled',
                    summary='Ralph loop cancelled manually',
                    reason='manual_cancel',
                ),
                cwd,
            )
        except StorageError as exc:
            raise StorageError(
                f'Cleared .codex/ralph/state.json, but failed to append the cancellation ledger row: {exc}'
            ) from exc

    if not state_existed:
        return {
            'status': 'missing',
            'message': 'No Ralph loop state was present.',
        }

    if state is None:
        return {
            'status': 'cleared_invalid_state',
            'message': 'Cleared Ralph loop state without appending progress because the state file was invalid.',
        }

    return {
        'status': 'cleared',
        'message': 'Cleared Ralph loop state and appended a cancellation ledger row.',
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command', required=True)

    start_parser = subparsers.add_parser('start')
    start_parser.add_argument('--cwd')
    start_parser.add_argument('--prompt')
    start_parser.add_argument('--max-iterations', type=int, default=DEFAULT_MAX_ITERATIONS)

    resume_parser = subparsers.add_parser('resume')
    resume_parser.add_argument('--cwd')

    report_parser = subparsers.add_parser('report')
    report_parser.add_argument('--cwd')
    report_parser.add_argument('--session-id', default=session_id_from_environment())
    report_parser.add_argument('--status', required=True)
    report_parser.add_argument('--summary', required=True)
    report_parser.add_argument('--reason')
    report_parser.add_argument('--file', action='append', dest='files')
    report_parser.add_argument('--check', action='append', dest='checks')

    cancel_parser = subparsers.add_parser('cancel')
    cancel_parser.add_argument('--cwd')

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'start':
            prompt = args.prompt if args.prompt is not None else sys.stdin.read()
            return emit_result(start_loop(
                cwd=args.cwd,
                prompt=prompt,
                max_iterations=args.max_iterations,
            ))
        if args.command == 'resume':
            return emit_result(resume_loop(cwd=args.cwd))
        if args.command == 'report':
            if not args.session_id:
                parser.error('report requires --session-id or RALPH_SESSION_ID/CODEX_SESSION_ID/CODEX_THREAD_ID')
            return emit_result(report_loop(
                cwd=args.cwd,
                session_id=args.session_id,
                status=args.status,
                summary=args.summary,
                files=args.files,
                checks=args.checks,
                reason=args.reason,
            ))
        return emit_result(cancel_loop(cwd=args.cwd))
    except (StorageError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
