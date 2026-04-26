from __future__ import annotations

from ralph_core.model import LoopState


def continuation_prompt(state: LoopState, *, next_iteration: int) -> str:
    prompt = state['prompt'].strip() or '(missing prompt)'
    max_iterations = state['max_iterations']
    return (
        f"[RALPH LOOP {next_iteration}/{max_iterations}]\n\n"
        'Continue the task from the current repository state.\n'
        'Ralph no longer reads control state from assistant prose.\n'
        'If the task should continue, reply normally and do not touch Ralph state.\n'
        'If the turn must stop Ralph, first run:\n'
        'bash "${AGENTS_HOME:-$HOME/.agents}/skills/ralph-loop/scripts/report_ralph.sh" '
        '--status <progress|blocked|failed|complete> --summary "<single-line summary>"\n'
        'Use repeatable `--file` and `--check` flags for changed paths and verification evidence.\n'
        'For `blocked` and `failed`, you must also provide `--reason`.\n'
        'Use `complete` only when the task is fully done and verified.\n'
        'If you do not report a terminal status, Ralph will continue automatically.\n\n'
        f'Original task:\n{prompt}'
    )
