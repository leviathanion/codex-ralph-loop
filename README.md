# Codex Ralph

Direct Codex Ralph package built from six skills and one hook bundle.

By default it installs into:

- `CODEX_HOME=${CODEX_HOME:-$HOME/.codex}`
- `AGENTS_HOME=${AGENTS_HOME:-$HOME/.agents}`

## What is included

- `skills/ralph-loop`
- `skills/install-ralph`
- `skills/uninstall-ralph`
- `skills/continue-ralph-loop`
- `skills/ralph-help`
- `skills/cancel-ralph`
- `hooks/common.py`
- `hooks/stop_continue.py`
- `hooks/hooks.json`

## Official Codex model

OpenAI's Codex docs indicate two relevant surfaces here:

- Skills are directories with `SKILL.md`, discovered from `.agents/skills` and `~/.agents/skills`.
- Hooks are registered in `~/.codex/hooks.json` or `<repo>/.codex/hooks.json`.

This package therefore treats `install-ralph` and `uninstall-ralph` as skills with embedded scripts.

Runtime behavior is split cleanly:

- `$ralph-loop` starts a loop by writing workspace state.
- `$continue-ralph-loop` resumes an existing loop explicitly.
- The `Stop` hook keeps unfinished active loops moving until the completion token is emitted or the iteration cap is reached.

## Install directly

Use the install skill after the skills are available:

```text
$install-ralph
```

That skill tells Codex to run the embedded installer, which:

- symlinks the six skills into `$AGENTS_HOME/skills`
- installs the Python hook helpers into `$CODEX_HOME/hooks/ralph`
- merges the Ralph `Stop` hook into `$CODEX_HOME/hooks.json`
- ensures `codex_hooks = true` in `$CODEX_HOME/config.toml`

To remove everything Ralph installed:

```text
$uninstall-ralph
```

## Bootstrap without the skill

If the install skill is not available yet, bootstrap once from the package root:

```bash
bash ./skills/install-ralph/scripts/install_ralph.sh
```

After that, restart Codex and use `$install-ralph` or `$uninstall-ralph`.

## Packaging notes

- This package installs the six skills directly into `$AGENTS_HOME/skills`.
- The canonical installer logic lives inside the skill-local `scripts/` directories.
