---
name: ralph-loop
description: Start or restart an explicit Ralph loop for the current workspace by writing `.codex/ralph/state.json` and relying on the Stop hook to continue unfinished work. Use when the user explicitly wants unattended multi-turn continuation for a repository task.
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
4. Begin working on the task immediately.
5. Every unfinished assistant response must end with exactly one status block as its final non-whitespace content:

```text
---RALPH_STATUS---
STATUS: progress|no_progress|blocked|complete
SUMMARY: <single-line summary, 200 chars max>
FILES: path/a, path/b
CHECKS: passed:npm test; failed:pytest -q
---END_RALPH_STATUS---
```

6. Only output `<promise>DONE</promise>` when the task is fully and verifiably complete.
   Put the token on the final non-whitespace line by itself.
   If you also include a `RALPH_STATUS` block before that token, it must report `STATUS: complete`.
   If an unfinished-turn status block is missing or malformed, Ralph will stop instead of silently continuing.
7. `FILES` is split on commas and `CHECKS` is split on semicolons. Do not put a literal comma inside one file item or a literal semicolon inside one check item; split or summarize instead.
8. Do not include the literal status markers inside `SUMMARY`, `FILES`, or `CHECKS`.

## Preconditions

- The Ralph hooks must already be installed. If they are missing, tell the user to run `$install-ralph`.
- If `$install-ralph` is not available yet, tell the user to bootstrap once with:

```bash
bash <codex-ralph-root>/skills/install-ralph/scripts/install_ralph.sh
```

- Use `$cancel-ralph` to stop the active loop.
- Use `$continue-ralph-loop` to resume an active loop manually.
- Use `$doctor-ralph` to diagnose installation or workspace state problems.
