from __future__ import annotations

import json
import sys

from common import clear_state, completion_token, load_state, save_state


def main() -> int:
    payload = json.load(sys.stdin)
    cwd = payload.get('cwd')
    session_id = payload.get('session_id')
    state = load_state(cwd)

    if state.get('parse_error'):
        sys.stdout.write(json.dumps({
            'continue': False,
            'systemMessage': 'Ralph state is invalid JSON. Run $cancel-ralph or repair .codex/ralph/state.json before continuing.'
        }))
        return 0

    if not state.get('active'):
        return 0

    claimed_session_id = state.get('claimed_session_id')
    if claimed_session_id and session_id and claimed_session_id != session_id:
        return 0

    if not claimed_session_id and session_id:
        state['claimed_session_id'] = session_id

    token = completion_token(state)
    last_assistant_message = payload.get('last_assistant_message') or ''
    if token in last_assistant_message:
        clear_state(cwd)
        return 0

    iteration = int(state.get('iteration', 0))
    max_iterations = int(state.get('max_iterations', 100) or 100)
    if iteration >= max_iterations:
        clear_state(cwd)
        sys.stdout.write(json.dumps({
            'continue': False,
            'systemMessage': f'Ralph stopped after reaching max_iterations={max_iterations} without emitting {token}.'
        }))
        return 0

    state['iteration'] = iteration + 1
    save_state(state, cwd)

    prompt = (state.get('prompt') or '').strip() or '(missing prompt)'
    message = (
        f"[RALPH LOOP {state['iteration']}/{max_iterations}]\n\n"
        'The previous assistant turn stopped without the completion token. '
        'Continue the task from the current repository state. '
        f'Only finish the loop when you can truthfully emit {token}.\n\n'
        f'Original task:\n{prompt}'
    )
    sys.stdout.write(json.dumps({
        'decision': 'block',
        'reason': message
    }))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
