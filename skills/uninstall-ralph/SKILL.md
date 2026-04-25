---
name: uninstall-ralph
description: Uninstall Codex Ralph from the current user profile by removing Ralph skill links and copied hooks. Use when the user wants to remove, disable, or clean up the Ralph package from this machine.
---

# Uninstall Ralph

Uninstall Codex Ralph from this user profile.

This removes Ralph-managed skill links, copied hooks, and Stop-hook registration.

## Required behavior

1. Run `bash scripts/uninstall_ralph.sh` from this skill directory.
2. Support the script flags `--skills-only` and `--hooks-only` when the user requests partial removal.
3. If the user passed supported arguments, append them exactly.
4. Do not describe the command without running it.
5. After the command finishes, summarize what was removed.
6. If there is nothing to uninstall, say so plainly.
7. Tell the user that restarting Codex may be required for the skill list to refresh.
8. Do not run multiple install/uninstall commands in parallel against the same user profile; that concurrency is out of scope for Ralph's installer.
