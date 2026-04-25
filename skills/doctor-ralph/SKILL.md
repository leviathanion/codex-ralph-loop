---
name: doctor-ralph
description: Validate the local Codex Ralph installation and the current workspace Ralph files. Use when the user wants to diagnose broken hooks, missing skills, invalid state, or a damaged progress ledger.
---

# Doctor Ralph

Run the Codex Ralph doctor for the current user profile and workspace.

## Required behavior

1. Run this skill's `scripts/doctor_ralph.sh` and pass the user's target workspace path as the first argument. Do not inspect this skill directory unless that is the workspace the user actually asked about.
2. Do not describe the command without running it.
3. After the command finishes, summarize any `[FAIL]` findings first.
4. If all checks pass, say that plainly.
5. Mention that the doctor validates:
   - installed skill links
   - installed hook files
   - Stop hook registration
   - workspace `.codex/ralph/` write access
   - `state.json` and `progress.jsonl` validity when present
