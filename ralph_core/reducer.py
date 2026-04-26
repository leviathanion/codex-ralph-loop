from __future__ import annotations

import json

from ralph_core.model import (
    DecisionKind,
    LoopState,
    PendingUpdate,
    ProgressDetails,
    ProgressEntry,
    RuntimeDecision,
    RuntimeEffect,
    StopEvent,
    now_iso,
)
from ralph_core.prompts import continuation_prompt
from ralph_core.protocol import fingerprint_message, truncate_summary


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


def progress_details_from_update(update: PendingUpdate) -> ProgressDetails:
    return {
        'summary': update['summary'],
        'files': update['files'],
        'checks': update['checks'],
    }


def repeat_fingerprint_for_turn(message_fingerprint: str, update: PendingUpdate | None) -> str:
    if update is None:
        return message_fingerprint
    payload = {
        'message_fingerprint': message_fingerprint,
        'status': update['status'],
        'summary': update['summary'],
        'files': update['files'],
        'checks': update['checks'],
        'reason': update['reason'],
        'session_id': update['session_id'],
    }
    # `updated_at` is deliberately excluded: a freshly written but semantically identical
    # report should still trip the repeated-response circuit after three turns.
    return fingerprint_message(json.dumps(payload, sort_keys=True, separators=(',', ':')))


def save_state_effect(state: LoopState) -> RuntimeEffect:
    return RuntimeEffect(kind='save_state', state=state)


def append_progress_effect(entry: ProgressEntry) -> RuntimeEffect:
    return RuntimeEffect(kind='append_progress', progress=entry)


def clear_state_effect() -> RuntimeEffect:
    return RuntimeEffect(kind='clear_state')


def paused_state(
    state: LoopState,
    *,
    phase: str,
    last_status: PendingUpdate | None,
    message_fingerprint: str,
    repeat_count: int,
) -> LoopState:
    next_state = dict(state)
    next_state['phase'] = phase
    next_state['pending_update'] = None
    if last_status is None:
        next_state['last_status'] = None
    else:
        next_state['last_status'] = {
            'status': last_status['status'],
            'summary': last_status['summary'],
            'files': list(last_status['files']),
            'checks': list(last_status['checks']),
            'reason': last_status['reason'],
            'updated_at': last_status['updated_at'],
        }
    next_state['updated_at'] = now_iso()
    next_state['last_message_fingerprint'] = message_fingerprint
    next_state['repeat_count'] = repeat_count
    return next_state


def continued_state(
    state: LoopState,
    *,
    message_fingerprint: str,
    repeat_count: int,
) -> LoopState:
    next_state = dict(state)
    next_state['phase'] = 'running'
    next_state['pending_update'] = None
    next_state['updated_at'] = now_iso()
    next_state['iteration'] = state['iteration'] + 1
    next_state['last_message_fingerprint'] = message_fingerprint
    next_state['repeat_count'] = repeat_count
    return next_state


def pause_with_entry(
    state: LoopState,
    entry: ProgressEntry,
    message: str,
    *,
    phase: str,
    last_status: PendingUpdate | None,
    message_fingerprint: str,
    repeat_count: int,
) -> RuntimeDecision:
    return RuntimeDecision(
        kind='pause',
        effects=(
            save_state_effect(paused_state(
                state,
                phase=phase,
                last_status=last_status,
                message_fingerprint=message_fingerprint,
                repeat_count=repeat_count,
            )),
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
    if state['phase'] != 'running':
        return RuntimeDecision(kind='noop')

    claimed_session_id = state['claimed_session_id']
    if claimed_session_id is not None and event.session_id != claimed_session_id:
        return RuntimeDecision(kind='noop')

    claimed_state = dict(state)
    if claimed_session_id is None:
        claimed_state['claimed_session_id'] = event.session_id

    last_assistant_message = event.last_assistant_message
    fallback_details = fallback_progress_details(last_assistant_message)
    message_fingerprint = fingerprint_message(last_assistant_message)
    iteration = state['iteration']
    max_iterations = state['max_iterations']
    pending_update = state['pending_update']
    if pending_update is not None and pending_update['iteration'] != iteration:
        details = fallback_details
        return pause_with_entry(
            claimed_state,
            progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status='stopped',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
                reason='stale_pending_update',
            ),
            (
                'Ralph paused because the persisted status update does not match the current iteration. '
                'Inspect .codex/ralph/state.json, then resume with $continue-ralph-loop or discard with $cancel-ralph.'
            ),
            phase='blocked',
            last_status=None,
            message_fingerprint=message_fingerprint,
            repeat_count=0,
        )

    if pending_update is not None:
        details = progress_details_from_update(pending_update)
        if pending_update['status'] == 'complete':
            return clear_loop_with_entry(
                progress_entry(
                    iteration=iteration,
                    session_id=event.session_id,
                    status='complete',
                    summary=details['summary'],
                    files=details['files'],
                    checks=details['checks'],
                    message_fingerprint=message_fingerprint,
                    reason=pending_update['reason'],
                ),
                kind='complete',
            )

        if pending_update['status'] == 'blocked':
            return pause_with_entry(
                claimed_state,
                progress_entry(
                    iteration=iteration,
                    session_id=event.session_id,
                    status='blocked',
                    summary=details['summary'],
                    files=details['files'],
                    checks=details['checks'],
                    message_fingerprint=message_fingerprint,
                    reason=pending_update['reason'],
                ),
                (
                    'Ralph paused because the assistant reported a blocking dependency. '
                    'Resolve the blocker, then resume with $continue-ralph-loop.'
                ),
                phase='blocked',
                last_status=pending_update,
                message_fingerprint=message_fingerprint,
                repeat_count=0,
            )

        if pending_update['status'] == 'failed':
            return pause_with_entry(
                claimed_state,
                progress_entry(
                    iteration=iteration,
                    session_id=event.session_id,
                    status='failed',
                    summary=details['summary'],
                    files=details['files'],
                    checks=details['checks'],
                    message_fingerprint=message_fingerprint,
                    reason=pending_update['reason'],
                ),
                (
                    'Ralph paused because the assistant reported a failed verification or unrecoverable issue. '
                    'Address the failure, then resume with $continue-ralph-loop.'
                ),
                phase='failed',
                last_status=pending_update,
                message_fingerprint=message_fingerprint,
                repeat_count=0,
            )

    repeat_fingerprint = repeat_fingerprint_for_turn(message_fingerprint, pending_update)
    repeat_count = state['repeat_count'] + 1 if state['last_message_fingerprint'] == repeat_fingerprint else 1
    details = progress_details_from_update(pending_update) if pending_update is not None else fallback_details

    if repeat_count >= 3:
        return pause_with_entry(
            claimed_state,
            progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status='stopped',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
                reason='repeated_response',
            ),
            (
                'Ralph paused after receiving the same assistant response three times in a row. '
                'Inspect .codex/ralph/progress.jsonl, then resume with $continue-ralph-loop.'
            ),
            phase='blocked',
            last_status=None,
            message_fingerprint=repeat_fingerprint,
            repeat_count=repeat_count,
        )

    if iteration >= max_iterations:
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
            message=f'Ralph stopped after reaching max_iterations={max_iterations}.',
        )

    return RuntimeDecision(
        kind='continue',
        effects=(
            append_progress_effect(progress_entry(
                iteration=iteration,
                session_id=event.session_id,
                status='progress',
                summary=details['summary'],
                files=details['files'],
                checks=details['checks'],
                message_fingerprint=message_fingerprint,
                reason=pending_update['reason'] if pending_update is not None else None,
            )),
            save_state_effect(continued_state(
                claimed_state,
                message_fingerprint=repeat_fingerprint,
                repeat_count=repeat_count,
            )),
        ),
        response=block_response(continuation_prompt(state, next_iteration=state['iteration'] + 1)),
    )
