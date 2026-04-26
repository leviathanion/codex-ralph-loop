from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 3
STATE_RELATIVE_PATH = Path('.codex/ralph/state.json')
PROGRESS_RELATIVE_PATH = Path('.codex/ralph/progress.jsonl')
LOCK_RELATIVE_PATH = Path('.codex/ralph/control.lock')
DEFAULT_MAX_ITERATIONS = 100
SUMMARY_LIMIT = 200
ACTIVE_PHASES = {'running', 'blocked', 'failed'}
ASSISTANT_PROGRESS_STATUSES = {
    'progress',
    'blocked',
    'failed',
    'complete',
}
LEDGER_PROGRESS_STATUSES = ASSISTANT_PROGRESS_STATUSES | {
    'started',
    'resumed',
    'cancelled',
    'stopped',
}


class StatusSnapshot(TypedDict):
    status: str
    summary: str
    files: list[str]
    checks: list[str]
    reason: str | None
    updated_at: str


class PendingUpdate(StatusSnapshot):
    iteration: int
    session_id: str


class LoopState(TypedDict):
    schema_version: int
    prompt: str
    iteration: int
    max_iterations: int
    claimed_session_id: str | None
    phase: str
    pending_update: PendingUpdate | None
    last_status: StatusSnapshot | None
    started_at: str
    updated_at: str
    last_message_fingerprint: str | None
    repeat_count: int


class ProgressEntry(TypedDict):
    ts: str
    iteration: int
    session_id: str | None
    status: str
    summary: str
    files: list[str]
    checks: list[str]
    message_fingerprint: str | None
    reason: str | None


class ProgressDetails(TypedDict):
    summary: str
    files: list[str]
    checks: list[str]


@dataclass(frozen=True)
class StopEvent:
    cwd: str
    session_id: str
    last_assistant_message: str


EffectKind = Literal['append_progress', 'save_state', 'clear_state']


@dataclass(frozen=True)
class RuntimeEffect:
    kind: EffectKind
    state: LoopState | None = None
    progress: ProgressEntry | None = None


DecisionKind = Literal[
    'noop',
    'continue',
    'pause',
    'complete',
    'terminal_stop',
    'runtime_error',
]


@dataclass(frozen=True)
class RuntimeDecision:
    kind: DecisionKind
    effects: tuple[RuntimeEffect, ...] = ()
    response: dict[str, Any] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
