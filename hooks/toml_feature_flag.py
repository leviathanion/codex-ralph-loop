from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from common import atomic_write_text

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[import-not-found]

ASSIGNMENT_PATTERN = re.compile(r'^\s*codex_hooks\s*=')
PROBE_KEY = '__codex_probe_key__'

EnsureStatus = Literal['created', 'deduplicated', 'updated', 'unchanged']
MultilineStringState = Literal['none', 'basic', 'literal']


@dataclass(frozen=True)
class HeaderInfo:
    path: tuple[str, ...]
    is_array: bool


@dataclass(frozen=True)
class AssignmentRange:
    start: int
    end: int
    closed: bool


def render_toml_lines(lines: list[str]) -> str:
    return '\n'.join(lines).rstrip() + '\n'


def write_text_atomic(path: Path, contents: str) -> None:
    atomic_write_text(path, contents, preserve_leaf_symlink=True)


def normalize_legacy_comment_syntax(text: str) -> str:
    normalized_lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if stripped.startswith(';'):
            indent = raw_line[:len(raw_line) - len(stripped)]
            normalized_lines.append(f'{indent}#{stripped[1:]}')
        else:
            normalized_lines.append(raw_line)
    return '\n'.join(normalized_lines)


def load_toml_document(text: str, path: Path) -> dict[str, Any]:
    if not text.strip():
        return {}

    try:
        document = tomllib.loads(normalize_legacy_comment_syntax(text))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f'invalid TOML in {path}: {exc}') from exc

    if not isinstance(document, dict):
        raise ValueError(f'invalid TOML in {path}: top-level document must be a table')
    return document


def document_has_codex_hooks_enabled(document: dict[str, Any]) -> bool:
    features = document.get('features')
    return isinstance(features, dict) and features.get('codex_hooks') is True


def load_enabled_toml_document(text: str, path: Path) -> dict[str, Any]:
    document = load_toml_document(text, path)
    if not document_has_codex_hooks_enabled(document):
        raise ValueError(f'failed to enable codex_hooks in {path}')
    return document


def strip_inline_comment(line: str) -> str:
    in_basic_string = False
    in_literal_string = False
    escaped = False

    for idx, char in enumerate(line):
        if in_basic_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_basic_string = False
            continue

        if in_literal_string:
            if char == "'":
                in_literal_string = False
            continue

        if char == '"':
            in_basic_string = True
            continue
        if char == "'":
            in_literal_string = True
            continue
        if char == '#':
            return line[:idx]
    return line


def advance_multiline_string_state(line: str, state: MultilineStringState) -> MultilineStringState:
    in_basic_string = False
    in_literal_string = False
    escaped = False
    idx = 0

    while idx < len(line):
        if state == 'basic':
            char = line[idx]
            if escaped:
                escaped = False
                idx += 1
                continue
            if char == '\\':
                escaped = True
                idx += 1
                continue
            if line.startswith('"""', idx):
                state = 'none'
                idx += 3
                continue
            idx += 1
            continue

        if state == 'literal':
            if line.startswith("'''", idx):
                state = 'none'
                idx += 3
                continue
            idx += 1
            continue

        char = line[idx]
        if in_basic_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_basic_string = False
            idx += 1
            continue

        if in_literal_string:
            if char == "'":
                in_literal_string = False
            idx += 1
            continue

        if char == '#':
            return state
        if line.startswith('"""', idx):
            state = 'basic'
            idx += 3
            continue
        if line.startswith("'''", idx):
            state = 'literal'
            idx += 3
            continue
        if char == '"':
            in_basic_string = True
            idx += 1
            continue
        if char == "'":
            in_literal_string = True
            idx += 1
            continue
        idx += 1

    return state


def iter_lines_outside_multiline_strings(lines: list[str]) -> Iterator[tuple[int, str]]:
    state: MultilineStringState = 'none'
    for idx, line in enumerate(lines):
        starts_inside_multiline_string = state != 'none'
        state = advance_multiline_string_state(line, state)
        if starts_inside_multiline_string:
            continue
        yield idx, line


def find_probe_path(node: Any, path: tuple[str, ...] = ()) -> tuple[str, ...] | None:
    if isinstance(node, dict):
        if PROBE_KEY in node:
            return path
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            result = find_probe_path(value, path + (key,))
            if result is not None:
                return result
        return None

    if isinstance(node, list):
        for value in node:
            result = find_probe_path(value, path)
            if result is not None:
                return result
    return None


def parse_header(line: str) -> HeaderInfo | None:
    content = strip_inline_comment(line).strip()
    if not content.startswith('[') or not content.endswith(']'):
        return None

    try:
        document = tomllib.loads(f'{content}\n{PROBE_KEY} = 1\n')
    except tomllib.TOMLDecodeError:
        return None

    path = find_probe_path(document)
    if path is None:
        return None
    return HeaderInfo(path=path, is_array=content.startswith('[['))


def find_features_section_start(lines: list[str]) -> int | None:
    for idx, line in iter_lines_outside_multiline_strings(lines):
        header = parse_header(line)
        if header is None:
            continue
        if not header.is_array and header.path == ('features',):
            return idx
    return None


def section_end_after(lines: list[str], section_start: int) -> int:
    for relative_idx, line in iter_lines_outside_multiline_strings(lines[section_start + 1:]):
        if parse_header(line) is not None:
            return section_start + 1 + relative_idx
    return len(lines)


def parse_codex_hooks_assignment(line: str) -> bool | None:
    content = strip_inline_comment(line)
    if not ASSIGNMENT_PATTERN.match(content):
        return None

    try:
        document = tomllib.loads(normalize_legacy_comment_syntax(content))
    except tomllib.TOMLDecodeError:
        return None

    value = document.get('codex_hooks')
    return value if isinstance(value, bool) else None


def assignment_range(lines: list[str], start: int) -> AssignmentRange:
    state = advance_multiline_string_state(lines[start], 'none')
    if state == 'none':
        return AssignmentRange(start=start, end=start + 1, closed=True)

    for idx in range(start + 1, len(lines)):
        state = advance_multiline_string_state(lines[idx], state)
        if state == 'none':
            return AssignmentRange(start=start, end=idx + 1, closed=True)

    # Trade-off: do not try to "repair" an unterminated multiline value by deleting everything
    # to EOF. Mark it unclosed so the section editor leaves the original text intact and lets
    # TOML validation fail without mutating unrelated lines after a malformed user edit.
    return AssignmentRange(start=start, end=start + 1, closed=False)


def build_updated_section(section_lines: list[str]) -> tuple[list[str], EnsureStatus]:
    active_ranges: list[AssignmentRange] = []
    for idx, line in iter_lines_outside_multiline_strings(section_lines):
        stripped = line.lstrip()
        if stripped.startswith('#') or stripped.startswith(';'):
            continue
        if ASSIGNMENT_PATTERN.match(strip_inline_comment(line)):
            active_ranges.append(assignment_range(section_lines, idx))

    updated_section = list(section_lines)
    if not active_ranges:
        insert_at = len(updated_section)
        while insert_at > 0 and updated_section[insert_at - 1].strip() == '':
            insert_at -= 1
        updated_section.insert(insert_at, 'codex_hooks = true')
        return updated_section, 'updated'

    if any(not current_range.closed for current_range in active_ranges):
        return updated_section, 'unchanged'

    first_range = active_ranges[0]
    first = first_range.start
    first_changed = parse_codex_hooks_assignment(updated_section[first]) is not True

    updated_section = []
    cursor = 0
    for range_index, current_range in enumerate(active_ranges):
        updated_section.extend(section_lines[cursor:current_range.start])
        if range_index == 0:
            if first_changed:
                # Trade-off: canonicalize only the codex_hooks assignment being repaired. For
                # multiline string values, replace the whole value range so leftover continuation
                # lines cannot corrupt the TOML candidate.
                updated_section.append('codex_hooks = true')
            else:
                updated_section.extend(section_lines[current_range.start:current_range.end])
        cursor = current_range.end
    updated_section.extend(section_lines[cursor:])

    if section_lines == updated_section:
        return updated_section, 'unchanged'
    if len(active_ranges) > 1 and not first_changed:
        return updated_section, 'deduplicated'
    return updated_section, 'updated'


def codex_hooks_enabled(path: Path) -> bool:
    if not path.exists():
        return False

    document = load_toml_document(path.read_text(encoding='utf-8'), path)
    return document_has_codex_hooks_enabled(document)


def ensure_codex_hooks_enabled(path: Path) -> EnsureStatus:
    original_text = path.read_text(encoding='utf-8') if path.exists() else ''
    lines = original_text.splitlines()
    original_error: ValueError | None = None
    original_document: dict[str, Any] | None = None
    if original_text.strip():
        try:
            original_document = load_toml_document(original_text, path)
        except ValueError as exc:
            original_error = exc

    section_start = find_features_section_start(lines)

    if section_start is None:
        if original_error is not None:
            raise original_error

        new_lines = list(lines)
        if new_lines and new_lines[-1] != '':
            new_lines.append('')
        new_lines.extend(['[features]', 'codex_hooks = true'])
        candidate_text = render_toml_lines(new_lines)
        try:
            load_enabled_toml_document(candidate_text, path)
        except ValueError as exc:
            # Trade-off: append a real [features] table only when the current
            # document can absorb that table without redefining keys. Implicit
            # parents created by child-table headers are repairable; dotted keys,
            # inline tables, scalars, and arrays are not.
            if original_document is not None and 'features' in original_document:
                raise ValueError(f'features in {path} is not an editable top-level table') from exc
            raise
        write_text_atomic(path, candidate_text)
        return 'created'

    section_end = section_end_after(lines, section_start)
    section_lines = lines[section_start + 1:section_end]
    updated_section, status = build_updated_section(section_lines)

    if status == 'unchanged':
        load_enabled_toml_document(render_toml_lines(lines), path)
        return status

    new_lines = lines[:section_start + 1] + updated_section + lines[section_end:]
    candidate_text = render_toml_lines(new_lines)
    load_enabled_toml_document(candidate_text, path)
    write_text_atomic(path, candidate_text)
    return status


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in {'get', 'ensure'}:
        print('usage: toml_feature_flag.py get|ensure <config.toml>', file=sys.stderr)
        return 2

    command, config_toml = args
    path = Path(config_toml)
    try:
        if command == 'get':
            print('true' if codex_hooks_enabled(path) else 'false')
        else:
            print(ensure_codex_hooks_enabled(path))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
