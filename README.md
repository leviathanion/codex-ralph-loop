# Codex Ralph

Direct Codex Ralph package built from seven skills, three installed runtime hooks, and package-local installer helpers.

By default it installs into:

- `CODEX_HOME=${CODEX_HOME:-$HOME/.codex}`
- `AGENTS_HOME=${AGENTS_HOME:-$HOME/.agents}`

## What is included

Installed directly by `$install-ralph`:

- `skills/ralph-loop`
- `skills/install-ralph`
- `skills/uninstall-ralph`
- `skills/continue-ralph-loop`
- `skills/ralph-help`
- `skills/cancel-ralph`
- `skills/doctor-ralph`
- `hooks/common.py`
- `hooks/state_store.py`
- `hooks/stop_continue.py`
- Ralph `Stop` hook registration merged into `$CODEX_HOME/hooks.json`

Packaged under `hooks/` for install/doctor support, but not copied into `$CODEX_HOME/hooks/ralph`:

- `hooks/doctor.py`
- `hooks/hook_registry.py`
- `hooks/loop_control.py`
- `hooks/package_manifest.py`
- `hooks/profile_installer.py`
- `hooks/toml_feature_flag.py`
- `hooks/hooks.json` (packaged registry example)

## Official Codex model

OpenAI's Codex docs indicate two relevant surfaces here:

- Skills are directories with `SKILL.md`, discovered from `.agents/skills` and `~/.agents/skills`.
- Hooks are registered in `~/.codex/hooks.json` or `<repo>/.codex/hooks.json`.

This package therefore treats `install-ralph` and `uninstall-ralph` as skills with embedded scripts.

Runtime behavior is split cleanly:

- `$ralph-loop` starts a loop by writing workspace state.
- `$continue-ralph-loop` resumes an existing active loop explicitly and can reclaim a stale `phase="running"` state after a crash or restart, even if the prior session never persisted a claim.
- `$doctor-ralph` validates installation and workspace state.
- The `Stop` hook keeps unfinished `phase="running"` loops moving until the completion token is emitted on the final non-whitespace line by itself, pauses recoverable stops in-place, and clears state only on completion or the iteration cap.

## Install directly

Use the install skill after the skills are available:

```text
$install-ralph
```

That skill tells Codex to run the embedded installer, which:

- symlinks the seven skills into `$AGENTS_HOME/skills`
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

- This package installs the seven skills directly into `$AGENTS_HOME/skills`.
- The canonical installer logic lives inside the skill-local `scripts/` directories.
- Workspace-local Ralph state changes are funneled through packaged scripts so the skills do not hand-write JSON blobs.

## Runtime files

- Active control state: `.codex/ralph/state.json`
- Append-only progress ledger: `.codex/ralph/progress.jsonl`
- Ralph validates state, progress, and hook registry files strictly. Malformed files stop the loop or fail `$doctor-ralph`; they are never auto-repaired by silently filling defaults.

Recoverable stops keep `.codex/ralph/state.json` in place and set `phase` to `blocked`.
Use `$continue-ralph-loop` to resume that paused loop explicitly.
That same command also reclaims a stale running state when Codex crashed or restarted mid-loop, including the window before a session claim was written.

A completed Ralph turn must place `<promise>DONE</promise>` on the final non-whitespace line by itself.
If a completed turn also includes a `RALPH_STATUS` block before that token, it must report `STATUS: complete`.

Every unfinished Ralph turn must end with exactly one status block as its final non-whitespace content:

```text
---RALPH_STATUS---
STATUS: progress|no_progress|blocked|complete
SUMMARY: <single-line summary, 200 chars max>
FILES: path/a, path/b
CHECKS: passed:npm test; failed:pytest -q
---END_RALPH_STATUS---
```

If the block is missing or malformed, Ralph stops instead of silently continuing.

`FILES` is parsed by splitting on commas and `CHECKS` is parsed by splitting on semicolons.
Do not put a literal comma inside one `FILES` item or a literal semicolon inside one `CHECKS` item.
Do not include the literal status markers inside `SUMMARY`, `FILES`, or `CHECKS`.

`max_iterations = N` means Ralph may emit up to `N` continuation prompts.
If the `N`th continued assistant turn still does not emit the completion token, the Stop hook records that turn and then stops the loop.

Use `$doctor-ralph` if skills, hooks, state, or progress files look wrong.
