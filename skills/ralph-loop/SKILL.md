---
name: ralph-loop
description: Start an explicit Ralph loop for the current workspace by writing `.codex/ralph/state.json` and relying on the Stop hook to continue unfinished work. Use when the user explicitly wants unattended multi-turn continuation for a repository task.
---

# Ralph Loop

Start a Codex Ralph loop in the current workspace.

## Required behavior

1. Do not hand-edit `.codex/ralph/state.json` or `.codex/ralph/progress.jsonl`.
2. Pipe the current user request into the packaged start script:

```bash
bash "${AGENTS_HOME:-$HOME/.agents}/skills/ralph-loop/scripts/start_ralph.sh" <<'EOF'
<current user request>
EOF
```

3. If the script fails, surface the error to the user instead of inventing or partially repairing Ralph state by hand.
   If it reports an existing active loop, tell the user to use `$continue-ralph-loop` or `$cancel-ralph` before starting a new loop.
4. Begin working on the task immediately.
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

7. `--summary` must be a truthful single-line summary. Use repeatable `--file` and `--check` flags for changed files and verification evidence.
8. `--reason` is required for `blocked` and `failed`.
9. Use `complete` only when the task is fully and verifiably complete.
10. If you do not report a terminal status, Ralph will continue automatically after the Stop hook fires.

## Preconditions

- The Ralph hooks must already be installed. If they are missing, tell the user to run the install script from the cloned Ralph package:

```bash
bash <codex-ralph-root>/scripts/install_ralph.sh
```

- Use `$cancel-ralph` to stop the active loop.
- `$ralph-loop` does not replace an active or invalid state file in place; cancel or repair first.
- Use `$continue-ralph-loop` to resume an active loop manually.
- Use `$doctor-ralph` to diagnose installation or workspace state problems.
