---
name: install-ralph
description: Install Codex Ralph into the current user profile by linking Ralph skills into `~/.agents/skills/`, copying hooks into `~/.codex/hooks/ralph/`, and enabling `codex_hooks`. Use when the user asks to install, bootstrap, relink, or repair the Ralph package.
---

# Install Ralph

Install Codex Ralph into this user profile.

## Required behavior

1. Run `bash scripts/install_ralph.sh` from this skill directory.
2. Support the script flags `--skills-only` and `--hooks-only` when the user requests partial installation.
3. If the user passed supported arguments, append them exactly.
4. Do not describe the command without running it.
5. After the command finishes, summarize what changed.
6. If the command reports that Ralph is already installed, say so plainly.
7. If skills do not appear immediately, tell the user to restart Codex.
