from __future__ import annotations

from ralph_core import effects, storage
from ralph_core.errors import StorageError
from ralph_core.model import LoopState, RuntimeDecision, StopEvent
from ralph_core.reducer import reduce_stop_event, stop_response

STOP_HOOK_LOCK_TIMEOUT_SECONDS = 25.0


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


def stop_decision(message: str) -> RuntimeDecision:
    return RuntimeDecision(kind='runtime_error', response=stop_response(message))


def state_read_decision(state_result: storage.StateReadResult) -> RuntimeDecision | None:
    if state_result.status == 'ok':
        return None
    if state_result.status == 'missing':
        return RuntimeDecision(kind='noop')
    if state_result.status == 'invalid_json':
        return stop_decision(
            'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json before continuing.'
        )
    if state_result.status == 'read_error':
        return stop_decision(storage_error_message('; '.join(state_result.errors)))
    if state_result.status == 'invalid_schema':
        return stop_decision(invalid_state_message(state_result.errors))
    return RuntimeDecision(kind='noop')


def state_needs_session_payload(state_result: storage.StateReadResult) -> bool:
    if state_result.status != 'ok':
        return False
    state = state_result.value
    if state is None:
        return False
    return state['phase'] == 'running'


def state_value_or_storage_error(state_result: storage.StateReadResult, cwd: str) -> LoopState:
    if state_result.value is None:
        raise StorageError(f'unable to read {storage.state_path(cwd)}: internal state read returned no payload')
    return state_result.value


def handle_stop_event(event: StopEvent) -> RuntimeDecision:
    try:
        with storage.workspace_lock(event.cwd, timeout_seconds=STOP_HOOK_LOCK_TIMEOUT_SECONDS):
            state_result = storage.read_state(event.cwd)
            state_decision = state_read_decision(state_result)
            if state_decision is not None:
                return state_decision

            state = state_value_or_storage_error(state_result, event.cwd)
            decision = reduce_stop_event(state, event)
            effects.apply_effects(decision.effects, event.cwd)
            return decision
    except StorageError as exc:
        return stop_decision(storage_error_message(str(exc)))
