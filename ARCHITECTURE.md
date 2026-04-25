# Codex Ralph Architecture

This document defines the proposed architecture for Codex Ralph.

## Goals

- Use the Codex `Stop` hook as the primary control point for Ralph loop
  continuation.
- Keep the hook precise and lightweight: adapt Codex events, call Ralph runtime
  logic, and render Codex hook output.
- Keep non-Ralph workspaces side-effect-free. If no Ralph workspace state exists,
  the global hook must return without validating session payloads, taking locks,
  creating directories, or writing files.
- Keep Ralph semantics host-agnostic. The loop state machine, progress model,
  and assistant-output protocol should not depend on Codex-specific hook details.
- Separate runtime behavior from profile installation. Runtime code must not
  import installer, doctor, or hook-registry modules.
- Make the high-risk boundary cases directly testable without running a real
  Codex session.

## Architecture Summary

Codex Ralph should be hook-first, but not hook-heavy.

```text
Codex Stop hook payload
  -> Codex host adapter
  -> Ralph runtime service
  -> Ralph core reducer
  -> storage/effects
  -> Codex hook response
```

The `Stop` hook is the right control point for Codex because it runs exactly at
assistant turn end, sees the last assistant message, and can decide whether to
continue the current session. A separate CLI runner would reduce some profile
complexity, but it would lose this precise in-session control. The trade-off is
that a global hook has a larger integration surface, so the hook itself must
stay thin and the core loop semantics must be isolated.

## Target Module Layout

```text
codex-ralph-loop/
  hooks/
    stop_continue.py              # Codex Stop hook adapter only

  ralph_core/
    __init__.py
    model.py                      # state, event, decision, effect types
    reducer.py                    # pure loop state machine
    protocol.py                   # completion token and RALPH_STATUS parser
    storage.py                    # safe paths, lock, atomic persistence
    effects.py                    # apply reducer effects to storage
    prompts.py                    # continuation prompt builder
    runtime.py                    # Stop-hook runtime orchestration
    control.py                    # start/resume/cancel workspace CLI
    errors.py                     # runtime error taxonomy

  profile/
    installer.py                  # install/uninstall profile files
    hook_registry.py              # hooks.json read/write/repair helpers
    doctor.py                     # read-only diagnostics
    toml_feature_flag.py          # config.toml feature flag handling
    package_manifest.py           # installed file/package manifest

  skills/
    ...                           # user-facing entrypoints

  tests/
    test_core_reducer.py
    test_protocol.py
    test_storage.py
    test_codex_stop_adapter.py
    test_profile_installer.py
```

The key rule is dependency direction: host adapters and profile tooling may
depend on Ralph core, but Ralph core must not depend on Codex or profile
installation.

## Layers

### Codex Stop Hook Adapter

`hooks/stop_continue.py` should only:

- read the JSON payload from stdin;
- validate the minimum fields needed to identify `cwd`;
- create a host-neutral `StopEvent` when runtime state requires it;
- call the Ralph runtime service;
- render the Codex hook JSON response.

It should not:

- parse `RALPH_STATUS`;
- know state compatibility rules;
- edit `hooks.json`, `config.toml`, or installed hook files;
- contain the loop state machine;
- create workspace files for a missing Ralph state.

### Ralph Protocol

`ralph_core/protocol.py` owns assistant-output parsing.

```text
last_assistant_message + completion_token -> ProtocolResult
```

It handles:

- final-line completion-token detection;
- optional terminal `RALPH_STATUS` block before the completion token;
- unfinished-turn `RALPH_STATUS` parsing;
- Markdown fenced and indented code masking;
- CRLF line endings;
- malformed, duplicated, misplaced, or non-final status blocks;
- fallback progress details for invalid output.

This layer does not know about sessions, locks, files, hooks, or installation.

### Ralph Core Reducer

`ralph_core/reducer.py` is the core domain model.

```text
LoopState + StopEvent + ProtocolResult -> Decision
```

The reducer should be pure or close to pure. It returns decisions and effects,
but does not read or write files.

Typical decisions:

- `Noop`: no active running loop, inactive state, blocked state, or session
  mismatch.
- `ContinueLoop`: append progress, advance iteration, save next state, and emit
  a continuation prompt.
- `PauseLoop`: save blocked state, append a stopped/blocked progress row, and
  tell Codex not to continue.
- `CompleteLoop`: clear state and append a completion row.
- `TerminalStop`: clear state after a hard stop such as `max_iterations`.
- `RuntimeErrorDecision`: stop with a user-facing storage or protocol message.

This makes the loop behavior testable as a matrix of inputs and expected
decisions.

### Storage

`ralph_core/storage.py` owns all workspace filesystem behavior:

- `.codex/ralph/state.json`;
- `.codex/ralph/progress.jsonl`;
- `.codex/ralph/control.lock`;
- strict managed-path symlink policy;
- bounded workspace lock acquisition for hook runtime;
- atomic writes with parent-directory fsync best effort;
- schema validation and explicit schema upgrade handlers.

State should include a schema version:

```json
{
  "schema_version": 1,
  "active": true,
  "phase": "running",
  "iteration": 0,
  "max_iterations": 100,
  "completion_token": "<promise>DONE</promise>",
  "prompt": "...",
  "claimed_session_id": null,
  "last_message_fingerprint": null,
  "repeat_count": 0,
  "started_at": "...",
  "updated_at": "..."
}
```

No legacy state schema is accepted. The operational trade-off is deliberate:
older active loops must be cancelled or rebuilt after this architecture change,
but the runtime no longer contains compatibility branches in the turn-end hot
path.

Trade-off: strict schema validation and symlink rejection can fail workspaces
that a permissive implementation might tolerate. That is intentional for Ralph
runtime control files: these files decide whether Codex continues executing, so
the safe failure mode is to stop and ask for repair instead of guessing.

### Effects

`ralph_core/effects.py` applies reducer effects to storage in one place.

The effect order is part of the contract:

- continue: append progress first, then save advanced state;
- pause: save blocked state first, then append progress;
- complete: clear state first, then append progress;
- terminal stop: clear state first, then append progress;
- cancel: clear state first, then append cancellation progress when a valid
  previous state exists.

Trade-off: no ordering is perfect. Continuing prioritizes auditability before
advancing control state. Terminal outcomes prioritize stopping the loop even if
the final ledger row later fails.

### Profile Tooling

Profile tooling owns installation and diagnostics only:

- link Ralph skills into `$AGENTS_HOME/skills`;
- copy runtime hook helper files into `$CODEX_HOME/hooks/ralph`;
- copy `ralph_core/` into `$CODEX_HOME/hooks/ralph/ralph_core`;
- register the global `Stop` hook in `$CODEX_HOME/hooks.json`;
- enable `codex_hooks = true` in `$CODEX_HOME/config.toml`;
- uninstall Ralph-managed artifacts without disabling shared Codex hook support;
- diagnose profile and workspace health.

The runtime hook must not import profile modules. This prevents installer and
registry complexity from entering the turn-end hot path.

## Runtime Flow

### Missing State

```text
Stop payload
  -> parse cwd only
  -> read state
  -> missing
  -> return no-op
```

No lock, mkdir, session validation, payload validation, or writes are allowed in
this path.

### Running Loop

```text
Stop payload
  -> parse cwd
  -> read state
  -> state is active and phase="running"
  -> validate session_id and last_assistant_message
  -> acquire bounded workspace lock
  -> re-read state
  -> parse assistant protocol
  -> reduce event
  -> apply effects
  -> render Codex response
```

The re-read after locking is mandatory because start, resume, cancel, and another
Stop hook may race with the first read.

### Continue

```text
assistant did not emit completion token
  -> valid STATUS=progress or STATUS=no_progress
  -> append progress row
  -> increment iteration
  -> save state
  -> return continuation prompt to Codex
```

### Complete

```text
assistant final non-whitespace line equals completion token
  -> optional terminal status block must report STATUS=complete
  -> clear state
  -> append completion progress row
  -> return no continuation
```

### Pause

```text
malformed status block
blocked status
completion/status mismatch
repeated response circuit
storage error
  -> save phase="blocked" when state is still valid
  -> append progress when possible
  -> return stop message
```

### Terminal Stop

```text
iteration >= max_iterations
  -> clear state
  -> append terminal progress row
  -> return stop message
```

## Host Adapter Contract

The core should support host adapters without forking loop semantics.

```python
class HostAdapter:
    def parse_payload(self, raw: str) -> "PayloadResult":
        ...

    def render_response(self, decision: "RuntimeDecision") -> str:
        ...
```

Codex is one adapter. Future OpenCode plugin or Claude runner integrations
should reuse the same core reducer, protocol, and state model.

## Failure Policy

The following policies are part of the architecture:

- Missing Ralph state: no-op with zero side effects.
- Invalid state JSON or schema: stop and ask the user to cancel or repair.
- Invalid progress ledger: do not append blindly; stop with a storage error.
- Lock timeout: stop the hook and report a lock timeout.
- Session mismatch: no-op.
- Missing `session_id` while state is running: stop cleanly without mutation.
- `max_iterations` reached: terminal stop and clear state.
- Completion token plus non-complete status: pause for explicit repair/resume.
- Profile registry unreadable during uninstall: fail closed before deleting hook
  files that may still be referenced.
- Readable but invalid profile registry during uninstall: remove local managed
  hook files when safe, but leave the registry untouched and report it.

## Testing Strategy

Tests should align with the architectural layers:

- protocol tests: status block parsing, Markdown masking, CRLF, completion token
  placement, malformed examples;
- reducer tests: decision matrix for running, blocked, inactive, session
  mismatch, completion, continuation, max iterations, repeated responses;
- storage tests: symlink policy, lock timeout, atomic write behavior, schema
  upgrade handling, invalid progress ledger handling;
- adapter tests: Codex payload validation, missing-state no-op contract, JSON
  response rendering;
- profile tests: install idempotency, hook registry repair, uninstall fail-closed
  cases, doctor read-only behavior.

The highest-risk regression suite is the boundary contract suite: global hook
no-op behavior, managed-path symlink handling, hook command equivalence, bounded
timeouts, and invalid persisted files.
