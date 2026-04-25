from __future__ import annotations

from ralph_core.model import LoopState


def continuation_prompt(state: LoopState, *, next_iteration: int) -> str:
    prompt = state['prompt'].strip() or '(missing prompt)'
    max_iterations = state['max_iterations']
    token = state['completion_token']
    return (
        f"[RALPH LOOP {next_iteration}/{max_iterations}]\n\n"
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

