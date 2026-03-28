---
name: ralph-help
description: Explain how the Codex Ralph package is installed and how its loop works.
---

# Codex Ralph Help

## Skills

- `$install-ralph` installs Ralph into the current user profile.
- `$uninstall-ralph` removes Ralph from the current user profile.
- `$ralph-loop` starts a workspace-local loop.
- `$continue-ralph-loop` manually resumes an existing workspace-local loop.
- `$cancel-ralph` stops the current loop.
- `$ralph-help` explains setup and runtime behavior.

## Bootstrap installer

Bootstrap from the cloned package root:

```bash
bash <codex-ralph-root>/skills/install-ralph/scripts/install_ralph.sh
```

## Completion rule

A Ralph loop only ends automatically when the assistant truthfully outputs:

```text
<promise>DONE</promise>
```

## Storage

- Workspace state: `.codex/ralph/state.json`
- User hook helpers: `~/.codex/hooks/ralph/`
- User skill symlinks: `~/.agents/skills/`
