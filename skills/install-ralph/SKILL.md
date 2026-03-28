---
name: install-ralph
description: Install Codex Ralph skills and hooks into this user profile.
---

# Install Ralph

Install Codex Ralph into this user profile.

## Required behavior

1. Run `bash scripts/install_ralph.sh` from this skill directory.
2. If the user passed arguments, append them exactly.
3. Do not describe the command without running it.
4. After the command finishes, summarize what changed.
5. If the command reports that Ralph is already installed, say so plainly.
6. If skills do not appear immediately, tell the user to restart Codex.
