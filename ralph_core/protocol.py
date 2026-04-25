from __future__ import annotations

import hashlib
import re

from ralph_core.model import (
    ASSISTANT_PROGRESS_STATUSES,
    RALPH_STATUS_END_MARKER,
    RALPH_STATUS_START_MARKER,
    SUMMARY_LIMIT,
    RalphStatusParseResult,
)

STATUS_BLOCK_REQUIRED_FIELDS = ('STATUS', 'SUMMARY', 'FILES', 'CHECKS')
STATUS_BLOCK_ALLOWED_FIELDS = frozenset(STATUS_BLOCK_REQUIRED_FIELDS)
STATUS_MARKER_STRINGS = (RALPH_STATUS_START_MARKER, RALPH_STATUS_END_MARKER)
STATUS_START_LINE_PATTERN = re.compile(
    rf'^[ \t]*{re.escape(RALPH_STATUS_START_MARKER)}[ \t]*\r?$',
    re.MULTILINE,
)
STATUS_END_LINE_PATTERN = re.compile(
    rf'^[ \t]*{re.escape(RALPH_STATUS_END_MARKER)}[ \t]*\r?$',
    re.MULTILINE,
)
STATUS_BLOCK_PATTERN = re.compile(
    rf'^[ \t]*{re.escape(RALPH_STATUS_START_MARKER)}[ \t]*\r?\n'
    rf'(.*?)'
    rf'\r?\n[ \t]*{re.escape(RALPH_STATUS_END_MARKER)}[ \t]*\r?$',
    re.DOTALL | re.MULTILINE,
)
MARKDOWN_FENCE_PATTERN = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})(.*)$')
MARKDOWN_INDENTED_CODE_PATTERN = re.compile(r'^(?: {4,}|\t)')


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
    original_lines = stripped.splitlines()
    masked_lines = mask_markdown_code_blocks(stripped, mask_unclosed_fences=True).splitlines()
    if not original_lines or not masked_lines:
        return False
    # Trade-off: completion clears Ralph state, so be stricter than status-marker scans:
    # a token used as a Markdown code example, including an unterminated fenced example, is
    # not treated as the user's explicit "task is done" control signal.
    return original_lines[-1].strip() == token and masked_lines[-1].strip() == token


def _mask_line_contents(line: str) -> str:
    return ''.join(char if char in '\r\n' else ' ' for char in line)


def mask_markdown_fenced_code_blocks(text: str, *, mask_unclosed: bool = False) -> str:
    # Trade-off: Ralph treats raw status markers as control syntax, but completed turns may also
    # document the protocol. Mask fenced code blocks before marker scans so markdown examples do
    # not participate in Stop-hook control flow while preserving character offsets for later slices.
    # Only mask fences that close successfully; an unterminated fence is treated as normal content
    # so malformed markdown cannot hide live control markers from the Stop hook.
    masked_lines: list[str] = []
    pending_fence_lines: list[str] | None = None
    active_fence: tuple[str, int] | None = None

    for raw_line in text.splitlines(keepends=True):
        match = MARKDOWN_FENCE_PATTERN.match(raw_line)
        if active_fence is None:
            if match is None:
                masked_lines.append(raw_line)
                continue

            fence = match.group(1)
            if fence[0] == '`' and '`' in match.group(2):
                # Trade-off: follow CommonMark's extra backtick-fence rule instead of masking
                # every line that starts with three backticks. Invalid pseudo-fences must not be
                # able to hide live Ralph status markers from the Stop hook.
                masked_lines.append(raw_line)
                continue
            active_fence = (fence[0], len(fence))
            pending_fence_lines = [raw_line]
            continue

        if pending_fence_lines is None:
            raise RuntimeError('internal markdown fence parser lost the pending fence buffer')
        pending_fence_lines.append(raw_line)
        if match is None:
            continue

        fence = match.group(1)
        if fence[0] != active_fence[0] or len(fence) < active_fence[1]:
            continue
        if match.group(2).strip():
            continue
        masked_lines.extend(_mask_line_contents(line) for line in pending_fence_lines)
        pending_fence_lines = None
        active_fence = None

    if pending_fence_lines is not None and mask_unclosed:
        masked_lines.extend(_mask_line_contents(line) for line in pending_fence_lines)
    elif pending_fence_lines is not None:
        masked_lines.extend(pending_fence_lines)

    return ''.join(masked_lines)


def mask_markdown_indented_code_blocks(text: str) -> str:
    # Trade-off: treat classic 4-space/tab-indented markdown blocks as code too so protocol
    # examples are never parsed as live Ralph control syntax. Real status blocks must therefore
    # be emitted as ordinary prose, not inside markdown code formatting.
    masked_lines: list[str] = []
    inside_block = False

    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip('\r\n')
        is_blank = not line.strip()
        is_indented = bool(MARKDOWN_INDENTED_CODE_PATTERN.match(line))

        if inside_block and (is_blank or is_indented):
            masked_lines.append(_mask_line_contents(raw_line))
            continue

        inside_block = is_indented
        if inside_block:
            masked_lines.append(_mask_line_contents(raw_line))
        else:
            masked_lines.append(raw_line)

    return ''.join(masked_lines)


def mask_markdown_code_blocks(text: str, *, mask_unclosed_fences: bool = False) -> str:
    return mask_markdown_indented_code_blocks(
        mask_markdown_fenced_code_blocks(text, mask_unclosed=mask_unclosed_fences)
    )


def contains_ralph_status_markup(text: str) -> bool:
    # Trade-off: only treat standalone marker lines as control syntax.
    # Inline mentions and markdown code examples remain normal prose so completed turns can
    # document the format safely without tripping Stop-hook control flow.
    searchable_text = mask_markdown_code_blocks(text)
    return (
        bool(STATUS_START_LINE_PATTERN.search(searchable_text))
        or bool(STATUS_END_LINE_PATTERN.search(searchable_text))
    )


def parse_trailing_ralph_status(text: str) -> tuple[RalphStatusParseResult, bool]:
    trimmed = text.rstrip()
    if not trimmed:
        return ({
            'ok': False,
            'error': 'missing RALPH_STATUS block',
        }, False)

    searchable_text = mask_markdown_code_blocks(trimmed)
    trailing_end = None
    for match in STATUS_END_LINE_PATTERN.finditer(searchable_text):
        if trimmed[match.end():].strip():
            continue
        trailing_end = match

    if trailing_end is not None:
        trailing_start = None
        for match in STATUS_START_LINE_PATTERN.finditer(searchable_text[:trailing_end.start()]):
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
    for match in STATUS_START_LINE_PATTERN.finditer(searchable_text):
        last_start = match
    for match in STATUS_END_LINE_PATTERN.finditer(searchable_text):
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
    searchable_text = mask_markdown_code_blocks(text)
    matches = list(STATUS_BLOCK_PATTERN.finditer(searchable_text))
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

    block_text = text[match.start():match.end()]
    block_match = STATUS_BLOCK_PATTERN.match(block_text)
    if block_match is None:
        return {
            'ok': False,
            'error': 'internal RALPH_STATUS parser failed to re-read the matched block',
        }
    block = block_match.group(1)
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
        if normalized_key not in STATUS_BLOCK_ALLOWED_FIELDS:
            return {
                'ok': False,
                'error': f'unknown {normalized_key} field in RALPH_STATUS block',
            }
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

    for field in ('SUMMARY', 'FILES', 'CHECKS'):
        if any(marker in fields[field] for marker in STATUS_MARKER_STRINGS):
            # Trade-off: this rejects rare prose/file/check text that literally mentions the
            # control markers, but it keeps marker scanning unambiguous across retries and logs.
            return {
                'ok': False,
                'error': f'{field} must not contain RALPH_STATUS marker strings',
            }

    status = fields.get('STATUS', '').lower()
    if status not in ASSISTANT_PROGRESS_STATUSES:
        return {
            'ok': False,
            'error': f'STATUS must be one of {", ".join(sorted(ASSISTANT_PROGRESS_STATUSES))}',
        }

    summary = normalize_text(fields.get('SUMMARY', ''))
    if not summary:
        return {
            'ok': False,
            'error': 'SUMMARY must be a non-empty single-line summary',
        }
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
