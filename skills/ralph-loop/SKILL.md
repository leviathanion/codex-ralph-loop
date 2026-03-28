---
name: ralph-loop
description: Start an explicit Ralph loop for the current Codex workspace and rely on the Stop hook to continue unfinished work.
---

# Ralph Loop

Start a Codex Ralph loop in the current workspace.

## Required behavior

1. Create or overwrite `.codex/ralph/state.json` in the current workspace.
2. Write this JSON shape:

```json
{
  "active": true,
  "prompt": "<the user task>",
  "iteration": 0,
  "max_iterations": 100,
  "completion_token": "<promise>DONE</promise>",
  "claimed_session_id": null
}
```

3. Use the current user request as `prompt`.
4. Begin working on the task immediately.
5. Only output `<promise>DONE</promise>` when the task is fully and verifiably complete.

## Preconditions

- The Ralph hooks must already be installed. If they are missing, tell the user to run `$install-ralph`.
- If `$install-ralph` is not available yet, tell the user to bootstrap once with:

```bash
bash <codex-ralph-root>/skills/install-ralph/scripts/install_ralph.sh
```

- Use `$cancel-ralph` to stop the active loop.
