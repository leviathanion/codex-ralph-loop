from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 1
STATE_RELATIVE_PATH = Path('.codex/ralph/state.json')
PROGRESS_RELATIVE_PATH = Path('.codex/ralph/progress.jsonl')
LOCK_RELATIVE_PATH = Path('.codex/ralph/control.lock')
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_COMPLETION_TOKEN = '<promise>DONE</promise>'
SUMMARY_LIMIT = 200
RALPH_STATUS_START_MARKER = '---RALPH_STATUS---'
RALPH_STATUS_END_MARKER = '---END_RALPH_STATUS---'
ALLOWED_PHASES = {'running', 'blocked'}
ASSISTANT_PROGRESS_STATUSES = {
    'progress',
    'no_progress',
    'blocked',
    'complete',
}
LEDGER_PROGRESS_STATUSES = ASSISTANT_PROGRESS_STATUSES | {
    'started',
    'resumed',
    'cancelled',
    'stopped',
}


class LoopState(TypedDict):
    schema_version: int
    active: bool
    prompt: str
    iteration: int
    max_iterations: int
    completion_token: str
    claimed_session_id: str | None
    phase: str
    started_at: str | None
    updated_at: str | None
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


class ParsedRalphStatus(ProgressDetails):
    ok: Literal[True]
    status: str


class RalphStatusParseError(TypedDict):
    ok: Literal[False]
    error: str


RalphStatusParseResult = ParsedRalphStatus | RalphStatusParseError


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

