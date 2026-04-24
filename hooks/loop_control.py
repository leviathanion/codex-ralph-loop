from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from common import (
    DEFAULT_COMPLETION_TOKEN,
    DEFAULT_MAX_ITERATIONS,
    STATE_RELATIVE_PATH,
    atomic_write_bytes,
    now_iso,
    state_path,
    symlink_component_error,
    workspace_path,
    workspace_root,
    workspace_root_error,
)
from state_store import (
    LoopState,
    StorageError,
    StateReadResult,
    append_progress_entry,
    clear_state,
    default_state,
    read_state,
    save_state,
    validate_state_payload,
    workspace_lock,
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


def _validate_start_request(prompt: str, max_iterations: int, completion_token: str) -> None:
    if not prompt.strip():
        raise ValueError('prompt must not be empty')

    candidate_state = default_state()
    candidate_state.update({
        'active': True,
        'prompt': prompt,
        'max_iterations': max_iterations,
        'completion_token': completion_token,
    })
    errors = validate_state_payload(candidate_state)
    if errors:
        raise ValueError('; '.join(errors))


def progress_entry(*, iteration: int, status: str, summary: str, reason: str | None = None) -> dict[str, Any]:
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


def start_loop(
    *,
    cwd: str | None,
    prompt: str,
    max_iterations: int,
    completion_token: str,
) -> dict[str, Any]:
    _validate_start_request(prompt, max_iterations, completion_token)

    _validate_workspace_root(cwd)
    existing_state = read_state(cwd)
    if existing_state.status == 'invalid_json':
        raise ValueError(
            'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json before starting a new loop.'
        )
    if existing_state.status == 'invalid_schema':
        raise ValueError(
            'Ralph state failed schema validation. Run $cancel-ralph or repair .codex/ralph/state.json before starting a new loop.'
        )
    if existing_state.status == 'read_error':
        raise StorageError('; '.join(existing_state.errors))
    if existing_state.status == 'ok':
        current_state = state_value_or_storage_error(existing_state, cwd)
        if current_state['active']:
            raise ValueError(
                'An active Ralph loop already exists in this workspace. Use $continue-ralph-loop to resume it, or '
                '$cancel-ralph before starting a new loop.'
            )

    with workspace_lock(cwd):
        return _start_loop_locked(
            cwd=cwd,
            prompt=prompt,
            max_iterations=max_iterations,
            completion_token=completion_token,
        )


def _start_loop_locked(
    *,
    cwd: str | None,
    prompt: str,
    max_iterations: int,
    completion_token: str,
) -> dict[str, Any]:
    existing_state = read_state(cwd)
    if existing_state.status == 'invalid_json':
        raise ValueError(
            'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json before starting a new loop.'
        )
    if existing_state.status == 'invalid_schema':
        raise ValueError(
            'Ralph state failed schema validation. Run $cancel-ralph or repair .codex/ralph/state.json before starting a new loop.'
        )
    if existing_state.status == 'read_error':
        raise StorageError('; '.join(existing_state.errors))
    if existing_state.status == 'ok':
        current_state = state_value_or_storage_error(existing_state, cwd)
        if current_state['active']:
            raise ValueError(
                'An active Ralph loop already exists in this workspace. Use $continue-ralph-loop to resume it, or '
                '$cancel-ralph before starting a new loop.'
            )

    timestamp = now_iso()
    state = default_state()
    state.update({
        'active': True,
        'prompt': prompt,
        'iteration': 0,
        'max_iterations': max_iterations,
        'completion_token': completion_token,
        'claimed_session_id': None,
        'phase': 'running',
        'started_at': timestamp,
        'updated_at': timestamp,
        'last_message_fingerprint': None,
        'repeat_count': 0,
    })

    # Trade-off: starting Ralph must not leave a half-started loop behind, but it also must not
    # destroy whatever state file was already in the workspace. Snapshot any pre-existing state
    # before overwriting it so a failed initial ledger write can restore the prior bytes exactly.
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
        'completion_token': completion_token,
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
            'message': 'Ralph state failed schema validation. Run $cancel-ralph or repair .codex/ralph/state.json.',
            'errors': list(result.errors),
        }
    if result.status == 'read_error':
        raise StorageError('; '.join(result.errors))

    state = state_value_or_storage_error(result, cwd)
    if not state['active']:
        return {
            'status': 'inactive',
            'message': 'The workspace state file exists but there is no active Ralph loop to continue.',
        }

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
            'message': 'Ralph state failed schema validation. Run $cancel-ralph or repair .codex/ralph/state.json.',
            'errors': list(result.errors),
        }
    if result.status == 'read_error':
        raise StorageError('; '.join(result.errors))

    state = state_value_or_storage_error(result, cwd)
    if not state['active']:
        return {
            'status': 'inactive',
            'message': 'The workspace state file exists but there is no active Ralph loop to continue.',
        }

    resume_summary = 'Ralph loop resumed'
    resume_reason = None
    if state['phase'] == 'running':
        # Trade-off: an explicit resume can duplicate work if a live Codex session is still
        # holding an unpersisted turn, but it is also the only reliable recovery signal after a
        # crash or restart. Prioritize recoverability over trying to infer liveness from the
        # persisted state alone, and record which kind of running state had to be reclaimed.
        if state['claimed_session_id'] is None:
            resume_summary = 'Ralph loop resumed after reclaiming an orphaned running state'
            resume_reason = 'orphaned_running_state'
        else:
            resume_summary = 'Ralph loop resumed after reclaiming a prior session claim'
            resume_reason = 'session_reclaimed'

    resumed_state = dict(state)
    resumed_state['claimed_session_id'] = None
    resumed_state['phase'] = 'running'
    resumed_state['updated_at'] = now_iso()
    resumed_state['last_message_fingerprint'] = None
    resumed_state['repeat_count'] = 0

    # Trade-off: resuming should only flip the loop back to running if both the state update and
    # the "resumed" audit row succeed, so restore the prior state if the ledger write fails.
    save_state(resumed_state, cwd)
    try:
        append_progress_entry(
            progress_entry(
                iteration=state['iteration'],
                status='resumed',
                summary=resume_summary,
                reason=resume_reason,
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
        'completion_token': resumed_state['completion_token'],
    }


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
            # Trade-off: cancellation prioritizes clearing control state. A later ledger failure is
            # surfaced, but Ralph is still stopped instead of continuing against user intent.
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
    start_parser.add_argument('--completion-token', default=DEFAULT_COMPLETION_TOKEN)

    resume_parser = subparsers.add_parser('resume')
    resume_parser.add_argument('--cwd')

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
                completion_token=args.completion_token,
            ))
        if args.command == 'resume':
            return emit_result(resume_loop(cwd=args.cwd))
        return emit_result(cancel_loop(cwd=args.cwd))
    except (StorageError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
