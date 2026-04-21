---
name: continue-ralph-loop
description: Resume an existing paused Ralph loop in the current workspace from `.codex/ralph/state.json`. Use when the user wants to continue, resume, retry, or recover an unfinished Ralph loop after a previous turn stopped early.
---

# Continue Ralph Loop

Continue the active Codex Ralph loop in the current workspace.

## Required behavior

1. Do not hand-edit `.codex/ralph/state.json` or `.codex/ralph/progress.jsonl`.
2. Run the packaged resume script:

```bash
bash "${AGENTS_HOME:-$HOME/.agents}/skills/continue-ralph-loop/scripts/continue_ralph.sh"
```

3. Read the JSON it prints and branch on `status`:
   - `missing` or `inactive`: tell the user there is no active Ralph loop to continue and suggest `$ralph-loop`.
   - `invalid_json` or `invalid_schema`: tell the user to run `$cancel-ralph` or repair `.codex/ralph/state.json`.
   - `resumed`: continue using the returned `prompt`, `iteration`, `max_iterations`, and `completion_token`.
4. If the script fails, surface the storage error instead of patching the files manually.
5. Every unfinished assistant response must end with exactly one status block as its final non-whitespace content:

```text
---RALPH_STATUS---
STATUS: progress|no_progress|blocked|complete
SUMMARY: <single-line summary, 200 chars max>
FILES: path/a, path/b
CHECKS: passed:npm test; failed:pytest -q
---END_RALPH_STATUS---
```

6. Only finish the loop when the task is fully and verifiably complete and the stored completion token is truthfully appropriate.
   Put the completion token on the final non-whitespace line by itself.
   If you also include a `RALPH_STATUS` block before that token, it must report `STATUS: complete`.
   If an unfinished-turn status block is missing or malformed, Ralph will stop instead of silently continuing.
7. `FILES` is split on commas and `CHECKS` is split on semicolons. Do not put a literal comma inside one file item or a literal semicolon inside one check item; split or summarize instead.
8. Do not include the literal status markers inside `SUMMARY`, `FILES`, or `CHECKS`.

## Notes

- The Ralph `Stop` hook must already be installed. If it is missing, tell the user to run `$install-ralph`.
- Use `$cancel-ralph` to stop the active loop.
- Use `$doctor-ralph` to diagnose installation or workspace state problems.
- Recoverable Stop-hook failures leave the loop paused in `.codex/ralph/state.json` until this skill resumes it.
- If Codex crashed or restarted while a loop was still marked `phase="running"`, this skill can reclaim that stale state so a new session can continue, even if no `claimed_session_id` was persisted yet.
