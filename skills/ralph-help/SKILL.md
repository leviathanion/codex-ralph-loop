---
name: ralph-help
description: Explain how the Codex Ralph package works, including installation, loop continuation, report-driven state updates, storage paths, and related commands. Use when the user asks what Ralph is, how to set it up, or why the loop continues or stops.
---

# Codex Ralph Help

Explain Ralph succinctly, then tailor the rest to the user's question.

## Commands

- `$uninstall-ralph` removes Ralph-managed skill links, copied hooks, and Stop-hook registration from the current user profile.
- `$doctor-ralph` validates Ralph installation and workspace state.
- `$ralph-loop` starts a workspace-local loop.
- `$continue-ralph-loop` manually resumes an existing workspace-local loop, including stale `phase="running"` state left behind by a crash or restart.
- `$cancel-ralph` stops the current loop.
- `$ralph-help` explains setup and runtime behavior.

## Installation

Install from the cloned package root:

```bash
bash <codex-ralph-root>/scripts/install_ralph.sh
```

Run the same script again to repair or refresh an existing install.
The script can install only skills or only hooks with `--skills-only` or `--hooks-only`.

## Runtime

- Workspace state lives in `.codex/ralph/state.json`.
- Workspace progress history lives in `.codex/ralph/progress.jsonl`.
- User hook helpers live in `~/.codex/hooks/ralph/`.
- User skill symlinks live in `~/.agents/skills/`.
- The Ralph start/resume/cancel skills use packaged scripts as the only write entrypoints for workspace state and progress files.
- Ralph validates state, progress, and hook-registry files strictly instead of filling defaults into malformed data.
- The Stop hook continues unfinished turns while `phase="running"`, consumes explicit report updates from Ralph's packaged report script, pauses recoverable failures by leaving `state.json` in place with `phase="blocked"` or `phase="failed"`, and clears state only on completion or the iteration cap.

## Report-driven stop rule

Ralph does not read control state from assistant prose.
If the task should continue, the assistant replies normally and leaves Ralph state alone.
If the turn should stop Ralph, the assistant must run:

```bash
bash "${AGENTS_HOME:-$HOME/.agents}/skills/ralph-loop/scripts/report_ralph.sh" \
  --status <progress|blocked|failed|complete> \
  --summary "single-line summary" \
  [--reason "required for blocked/failed"] \
  [--file path/to/file]... \
  [--check "passed:pytest -q"]...
```

Use `complete` only when the task is fully done and verified.
Use `blocked` or `failed` only with a truthful `--reason`.
If the assistant does not report a terminal status, Ralph continues automatically.

`max_iterations = N` allows up to `N` continuation prompts. If the `N`th continued assistant turn still lacks a terminal report, Ralph records that turn and then stops.
