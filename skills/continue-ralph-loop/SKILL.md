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
   - `missing`: tell the user there is no active Ralph loop to continue and suggest `$ralph-loop`.
   - `invalid_json` or `invalid_schema`: tell the user to run `$cancel-ralph` or repair `.codex/ralph/state.json`.
   - `resumed`: continue using the returned `prompt`, `iteration`, and `max_iterations`.
4. If the script fails, surface the storage error instead of patching the files manually.
5. If the task should keep going, reply normally and do not hand-edit Ralph state.
6. If the turn must stop Ralph, run the packaged report script before your final response:

```bash
bash "${AGENTS_HOME:-$HOME/.agents}/skills/ralph-loop/scripts/report_ralph.sh" \
  --status <progress|blocked|failed|complete> \
  --summary "single-line summary" \
  [--reason "required for blocked/failed"] \
  [--file path/to/file]... \
  [--check "passed:pytest -q"]...
```

7. `--summary` must be truthful and single-line. Use repeatable `--file` and `--check` flags for changed files and verification evidence.
8. `--reason` is required for `blocked` and `failed`.
9. Use `complete` only when the task is fully and verifiably complete.
10. If you do not report a terminal status, Ralph will continue automatically after the Stop hook fires.

## Notes

- The Ralph `Stop` hook must already be installed. If it is missing, tell the user to run `bash <codex-ralph-root>/scripts/install_ralph.sh` from the cloned Ralph package.
- Use `$cancel-ralph` to stop the active loop.
- Use `$doctor-ralph` to diagnose installation or workspace state problems.
- Recoverable Stop-hook failures leave the loop paused in `.codex/ralph/state.json` until this skill resumes it.
- If Codex crashed or restarted while a loop was still marked `phase="running"`, this skill can reclaim that stale state so a new session can continue, even if no `claimed_session_id` was persisted yet.
