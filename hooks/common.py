from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal, TypedDict

STATE_RELATIVE_PATH = Path('.codex/ralph/state.json')
PROGRESS_RELATIVE_PATH = Path('.codex/ralph/progress.jsonl')
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
STATUS_BLOCK_REQUIRED_FIELDS = ('STATUS', 'SUMMARY', 'FILES', 'CHECKS')
STATUS_START_LINE_PATTERN = re.compile(
    rf'^[ \t]*{re.escape(RALPH_STATUS_START_MARKER)}[ \t]*$',
    re.MULTILINE,
)
STATUS_END_LINE_PATTERN = re.compile(
    rf'^[ \t]*{re.escape(RALPH_STATUS_END_MARKER)}[ \t]*$',
    re.MULTILINE,
)
STATUS_BLOCK_PATTERN = re.compile(
    rf'^[ \t]*{re.escape(RALPH_STATUS_START_MARKER)}[ \t]*\r?\n'
    rf'(.*?)'
    rf'\r?\n[ \t]*{re.escape(RALPH_STATUS_END_MARKER)}[ \t]*$',
    re.DOTALL | re.MULTILINE,
)


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


def workspace_root() -> Path:
    return Path(os.environ.get('PWD') or os.getcwd())


def state_path(cwd: str | None = None) -> Path:
    root = Path(cwd) if cwd else workspace_root()
    return root / STATE_RELATIVE_PATH


def progress_path(cwd: str | None = None) -> Path:
    root = Path(cwd) if cwd else workspace_root()
    return root / PROGRESS_RELATIVE_PATH


def symlink_component_error(root: Path, relative_path: Path) -> str | None:
    current = root
    for part in relative_path.parts:
        current = current / part
        if not current.is_symlink():
            continue
        # Trade-off: Ralph's managed paths (.codex/ralph and installed hook dirs) are internal
        # control surfaces, not user-authored references. Reject every symlink component here,
        # even live links that resolve back inside the tree, so reads and writes never depend on
        # path aliasing and can fail closed before mutating or executing anything unexpected.
        if current.exists():
            return f'path component is a symlink: {current}'
        return f'path component is a dangling symlink: {current}'
    return None


def symlink_parent_error(root: Path, relative_path: Path) -> str | None:
    parent = relative_path.parent
    if parent == Path('.'):
        return None
    # Trade-off: cleanup paths may unlink a symlink at the leaf itself, but parent-directory
    # symlinks still redirect the managed storage tree and remain forbidden.
    return symlink_component_error(root, parent)


def resolve_atomic_write_target(path: Path, *, preserve_leaf_symlink: bool) -> Path:
    if not preserve_leaf_symlink or not path.is_symlink():
        return path
    try:
        return path.resolve(strict=False)
    except RuntimeError as exc:
        raise OSError(f'unable to resolve symlink target for {path}: {exc}') from exc


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def normalize_text(text: str) -> str:
    return ' '.join(text.split())


def truncate_summary(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ''
    return normalized[:SUMMARY_LIMIT]


def fingerprint_message(text: str) -> str:
    normalized = normalize_text(text)
    digest = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    return f'sha256:{digest}'


def completion_token_emitted(text: str, token: str) -> bool:
    if not token:
        return False
    stripped = text.rstrip()
    if not stripped:
        return False
    last_line = stripped.splitlines()[-1].strip()
    return last_line == token


def contains_ralph_status_markup(text: str) -> bool:
    # Trade-off: only treat standalone marker lines as control syntax.
    # Inline mentions remain normal prose so completed turns can document the format safely.
    return (
        bool(STATUS_START_LINE_PATTERN.search(text))
        or bool(STATUS_END_LINE_PATTERN.search(text))
    )


def parse_trailing_ralph_status(text: str) -> tuple[RalphStatusParseResult, bool]:
    trimmed = text.rstrip()
    if not trimmed:
        return ({
            'ok': False,
            'error': 'missing RALPH_STATUS block',
        }, False)

    trailing_end = None
    for match in STATUS_END_LINE_PATTERN.finditer(trimmed):
        if trimmed[match.end():].strip():
            continue
        trailing_end = match

    if trailing_end is not None:
        trailing_start = None
        for match in STATUS_START_LINE_PATTERN.finditer(trimmed[:trailing_end.start()]):
            trailing_start = match
        if trailing_start is None:
            return ({
                'ok': False,
                'error': 'missing RALPH_STATUS start marker before trailing end marker',
            }, True)
        # Trade-off: on completed turns, only the terminal block immediately before the
        # completion token is treated as control data. Earlier blocks remain normal message
        # content so doc/help responses can quote the protocol without being trapped as paused.
        candidate = trimmed[trailing_start.start():trailing_end.end()]
        return (parse_ralph_status(candidate), True)

    last_start = None
    last_end = None
    for match in STATUS_START_LINE_PATTERN.finditer(trimmed):
        last_start = match
    for match in STATUS_END_LINE_PATTERN.finditer(trimmed):
        last_end = match
    if last_start is not None and (last_end is None or last_start.start() > last_end.start()):
        return ({
            'ok': False,
            'error': f'missing trailing {RALPH_STATUS_END_MARKER} marker',
        }, True)

    return ({
        'ok': False,
        'error': 'missing RALPH_STATUS block',
    }, False)


def parse_ralph_status(text: str, *, require_final: bool = True) -> RalphStatusParseResult:
    matches = list(STATUS_BLOCK_PATTERN.finditer(text))
    if not matches:
        return {
            'ok': False,
            'error': 'missing RALPH_STATUS block',
        }
    if len(matches) != 1:
        return {
            'ok': False,
            'error': f'expected exactly one RALPH_STATUS block, found {len(matches)}',
        }

    match = matches[0]
    if require_final and text[match.end():].strip():
        return {
            'ok': False,
            'error': 'RALPH_STATUS block must be the final non-whitespace content in the message',
        }

    block = match.group(1)
    fields: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ':' not in line:
            return {
                'ok': False,
                'error': f'invalid line inside RALPH_STATUS block: {line!r}',
            }
        key, value = line.split(':', 1)
        normalized_key = key.strip().upper()
        if normalized_key in fields:
            return {
                'ok': False,
                'error': f'duplicate {normalized_key} field in RALPH_STATUS block',
            }
        fields[normalized_key] = value.strip()

    missing_fields = [
        field
        for field in STATUS_BLOCK_REQUIRED_FIELDS
        if field not in fields
    ]
    if missing_fields:
        return {
            'ok': False,
            'error': f'missing required field(s): {", ".join(missing_fields)}',
        }

    status = fields.get('STATUS', '').lower()
    if status not in ASSISTANT_PROGRESS_STATUSES:
        return {
            'ok': False,
            'error': f'STATUS must be one of {", ".join(sorted(ASSISTANT_PROGRESS_STATUSES))}',
        }

    summary = normalize_text(fields.get('SUMMARY', ''))
    if len(summary) > SUMMARY_LIMIT:
        return {
            'ok': False,
            'error': f'SUMMARY must be <= {SUMMARY_LIMIT} characters after whitespace normalization',
        }
    files = [
        item.strip()
        for item in fields.get('FILES', '').split(',')
        if item.strip()
    ]
    checks = [
        item.strip()
        for item in fields.get('CHECKS', '').split(';')
        if item.strip()
    ]
    return {
        'ok': True,
        'status': status,
        'summary': summary,
        'files': files,
        'checks': checks,
    }
