---
name: cancel-ralph
description: Stop or cancel the active Ralph loop in the current workspace by clearing `.codex/ralph/state.json`. Use when the user wants to stop, abort, reset, or clear a stuck Ralph loop before it continues again.
---

# Cancel Ralph

Stop the active Ralph loop for the current workspace.

## Required behavior

1. Remove `.codex/ralph/state.json` if it exists, even if the file contents are invalid.
2. Leave installed user-level skills and hooks untouched.
3. Tell the user whether loop state was cleared or no loop state was present.
