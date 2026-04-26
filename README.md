# Codex Ralph

Direct Codex Ralph package built from six skills, one install script, one installed Stop-hook adapter,
host-agnostic runtime core, and package-local profile tooling.

By default it installs into:

- `CODEX_HOME=${CODEX_HOME:-$HOME/.codex}`
- `AGENTS_HOME=${AGENTS_HOME:-$HOME/.agents}`

## What is included

Installed into the user profile by `scripts/install_ralph.sh`:

- `skills/ralph-loop`
- `skills/uninstall-ralph`
- `skills/continue-ralph-loop`
- `skills/ralph-help`
- `skills/cancel-ralph`
- `skills/doctor-ralph`
- `hooks/stop_continue.py`
- `ralph_core/`
- Ralph `Stop` hook registration merged into `$CODEX_HOME/hooks.json`

Packaged for install/doctor support, but not copied into `$CODEX_HOME/hooks/ralph`:

- `profile/doctor.py`
- `profile/hook_registry.py`
- `profile/installer.py`
- `profile/package_manifest.py`
- `hooks/hooks.json` (packaged registry example)

## Official Codex model

OpenAI's Codex docs indicate two relevant surfaces here:

- Skills are directories with `SKILL.md`, discovered from `.agents/skills` and `~/.agents/skills`.
- Hooks are registered in `~/.codex/hooks.json` or `<repo>/.codex/hooks.json`.

This package deliberately keeps installation as a package script instead of an
`install-ralph` skill, because a skill cannot be used before it is installed.
`uninstall-ralph` remains an installed maintenance skill for removing an already
installed profile.

Runtime behavior is split cleanly:

- `$ralph-loop` starts a loop by writing workspace state.
- `$ralph-loop` refuses to overwrite an existing active or invalid state file; use `$continue-ralph-loop` to resume or `$cancel-ralph` before starting over.
- `$continue-ralph-loop` resumes an existing active loop explicitly and can reclaim a stale `phase="running"` state after a crash or restart, even if the prior session never persisted a claim.
- `$doctor-ralph` validates installation and workspace state.
- The `Stop` hook keeps unfinished `phase="running"` loops moving by default, consumes explicit state updates written by Ralph's packaged report script, pauses recoverable stops in-place, and clears state only on completion, cancellation, or the iteration cap.

## Python dependency

Ralph requires Python 3.10 or newer and uses only the Python standard library.

## Install

From the cloned package root:

```bash
bash ./scripts/install_ralph.sh
```

That install script:

- symlinks the six skills into `$AGENTS_HOME/skills`
- installs the Python hook helpers into `$CODEX_HOME/hooks/ralph`
- merges the Ralph `Stop` hook into `$CODEX_HOME/hooks.json`

Restart Codex after the first install so the skill list refreshes.

Run the same script again to repair or refresh an existing install.

To remove Ralph-managed skill links, copied hooks, and Stop-hook registration:

```text
$uninstall-ralph
```

The package uninstall script is also available when the uninstall skill is not
available or Codex has not been restarted:

```bash
bash ./skills/uninstall-ralph/scripts/uninstall_ralph.sh
```

## Packaging notes

- This package installs the six runtime and maintenance skills directly into `$AGENTS_HOME/skills`.
- `scripts/install_ralph.sh` is the install, reinstall, and repair entrypoint.
  Profile install/uninstall mutations are implemented in `profile/installer.py`.
- Workspace-local Ralph state changes are funneled through packaged scripts so the skills do not hand-write JSON blobs.
- Install and uninstall are transaction-safe for one caller, but they are not designed to be run concurrently. Do not run multiple install/uninstall commands in parallel against the same profile.

## Runtime files

- Active control state: `.codex/ralph/state.json`
- Append-only progress ledger: `.codex/ralph/progress.jsonl`
- State files must use `schema_version = 3`.
- Ralph validates state, progress, and hook registry files strictly. Malformed or old-schema files stop the loop or fail `$doctor-ralph`; they are never auto-repaired by silently filling defaults.

Recoverable stops keep `.codex/ralph/state.json` in place and set `phase` to `blocked` or `failed`.
Use `$continue-ralph-loop` to resume that paused loop explicitly.
That same command also reclaims a stale running state when Codex crashed or restarted mid-loop, including the window before a session claim was written.
If you want to abandon the current loop and start fresh, run `$cancel-ralph` before `$ralph-loop`.

Ralph does not read control state from assistant prose anymore.
If the task should keep going, reply normally and do not touch Ralph state.
If the turn should stop Ralph, first run:

```bash
bash "${AGENTS_HOME:-$HOME/.agents}/skills/ralph-loop/scripts/report_ralph.sh" \
  --status <progress|blocked|failed|complete> \
  --summary "single-line summary" \
  [--reason "required for blocked/failed"] \
  [--file path/to/file]... \
  [--check "passed:pytest -q"]...
```

The report script binds the update to the current Codex session using
`RALPH_SESSION_ID`, `CODEX_SESSION_ID`, or `CODEX_THREAD_ID`; pass
`--session-id` only when integrating another host that supplies the Stop hook
session id differently.

`max_iterations = N` means Ralph may emit up to `N` continuation prompts.
If the `N`th continued assistant turn still has no terminal report, the Stop hook records that turn and then stops the loop.

Use `$doctor-ralph` if skills, hooks, state, or progress files look wrong.
