from __future__ import annotations

from ralph_core.model import (
    DecisionKind,
    LoopState,
    ParsedRalphStatus,
    ProgressDetails,
    ProgressEntry,
    RuntimeDecision,
    RuntimeEffect,
    StopEvent,
    now_iso,
)
from ralph_core.prompts import continuation_prompt
from ralph_core.protocol import (
    completion_token_emitted,
    contains_ralph_status_markup,
    fingerprint_message,
    parse_ralph_status,
    parse_trailing_ralph_status,
    truncate_summary,
)


def stop_response(message: str) -> dict[str, object]:
    return {
        'continue': False,
        'systemMessage': message,
    }


def block_response(message: str) -> dict[str, object]:
    return {
        'decision': 'block',
        'reason': message,
    }


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


def save_state_effect(state: LoopState) -> RuntimeEffect:
    return RuntimeEffect(kind='save_state', state=state)


def append_progress_effect(entry: ProgressEntry) -> RuntimeEffect:
    return RuntimeEffect(kind='append_progress', progress=entry)


def clear_state_effect() -> RuntimeEffect:
    return RuntimeEffect(kind='clear_state')


def blocked_state(state: LoopState) -> LoopState:
    next_state = dict(state)
    next_state['phase'] = 'blocked'
    next_state['updated_at'] = now_iso()
    return next_state


def pause_loop_with_reason(
    *,
    state: LoopState,
    iteration: int,
    session_id: str | None,
    message_fingerprint: str,
    details: ProgressDetails,
    reason: str,
    message: str,
) -> RuntimeDecision:
    # Trade-off: pause first saves blocked control state, then writes the audit row. This can
    # lose a ledger row if the append fails, but it prevents accidental auto-continuation.
    return RuntimeDecision(
        kind='pause',
        effects=(
            save_state_effect(blocked_state(state)),
            append_progress_effect(progress_entry(
                iteration=iteration,
                session_id=session_id,
                status='stopped',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
                reason=reason,
            )),
        ),
        response=stop_response(message),
    )


def pause_with_entry(state: LoopState, entry: ProgressEntry, message: str) -> RuntimeDecision:
    return RuntimeDecision(
        kind='pause',
        effects=(
            save_state_effect(blocked_state(state)),
            append_progress_effect(entry),
        ),
        response=stop_response(message),
    )


def clear_loop_with_entry(entry: ProgressEntry, *, kind: DecisionKind, message: str | None = None) -> RuntimeDecision:
    response = stop_response(message) if message is not None else None
    return RuntimeDecision(
        kind=kind,
        effects=(
            clear_state_effect(),
            append_progress_effect(entry),
        ),
        response=response,
    )


def reduce_stop_event(state: LoopState, event: StopEvent) -> RuntimeDecision:
    if not state['active']:
        return RuntimeDecision(kind='noop')

    if state['phase'] != 'running':
        return RuntimeDecision(kind='noop')

    claimed_session_id = state['claimed_session_id']
    if claimed_session_id is not None and event.session_id != claimed_session_id:
        return RuntimeDecision(kind='noop')

    claimed_state = dict(state)
    if claimed_session_id is None:
        claimed_state['claimed_session_id'] = event.session_id

    last_assistant_message = event.last_assistant_message
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
                iteration=iteration,
                session_id=event.session_id,
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
                iteration=iteration,
                session_id=event.session_id,
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
                iteration=iteration,
                session_id=event.session_id,
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
        return clear_loop_with_entry(
            progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status='complete',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
            ),
            kind='complete',
        )

    if iteration >= max_iterations:
        details = progress_details_from_status(parsed_status) if parsed_status['ok'] else fallback_details
        return clear_loop_with_entry(
            progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status='stopped',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
                reason='max_iterations',
            ),
            kind='terminal_stop',
            message=f'Ralph stopped after reaching max_iterations={max_iterations} without emitting {token}.',
        )

    if not parsed_status['ok']:
        error = parsed_status['error']
        return pause_loop_with_reason(
            state=claimed_state,
            iteration=iteration,
            session_id=event.session_id,
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
            iteration=iteration,
            session_id=event.session_id,
            message_fingerprint=message_fingerprint,
            details=progress_details_from_status(parsed_status),
            reason='missing_completion_token',
            message=(
                'Ralph paused because the assistant reported STATUS=complete without emitting '
                f'{token}. Emit the completion token only when the task is fully done, then resume with $continue-ralph-loop.'
            ),
        )

    if parsed_status['status'] == 'blocked':
        return pause_with_entry(
            claimed_state,
            progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status='blocked',
                summary=parsed_status['summary'],
                files=parsed_status['files'],
                checks=parsed_status['checks'],
                message_fingerprint=message_fingerprint,
                reason='awaiting_user_input',
            ),
            (
                'Ralph paused because the assistant reported STATUS=blocked and needs user input. '
                'Address the blocker, then resume with $continue-ralph-loop. '
                'If you want to discard this loop and start over, run $cancel-ralph before $ralph-loop.'
            ),
        )

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
        return pause_with_entry(
            next_state,
            progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status='stopped',
                summary=parsed_status['summary'],
                files=parsed_status['files'],
                checks=parsed_status['checks'],
                message_fingerprint=message_fingerprint,
                reason='repeated_response',
            ),
            (
                'Ralph paused after receiving the same assistant response three times in a row. '
                'Inspect .codex/ralph/progress.jsonl, then resume with $continue-ralph-loop. '
                'If you want to discard this loop and start over, run $cancel-ralph before $ralph-loop.'
            ),
        )

    next_state['phase'] = 'running'
    next_state['iteration'] = iteration + 1
    return RuntimeDecision(
        kind='continue',
        effects=(
            append_progress_effect(progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status=parsed_status['status'],
                summary=parsed_status['summary'],
                files=parsed_status['files'],
                checks=parsed_status['checks'],
                message_fingerprint=message_fingerprint,
            )),
            save_state_effect(next_state),
        ),
        response=block_response(continuation_prompt(state, next_iteration=next_state['iteration'])),
    )
