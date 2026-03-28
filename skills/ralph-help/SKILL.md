---
name: ralph-help
description: Explain how the Codex Ralph package works, including installation, loop continuation, completion token behavior, storage paths, and related commands. Use when the user asks what Ralph is, how to set it up, or why the loop continues or stops.
---

# Codex Ralph Help

Explain Ralph succinctly, then tailor the rest to the user's question.

## Commands

- `$install-ralph` installs Ralph into the current user profile.
- `$uninstall-ralph` removes Ralph from the current user profile.
- `$ralph-loop` starts a workspace-local loop.
- `$continue-ralph-loop` manually resumes an existing workspace-local loop.
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
- User hook helpers live in `~/.codex/hooks/ralph/`.
- User skill symlinks live in `~/.agents/skills/`.
- The Stop hook continues unfinished turns until the assistant truthfully emits the completion token.

## Completion rule

The default Ralph loop only ends automatically when the assistant truthfully outputs:

```text
<promise>DONE</promise>
```
