from __future__ import annotations

import json
import sys

from common import (
    ParsedRalphStatus,
    ProgressDetails,
    completion_token_emitted,
    fingerprint_message,
    now_iso,
    parse_ralph_status,
    parse_trailing_ralph_status,
    truncate_summary,
)
from state_store import (
    LoopState,
    ProgressEntry,
    StorageError,
    append_progress_entry,
    clear_state,
    read_state,
    save_state,
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


def main() -> int:
    payload = json.load(sys.stdin)
    cwd = payload.get('cwd')
    session_id = payload.get('session_id')

    state_result = read_state(cwd)
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

    state = state_result.value
    assert state is not None

    try:
        if not state['active']:
            return 0

        if state['phase'] != 'running':
            return 0

        claimed_session_id = state['claimed_session_id']
        if claimed_session_id and session_id and claimed_session_id != session_id:
            return 0

        claimed_state = dict(state)
        if not claimed_session_id and session_id:
            claimed_state['claimed_session_id'] = session_id

        last_assistant_message = payload.get('last_assistant_message') or ''
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
                    'Use $ralph-loop only if you want to discard the current loop and start over.'
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
                'Use $ralph-loop only if you want to discard the current loop and start over.'
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
                'Use $ralph-loop only if you want to discard the current loop and start over.'
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
            'SUMMARY: <single-line summary>\n'
            'FILES: path/a, path/b\n'
            'CHECKS: passed:npm test; failed:pytest -q\n'
            '---END_RALPH_STATUS---\n'
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
    except StorageError as exc:
        # When Ralph storage is already broken, stop cleanly instead of guessing a partial repair.
        emit_stop(storage_error_message(str(exc)))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
