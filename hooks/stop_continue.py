from __future__ import annotations

import json
import sys
from typing import Any

from common import (
    ParsedRalphStatus,
    ProgressDetails,
    completion_token_emitted,
    contains_ralph_status_markup,
    fingerprint_message,
    now_iso,
    parse_ralph_status,
    parse_trailing_ralph_status,
    state_path,
    truncate_summary,
    workspace_path,
    workspace_root_error,
)
from state_store import (
    LoopState,
    ProgressEntry,
    StateReadResult,
    StorageError,
    append_progress_entry,
    clear_state,
    read_state,
    save_state,
    workspace_lock,
)


def emit_stop(message: str) -> None:
    sys.stdout.write(json.dumps({
        'continue': False,
        'systemMessage': message,
    }))


def progress_entry(
    *,
    iteration: int,
    session_id: str | None,
    status: str,
    summary: str,
    files: list[str],
    checks: list[str],
    message_fingerprint: str | None,
    reason: str | None = None,
) -> ProgressEntry:
    return {
        'ts': now_iso(),
        'iteration': iteration,
        'session_id': session_id,
        'status': status,
        'summary': summary,
        'files': files,
        'checks': checks,
        'message_fingerprint': message_fingerprint,
        'reason': reason,
    }


def invalid_state_message(errors: tuple[str, ...]) -> str:
    details = '; '.join(errors)
    return (
        'Ralph state is invalid. Run $cancel-ralph or repair .codex/ralph/state.json before continuing. '
        f'Details: {details}'
    )


def storage_error_message(error: str) -> str:
    return (
        'Ralph storage is unavailable. Run $cancel-ralph or repair the files under .codex/ralph before continuing. '
        f'Details: {error}'
    )


def invalid_payload_message(error: str) -> str:
    return f'Ralph stop hook received invalid input from Codex. Details: {error}'


def state_read_exit_code(state_result: StateReadResult) -> int | None:
    if state_result.status == 'ok':
        return None
    if state_result.status == 'missing':
        return 0
    if state_result.status == 'invalid_json':
        emit_stop(
            'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json before continuing.'
        )
        return 0
    if state_result.status == 'read_error':
        emit_stop(storage_error_message('; '.join(state_result.errors)))
        return 0
    if state_result.status == 'invalid_schema':
        emit_stop(invalid_state_message(state_result.errors))
        return 0
    return 0


def fallback_progress_details(text: str) -> ProgressDetails:
    summary = truncate_summary(text)
    if not summary:
        summary = '(empty assistant message)'
    return {
        'summary': summary,
        'files': [],
        'checks': [],
    }


def progress_details_from_status(parsed_status: ParsedRalphStatus) -> ProgressDetails:
    return {
        'summary': parsed_status['summary'],
        'files': parsed_status['files'],
        'checks': parsed_status['checks'],
    }


def pause_loop_with_reason(
    *,
    state: LoopState,
    cwd: str | None,
    iteration: int,
    session_id: str | None,
    message_fingerprint: str,
    details: ProgressDetails,
    reason: str,
    message: str,
) -> int:
    pause_loop_and_record_progress(
        state=state,
        cwd=cwd,
        entry=progress_entry(
            iteration=iteration,
            session_id=session_id,
            status='stopped',
            summary=details['summary'],
            files=details['files'],
            checks=details['checks'],
            message_fingerprint=message_fingerprint,
            reason=reason,
        ),
    )
    emit_stop(message)
    return 0


def pause_loop_and_record_progress(
    *,
    state: LoopState,
    cwd: str | None,
    entry: ProgressEntry,
) -> None:
    # Trade-off: when pausing Ralph, persist the blocked control state before the audit row.
    # If the ledger write fails afterward, the audit trail is incomplete, but Ralph will not
    # silently auto-continue against a recorded blocked/stopped intent.
    pause_loop(state, cwd)
    append_progress_entry(entry, cwd)


def clear_loop_and_record_progress(
    *,
    cwd: str | None,
    entry: ProgressEntry,
) -> None:
    # Trade-off: for terminal outcomes, clear the control state before writing the ledger row.
    # This can lose the final audit row on a later write failure, but it guarantees the loop is
    # actually stopped instead of continuing after a "complete" or terminal "stopped" record.
    clear_state(cwd)
    append_progress_entry(entry, cwd)


def pause_loop(state: LoopState, cwd: str | None) -> None:
    state['phase'] = 'blocked'
    state['updated_at'] = now_iso()
    save_state(state, cwd)


def validated_payload(payload: Any) -> tuple[str, str, str] | None:
    if not isinstance(payload, dict):
        emit_stop(invalid_payload_message('payload must be a JSON object'))
        return None

    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not cwd:
        emit_stop(invalid_payload_message('cwd must be a non-empty string'))
        return None

    session_id = payload.get('session_id')
    # Trade-off: older Stop-hook payloads could omit session_id, but Ralph cannot safely
    # distinguish a retry from a stale or concurrent invocation without a stable session claim.
    # Fail closed here instead of guessing and risking duplicate iteration advances or silently
    # ignoring the live session after a claim has already been recorded.
    if not isinstance(session_id, str) or not session_id:
        emit_stop(invalid_payload_message('session_id must be a non-empty string'))
        return None

    last_assistant_message = payload.get('last_assistant_message')
    if last_assistant_message is None:
        normalized_message = ''
    elif isinstance(last_assistant_message, str):
        normalized_message = last_assistant_message
    else:
        emit_stop(invalid_payload_message('last_assistant_message must be a string or null'))
        return None

    return cwd, session_id, normalized_message


def validated_payload_cwd(payload: Any) -> tuple[dict[str, Any], str] | None:
    if not isinstance(payload, dict):
        emit_stop(invalid_payload_message('payload must be a JSON object'))
        return None

    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not cwd:
        emit_stop(invalid_payload_message('cwd must be a non-empty string'))
        return None

    return payload, cwd


def state_needs_session_payload(state_result: StateReadResult) -> bool:
    if state_result.status != 'ok':
        return False
    state = state_result.value
    if state is None:
        return False
    return state['active'] and state['phase'] == 'running'


def state_value_or_storage_error(state_result: StateReadResult, cwd: str) -> LoopState:
    if state_result.value is None:
        raise StorageError(f'unable to read {state_path(cwd)}: internal state read returned no payload')
    return state_result.value


def process_stop_state(
    *,
    state: LoopState,
    cwd: str,
    session_id: str,
    last_assistant_message: str,
) -> int:
    if not state['active']:
        return 0

    if state['phase'] != 'running':
        return 0

    claimed_session_id = state['claimed_session_id']
    if claimed_session_id is not None and session_id != claimed_session_id:
        return 0

    claimed_state = dict(state)
    if claimed_session_id is None:
        claimed_state['claimed_session_id'] = session_id

    parsed_status = parse_ralph_status(last_assistant_message)
    fallback_details = fallback_progress_details(last_assistant_message)
    message_fingerprint = fingerprint_message(last_assistant_message)
    iteration = state['iteration']
    max_iterations = state['max_iterations']
    token = state['completion_token']

    if completion_token_emitted(last_assistant_message, token):
        completion_body = '\n'.join(last_assistant_message.rstrip().splitlines()[:-1])
        completion_status, attempted_status_block = parse_trailing_ralph_status(completion_body)
        if completion_status['ok'] and completion_status['status'] != 'complete':
            return pause_loop_with_reason(
                state=claimed_state,
                cwd=cwd,
                iteration=iteration,
                session_id=session_id,
                message_fingerprint=message_fingerprint,
                details=progress_details_from_status(completion_status),
                reason='completion_status_mismatch',
                message=(
                    'Ralph paused because the assistant emitted the completion token but the '
                    f'RALPH_STATUS block reported STATUS={completion_status["status"]}. '
                    'A completed turn must either omit the status block or report STATUS=complete, '
                    f'and {token} must be on the final non-whitespace line by itself. '
                    'Fix the response, then resume with $continue-ralph-loop.'
                ),
            )
        if attempted_status_block and not completion_status['ok']:
            return pause_loop_with_reason(
                state=claimed_state,
                cwd=cwd,
                iteration=iteration,
                session_id=session_id,
                message_fingerprint=message_fingerprint,
                details=fallback_details,
                reason='invalid_status_block',
                message=(
                    'Ralph paused because the assistant emitted the completion token but the '
                    f'RALPH_STATUS block was malformed. Details: {completion_status["error"]}. '
                    'Either remove the status block entirely or fix it so it reports STATUS=complete, '
                    f'then resume with $continue-ralph-loop. {token} must remain on the final non-whitespace line by itself.'
                ),
            )
        if not attempted_status_block and contains_ralph_status_markup(completion_body):
            malformed_status = parse_ralph_status(completion_body)
            status_error = (
                malformed_status['error']
                if not malformed_status['ok']
                else 'RALPH_STATUS block must be the final non-whitespace content before the completion token'
            )
            return pause_loop_with_reason(
                state=claimed_state,
                cwd=cwd,
                iteration=iteration,
                session_id=session_id,
                message_fingerprint=message_fingerprint,
                details=fallback_details,
                reason='invalid_status_block',
                message=(
                    'Ralph paused because the assistant emitted the completion token but left '
                    f'non-terminal RALPH_STATUS markup earlier in the message. Details: {status_error}. '
                    'Either remove the status block entirely or move it immediately before the completion token '
                    f'and make it report STATUS=complete, then resume with $continue-ralph-loop. {token} must '
                    'remain on the final non-whitespace line by itself.'
                ),
            )
        details = progress_details_from_status(completion_status) if completion_status['ok'] else fallback_details
        clear_loop_and_record_progress(
            cwd=cwd,
            entry=progress_entry(
                iteration=iteration,
                session_id=session_id,
                status='complete',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
            ),
        )
        return 0

    if iteration >= max_iterations:
        # Trade-off: once Ralph consumes the final allowed continuation turn without the
        # completion token, stop terminally even if the status block is blocked or malformed.
        # This keeps max_iterations as a true hard ceiling instead of a pause that can resume later.
        details = progress_details_from_status(parsed_status) if parsed_status['ok'] else fallback_details
        clear_loop_and_record_progress(
            cwd=cwd,
            entry=progress_entry(
                iteration=iteration,
                session_id=session_id,
                status='stopped',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
                reason='max_iterations',
            ),
        )
        emit_stop(f'Ralph stopped after reaching max_iterations={max_iterations} without emitting {token}.')
        return 0

    if not parsed_status['ok']:
        error = parsed_status['error']
        return pause_loop_with_reason(
            state=claimed_state,
            cwd=cwd,
            iteration=iteration,
            session_id=session_id,
            message_fingerprint=message_fingerprint,
            details=fallback_details,
            reason='invalid_status_block',
            message=(
                'Ralph paused because the assistant response did not end with a valid RALPH_STATUS block. '
                f'Details: {error}. Fix the response format, then resume with $continue-ralph-loop. '
                'If you want to discard this loop and start over, run $cancel-ralph before $ralph-loop.'
            ),
        )

    if parsed_status['status'] == 'complete':
        return pause_loop_with_reason(
            state=claimed_state,
            cwd=cwd,
            iteration=iteration,
            session_id=session_id,
            message_fingerprint=message_fingerprint,
            details=progress_details_from_status(parsed_status),
            reason='missing_completion_token',
            message=(
                'Ralph paused because the assistant reported STATUS=complete without emitting '
                f'{token}. Emit the completion token only when the task is fully done, then resume with $continue-ralph-loop.'
            ),
        )

    if parsed_status['status'] == 'blocked':
        pause_loop_and_record_progress(
            state=claimed_state,
            cwd=cwd,
            entry=progress_entry(
                iteration=iteration,
                session_id=session_id,
                status='blocked',
                summary=parsed_status['summary'],
                files=parsed_status['files'],
                checks=parsed_status['checks'],
                message_fingerprint=message_fingerprint,
                reason='awaiting_user_input',
            ),
        )
        emit_stop(
            'Ralph paused because the assistant reported STATUS=blocked and needs user input. '
            'Address the blocker, then resume with $continue-ralph-loop. '
            'If you want to discard this loop and start over, run $cancel-ralph before $ralph-loop.'
        )
        return 0

    next_state = dict(claimed_state)
    repeat_count = state['repeat_count']
    last_message_fingerprint = state['last_message_fingerprint']
    if last_message_fingerprint == message_fingerprint:
        repeat_count += 1
    else:
        repeat_count = 1

    next_state['last_message_fingerprint'] = message_fingerprint
    next_state['repeat_count'] = repeat_count
    next_state['updated_at'] = now_iso()

    if repeat_count >= 3:
        pause_loop_and_record_progress(
            state=next_state,
            cwd=cwd,
            entry=progress_entry(
                iteration=iteration,
                session_id=session_id,
                status='stopped',
                summary=parsed_status['summary'],
                files=parsed_status['files'],
                checks=parsed_status['checks'],
                message_fingerprint=message_fingerprint,
                reason='repeated_response',
            ),
        )
        emit_stop(
            'Ralph paused after receiving the same assistant response three times in a row. '
            'Inspect .codex/ralph/progress.jsonl, then resume with $continue-ralph-loop. '
            'If you want to discard this loop and start over, run $cancel-ralph before $ralph-loop.'
        )
        return 0

    next_state['phase'] = 'running'
    next_state['iteration'] = iteration + 1
    # Trade-off: write the audit ledger before the control state. A later state-write failure can
    # duplicate a ledger row on retry, but it cannot silently advance the loop without persisted audit.
    append_progress_entry(progress_entry(
        iteration=iteration,
        session_id=session_id,
        status=parsed_status['status'],
        summary=parsed_status['summary'],
        files=parsed_status['files'],
        checks=parsed_status['checks'],
        message_fingerprint=message_fingerprint,
    ), cwd)
    save_state(next_state, cwd)

    prompt = state['prompt'].strip() or '(missing prompt)'
    message = (
        f"[RALPH LOOP {next_state['iteration']}/{max_iterations}]\n\n"
        'The previous assistant turn stopped without the completion token. '
        'Continue the task from the current repository state. '
        f'Only finish the loop when you can truthfully emit {token} on the final non-whitespace line by itself.\n'
        'Every unfinished turn must end with exactly one RALPH_STATUS block as the final non-whitespace content:\n'
        '---RALPH_STATUS---\n'
        'STATUS: progress|no_progress|blocked|complete\n'
        'SUMMARY: <non-empty single-line summary>\n'
        'FILES: path/a, path/b\n'
        'CHECKS: passed:npm test; failed:pytest -q\n'
        '---END_RALPH_STATUS---\n'
        'Use exactly those four fields and do not add extra fields.\n'
        'Do not put literal commas inside one FILES item or literal semicolons inside one CHECKS item.\n'
        'Do not repeat the status marker strings inside SUMMARY, FILES, or CHECKS.\n'
        'If the block is missing or malformed, Ralph will stop.\n\n'
        f'Original task:\n{prompt}'
    )
    sys.stdout.write(json.dumps({
        'decision': 'block',
        'reason': message,
    }))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        emit_stop(invalid_payload_message(f'invalid JSON payload: {exc.msg}'))
        return 0
    validated_cwd = validated_payload_cwd(payload)
    if validated_cwd is None:
        return 0

    payload, cwd = validated_cwd
    workspace_error = workspace_root_error(workspace_path(cwd))
    if workspace_error is not None:
        emit_stop(storage_error_message(workspace_error))
        return 0
    initial_state_result = read_state(cwd)
    initial_exit = state_read_exit_code(initial_state_result)
    if initial_exit is not None:
        return initial_exit
    if not state_needs_session_payload(initial_state_result):
        return 0

    validated = validated_payload(payload)
    if validated is None:
        return 0

    _, session_id, last_assistant_message = validated

    try:
        # Trade-off: probe state.json once without taking Ralph's workspace lock so the
        # global Stop hook stays side-effect-free in repositories that are not using Ralph.
        # Re-read after locking so concurrent clear/repair operations still resolve inside
        # one serialized control mutation before we append progress or rewrite state.
        with workspace_lock(cwd):
            state_result = read_state(cwd)
            state_exit = state_read_exit_code(state_result)
            if state_exit is not None:
                return state_exit

            state = state_value_or_storage_error(state_result, cwd)
            return process_stop_state(
                state=state,
                cwd=cwd,
                session_id=session_id,
                last_assistant_message=last_assistant_message,
            )
    except StorageError as exc:
        # When Ralph storage is already broken, stop cleanly instead of guessing a partial repair.
        emit_stop(storage_error_message(str(exc)))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
