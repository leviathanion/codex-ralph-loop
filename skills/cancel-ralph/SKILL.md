---
name: cancel-ralph
description: Stop or cancel the active Ralph loop in the current workspace by clearing `.codex/ralph/state.json`. Use when the user wants to stop, abort, reset, or clear a stuck Ralph loop before it continues again.
---

# Cancel Ralph

Stop the active Ralph loop for the current workspace.

## Required behavior

1. Do not hand-edit `.codex/ralph/state.json` or `.codex/ralph/progress.jsonl`.
2. Run the packaged cancel script:

```bash
bash "${AGENTS_HOME:-$HOME/.agents}/skills/cancel-ralph/scripts/cancel_ralph.sh"
```

3. Read the JSON it prints and report the outcome:
   - `missing`: tell the user no Ralph loop state was present.
   - `cleared`: tell the user the loop state was cleared and a cancellation ledger row was appended.
   - `cleared_invalid_state`: tell the user the loop state was cleared but no ledger row was appended because the state file was invalid.
4. If the script fails, surface the storage error instead of inventing or manually rewriting Ralph files.
5. Leave installed user-level skills and hooks untouched.
