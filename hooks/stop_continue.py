from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _add_runtime_import_root() -> None:
    current_dir = Path(__file__).resolve().parent
    candidates = (
        current_dir,
        current_dir.parent,
    )
    for candidate in candidates:
        if (candidate / 'ralph_core').is_dir():
            sys.path.insert(0, str(candidate))
            return


_add_runtime_import_root()

from ralph_core import runtime, storage  # noqa: E402
from ralph_core.model import RuntimeDecision, StopEvent  # noqa: E402


def render_response(decision: RuntimeDecision) -> str:
    if decision.response is None:
        return ''
    return json.dumps(decision.response)


def emit_decision(decision: RuntimeDecision) -> None:
    sys.stdout.write(render_response(decision))


def invalid_payload_decision(error: str) -> RuntimeDecision:
    return runtime.stop_decision(runtime.invalid_payload_message(error))


def validate_payload_cwd(payload: Any) -> tuple[dict[str, Any], str] | RuntimeDecision:
    if not isinstance(payload, dict):
        return invalid_payload_decision('payload must be a JSON object')

    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not cwd:
        return invalid_payload_decision('cwd must be a non-empty string')

    return payload, cwd


def validate_payload_event(payload: dict[str, Any], cwd: str) -> StopEvent | RuntimeDecision:
    session_id = payload.get('session_id')
    # Trade-off: a running Ralph loop cannot distinguish a retry from a stale concurrent hook
    # without a non-empty session claim, so running state fails closed on missing session_id.
    if not isinstance(session_id, str) or not session_id:
        return invalid_payload_decision('session_id must be a non-empty string')

    last_assistant_message = payload.get('last_assistant_message')
    if last_assistant_message is None:
        normalized_message = ''
    elif isinstance(last_assistant_message, str):
        normalized_message = last_assistant_message
    else:
        return invalid_payload_decision('last_assistant_message must be a string or null')

    return StopEvent(
        cwd=cwd,
        session_id=session_id,
        last_assistant_message=normalized_message,
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        emit_decision(invalid_payload_decision(f'invalid JSON payload: {exc.msg}'))
        return 0

    validated_cwd = validate_payload_cwd(payload)
    if isinstance(validated_cwd, RuntimeDecision):
        emit_decision(validated_cwd)
        return 0

    payload, cwd = validated_cwd
    workspace_error = storage.workspace_root_error(storage.workspace_path(cwd))
    if workspace_error is not None:
        emit_decision(runtime.stop_decision(runtime.storage_error_message(workspace_error)))
        return 0

    # Missing Ralph state is the global-hook fast path: no lock, mkdir, session validation, or writes.
    initial_state_result = storage.read_state(cwd)
    initial_state_decision = runtime.state_read_decision(initial_state_result)
    if initial_state_decision is not None:
        emit_decision(initial_state_decision)
        return 0
    if not runtime.state_needs_session_payload(initial_state_result):
        return 0

    event = validate_payload_event(payload, cwd)
    if isinstance(event, RuntimeDecision):
        emit_decision(event)
        return 0

    emit_decision(runtime.handle_stop_event(event))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
