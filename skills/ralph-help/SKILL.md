---
name: ralph-help
description: Explain how the Codex Ralph package works, including installation, loop continuation, completion token behavior, storage paths, and related commands. Use when the user asks what Ralph is, how to set it up, or why the loop continues or stops.
---

# Codex Ralph Help

Explain Ralph succinctly, then tailor the rest to the user's question.

## Commands

- `$install-ralph` installs Ralph into the current user profile.
- `$uninstall-ralph` removes Ralph-managed skill links, copied hooks, and Stop-hook registration from the current user profile.
- `$doctor-ralph` validates Ralph installation and workspace state.
- `$ralph-loop` starts a workspace-local loop.
- `$continue-ralph-loop` manually resumes an existing workspace-local loop, including stale `phase="running"` state left behind by a crash or restart.
- `$cancel-ralph` stops the current loop.
- `$ralph-help` explains setup and runtime behavior.

## Installation

Bootstrap from the cloned package root:

```bash
bash <codex-ralph-root>/skills/install-ralph/scripts/install_ralph.sh
```

`$install-ralph` can also install only skills or only hooks with `--skills-only` or `--hooks-only`.

## Runtime

- Workspace state lives in `.codex/ralph/state.json`.
- Workspace progress history lives in `.codex/ralph/progress.jsonl`.
- User hook helpers live in `~/.codex/hooks/ralph/`.
- User skill symlinks live in `~/.agents/skills/`.
- The Ralph start/resume/cancel skills use packaged scripts as the only write entrypoints for workspace state and progress files.
- Ralph validates state, progress, and hook-registry files strictly instead of filling defaults into malformed data.
- The Stop hook continues unfinished turns while `phase="running"`, pauses recoverable failures by leaving `state.json` in place with `phase="blocked"`, and clears state only on completion or the iteration cap.

## Completion rule

The default Ralph loop only ends automatically when the assistant truthfully outputs this token on the final non-whitespace line by itself:

```text
<promise>DONE</promise>
```

If a completed turn also includes a `RALPH_STATUS` block before that token, it must report `STATUS: complete`.

## Required unfinished-turn status block

Every unfinished Ralph turn must end with exactly one status block as its final non-whitespace content:

```text
---RALPH_STATUS---
STATUS: progress|no_progress|blocked|complete
SUMMARY: <non-empty single-line summary, 200 chars max>
FILES: path/a, path/b
CHECKS: passed:npm test; failed:pytest -q
---END_RALPH_STATUS---
```

If the status block is missing or malformed, Ralph will stop instead of silently continuing.
Use exactly those four fields and no extras.

`FILES` is parsed by commas and `CHECKS` is parsed by semicolons, so one item cannot contain those separators literally.
Do not include the literal status markers inside `SUMMARY`, `FILES`, or `CHECKS`.

`max_iterations = N` allows up to `N` continuation prompts. If the `N`th continued turn still lacks the completion token, Ralph records that turn and then stops.
