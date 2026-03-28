---
name: continue-ralph-loop
description: Resume an existing Ralph loop in the current workspace from `.codex/ralph/state.json`. Use when the user wants to continue, resume, retry, or recover an unfinished Ralph loop after a previous turn stopped early.
---

# Continue Ralph Loop

Continue the active Codex Ralph loop in the current workspace.

## Required behavior

1. Read `.codex/ralph/state.json` from the current workspace.
2. If the file does not exist, tell the user there is no active Ralph loop to continue and suggest `$ralph-loop`.
3. If the state exists but `active` is not `true`, tell the user there is no active Ralph loop to continue.
4. If the state is invalid JSON, tell the user to run `$cancel-ralph` or repair `.codex/ralph/state.json`.
5. Before continuing, update the state file so `claimed_session_id` is `null` while preserving the rest of the active loop state.
6. Continue the stored task using:
   - the current repository state
   - the stored `prompt`
   - the stored `iteration`, `max_iterations`, and `completion_token`
7. Only finish the loop when the task is fully and verifiably complete and the stored completion token is truthfully appropriate.

## Notes

- The Ralph `Stop` hook must already be installed. If it is missing, tell the user to run `$install-ralph`.
- Use `$cancel-ralph` to stop the active loop.
