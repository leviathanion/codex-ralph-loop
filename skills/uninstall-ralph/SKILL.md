---
name: uninstall-ralph
description: Uninstall Codex Ralph from this user profile.
---

# Uninstall Ralph

Uninstall Codex Ralph from this user profile.

## Required behavior

1. Run `bash scripts/uninstall_ralph.sh` from this skill directory.
2. If the user passed arguments, append them exactly.
3. Do not describe the command without running it.
4. After the command finishes, summarize what was removed.
5. If there is nothing to uninstall, say so plainly.
6. Tell the user that restarting Codex may be required for the skill list to refresh.
