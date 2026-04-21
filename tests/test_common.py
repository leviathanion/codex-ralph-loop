from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))

import common  # noqa: E402
import state_store  # noqa: E402


class CommonModuleTests(unittest.TestCase):
    def test_read_state_upgrades_legacy_state_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = workspace / '.codex' / 'ralph' / 'state.json'
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                'active': True,
                'prompt': 'finish the task',
                'iteration': 2,
                'max_iterations': 5,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': 'session-a',
            }) + '\n', encoding='utf-8')

            result = state_store.read_state(str(workspace))

            self.assertEqual(result.status, 'ok')
            assert result.value is not None
            self.assertEqual(result.value['phase'], 'running')
            self.assertIsNone(result.value['started_at'])
            self.assertIsNone(result.value['updated_at'])
            self.assertIsNone(result.value['last_message_fingerprint'])
            self.assertEqual(result.value['repeat_count'], 0)

    def test_read_state_requires_complete_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = workspace / '.codex' / 'ralph' / 'state.json'
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({'active': True}) + '\n', encoding='utf-8')

            result = state_store.read_state(str(workspace))

            self.assertEqual(result.status, 'invalid_schema')
            self.assertIn('prompt must be a string', result.errors)
            self.assertIn('iteration must be an integer', result.errors)

    def test_save_state_round_trips_complete_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'active': True,
                'prompt': 'finish the task',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
            })

            state_store.save_state(state, str(workspace))
            loaded = state_store.read_state(str(workspace))

            self.assertEqual(loaded.status, 'ok')
            self.assertEqual(loaded.value, state)

    def test_save_state_rejects_live_symlinked_storage_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / 'workspace'
            workspace.mkdir()
            external_root = Path(tmpdir) / 'external-state'
            external_root.mkdir()
            os.symlink(external_root, workspace / '.codex')
            state = state_store.default_state()
            state.update({
                'active': True,
                'prompt': 'finish the task',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
            })

            with self.assertRaises(state_store.StorageError) as exc:
                state_store.save_state(state, str(workspace))

            self.assertIn('path component is a symlink', str(exc.exception))
            self.assertFalse((external_root / 'ralph' / 'state.json').exists())

    def test_read_state_reports_unreadable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = workspace / '.codex' / 'ralph' / 'state.json'
            state_file.mkdir(parents=True, exist_ok=True)

            result = state_store.read_state(str(workspace))

            self.assertEqual(result.status, 'read_error')
            self.assertIn('unable to read', result.errors[0])

    def test_append_progress_entry_creates_jsonl_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            entry = {
                'ts': common.now_iso(),
                'iteration': 0,
                'session_id': None,
                'status': 'started',
                'summary': 'Ralph loop started',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }

            ledger = state_store.append_progress_entry(entry, str(workspace))
            self.assertTrue(ledger.exists())

            payload = json.loads(ledger.read_text(encoding='utf-8').strip())
            self.assertEqual(state_store.validate_progress_entry(payload), [])

    def test_append_progress_entry_rejects_live_symlinked_ralph_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / 'workspace'
            workspace.mkdir()
            (workspace / '.codex').mkdir()
            external_root = Path(tmpdir) / 'external-ledger'
            external_root.mkdir()
            os.symlink(external_root, workspace / '.codex' / 'ralph')
            entry = {
                'ts': common.now_iso(),
                'iteration': 0,
                'session_id': None,
                'status': 'started',
                'summary': 'Ralph loop started',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }

            with self.assertRaises(state_store.StorageError) as exc:
                state_store.append_progress_entry(entry, str(workspace))

            self.assertIn('path component is a symlink', str(exc.exception))
            self.assertFalse((external_root / 'progress.jsonl').exists())

    def test_append_progress_entry_rejects_invalid_existing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            ledger = common.progress_path(str(workspace))
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text('not-json\n', encoding='utf-8')
            entry = {
                'ts': common.now_iso(),
                'iteration': 1,
                'session_id': None,
                'status': 'progress',
                'summary': 'keep working',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }

            with self.assertRaises(state_store.StorageError) as exc:
                state_store.append_progress_entry(entry, str(workspace))

            self.assertIn('progress ledger is invalid', str(exc.exception))
            self.assertEqual(ledger.read_text(encoding='utf-8'), 'not-json\n')

    def test_validate_progress_file_reports_truncated_last_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            ledger = common.progress_path(str(workspace))
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(
                json.dumps({
                    'ts': common.now_iso(),
                    'iteration': 0,
                    'session_id': None,
                    'status': 'started',
                    'summary': 'Ralph loop started',
                    'files': [],
                    'checks': [],
                    'message_fingerprint': None,
                    'reason': None,
                }) + '\n{"ts": ',
                encoding='utf-8',
            )

            errors = state_store.validate_progress_file(ledger)

            self.assertEqual(errors, ['line 2: invalid JSON (Expecting value)'])

    def test_append_progress_entry_rejects_truncated_last_line_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            ledger = common.progress_path(str(workspace))
            ledger.parent.mkdir(parents=True, exist_ok=True)
            first_entry = {
                'ts': common.now_iso(),
                'iteration': 0,
                'session_id': None,
                'status': 'started',
                'summary': 'Ralph loop started',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }
            original_contents = json.dumps(first_entry) + '\n{"ts": '
            ledger.write_text(original_contents, encoding='utf-8')
            second_entry = {
                'ts': common.now_iso(),
                'iteration': 1,
                'session_id': None,
                'status': 'progress',
                'summary': 'keep working',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }

            with self.assertRaises(state_store.StorageError) as exc:
                state_store.append_progress_entry(second_entry, str(workspace))

            self.assertIn('line 2: invalid JSON', str(exc.exception))
            self.assertEqual(ledger.read_text(encoding='utf-8'), original_contents)

    def test_validate_progress_file_reports_unreadable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / 'progress.jsonl'
            progress_dir.mkdir()

            errors = state_store.validate_progress_file(progress_dir)

            self.assertEqual(len(errors), 1)
            self.assertIn('unable to read', errors[0])

    def test_validate_progress_file_rejects_live_symlinked_workspace_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / 'workspace'
            workspace.mkdir()
            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            external_root = Path(tmpdir) / 'external-ledger'
            external_root.mkdir()
            external_ledger = external_root / 'progress.jsonl'
            external_ledger.write_text(json.dumps({
                'ts': common.now_iso(),
                'iteration': 0,
                'session_id': None,
                'status': 'started',
                'summary': 'Ralph loop started',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }) + '\n', encoding='utf-8')
            os.symlink(external_ledger, ralph_dir / 'progress.jsonl')

            errors = state_store.validate_progress_file(
                common.progress_path(str(workspace)),
                cwd=str(workspace),
            )

            self.assertEqual(len(errors), 1)
            self.assertIn('path component is a symlink', errors[0])

    def test_parse_ralph_status_accepts_single_final_block(self) -> None:
        summary = 'x' * common.SUMMARY_LIMIT
        message = f"""
Still working
---RALPH_STATUS---
STATUS: progress
SUMMARY: {summary}
FILES: a.py, b.py
CHECKS: passed:first; failed:second
---END_RALPH_STATUS---
"""
        parsed = common.parse_ralph_status(message)
        self.assertTrue(parsed['ok'])
        self.assertEqual(parsed['status'], 'progress')
        self.assertEqual(parsed['summary'], summary)
        self.assertEqual(parsed['files'], ['a.py', 'b.py'])
        self.assertEqual(parsed['checks'], ['passed:first', 'failed:second'])

    def test_completion_token_must_be_final_line_by_itself(self) -> None:
        token = common.DEFAULT_COMPLETION_TOKEN

        self.assertTrue(common.completion_token_emitted(f'wrapped up\n{token}\n', token))
        self.assertFalse(common.completion_token_emitted(f'wrapped up {token}', token))

    def test_completion_token_ignores_earlier_mentions(self) -> None:
        token = common.DEFAULT_COMPLETION_TOKEN
        message = f"""
Reminder: never print {token} early.
---RALPH_STATUS---
STATUS: progress
SUMMARY: still working
FILES:
CHECKS:
---END_RALPH_STATUS---
"""
        self.assertFalse(common.completion_token_emitted(message, token))

    def test_parse_ralph_status_ignores_inline_marker_mentions(self) -> None:
        message = (
            'Document the literal markers ---RALPH_STATUS--- and ---END_RALPH_STATUS--- in README prose.\n'
            'No real status block is present here.\n'
        )

        parsed = common.parse_ralph_status(message)

        self.assertFalse(parsed['ok'])
        self.assertEqual(parsed['error'], 'missing RALPH_STATUS block')
        self.assertFalse(common.contains_ralph_status_markup(message))

    def test_parse_trailing_ralph_status_ignores_quoted_example_block(self) -> None:
        message = (
            'Document the required format for future contributors.\n'
            '```text\n'
            '---RALPH_STATUS---\n'
            'STATUS: progress\n'
            'SUMMARY: example only\n'
            'FILES:\n'
            'CHECKS:\n'
            '---END_RALPH_STATUS---\n'
            '```\n'
        )

        parsed, attempted = common.parse_trailing_ralph_status(message)

        self.assertFalse(attempted)
        self.assertFalse(parsed['ok'])
        self.assertEqual(parsed['error'], 'missing RALPH_STATUS block')

    def test_parse_trailing_ralph_status_prefers_terminal_block_over_earlier_example(self) -> None:
        message = (
            'Document the required format for future contributors.\n'
            '```text\n'
            '---RALPH_STATUS---\n'
            'STATUS: progress\n'
            'SUMMARY: example only\n'
            'FILES:\n'
            'CHECKS:\n'
            '---END_RALPH_STATUS---\n'
            '```\n'
            '---RALPH_STATUS---\n'
            'STATUS: complete\n'
            'SUMMARY: final answer is complete\n'
            'FILES: hooks/common.py\n'
            'CHECKS: passed:python3 -m unittest\n'
            '---END_RALPH_STATUS---\n'
        )

        parsed, attempted = common.parse_trailing_ralph_status(message)

        self.assertTrue(attempted)
        self.assertTrue(parsed['ok'])
        self.assertEqual(parsed['status'], 'complete')
        self.assertEqual(parsed['files'], ['hooks/common.py'])

    def test_parse_ralph_status_rejects_multiple_blocks(self) -> None:
        message = """
---RALPH_STATUS---
STATUS: progress
SUMMARY: first
FILES:
CHECKS:
---END_RALPH_STATUS---
---RALPH_STATUS---
STATUS: blocked
SUMMARY: second
FILES:
CHECKS:
---END_RALPH_STATUS---
"""
        parsed = common.parse_ralph_status(message)
        self.assertFalse(parsed['ok'])
        self.assertIn('exactly one RALPH_STATUS block', parsed['error'])

    def test_parse_ralph_status_rejects_nonfinal_block(self) -> None:
        message = """
---RALPH_STATUS---
STATUS: progress
SUMMARY: almost there
FILES:
CHECKS:
---END_RALPH_STATUS---
More text after the block
"""
        parsed = common.parse_ralph_status(message)
        self.assertFalse(parsed['ok'])
        self.assertIn('final non-whitespace content', parsed['error'])

    def test_parse_ralph_status_rejects_internal_only_status(self) -> None:
        message = """
---RALPH_STATUS---
STATUS: cancelled
SUMMARY: no longer running
FILES:
CHECKS:
---END_RALPH_STATUS---
"""
        parsed = common.parse_ralph_status(message)
        self.assertFalse(parsed['ok'])
        self.assertIn('STATUS must be one of', parsed['error'])

    def test_parse_ralph_status_requires_all_fields(self) -> None:
        message = """
---RALPH_STATUS---
STATUS: progress
SUMMARY: missing checks
FILES: hooks/common.py
---END_RALPH_STATUS---
"""
        parsed = common.parse_ralph_status(message)
        self.assertFalse(parsed['ok'])
        self.assertIn('missing required field(s): CHECKS', parsed['error'])

    def test_parse_ralph_status_rejects_overlong_summary(self) -> None:
        message = (
            '---RALPH_STATUS---\n'
            'STATUS: progress\n'
            f'SUMMARY: {"x" * (common.SUMMARY_LIMIT + 1)}\n'
            'FILES:\n'
            'CHECKS:\n'
            '---END_RALPH_STATUS---\n'
        )

        parsed = common.parse_ralph_status(message)

        self.assertFalse(parsed['ok'])
        self.assertIn(f'SUMMARY must be <= {common.SUMMARY_LIMIT} characters', parsed['error'])

    def test_validate_progress_entry_requires_timestamp(self) -> None:
        entry = {
            'iteration': 0,
            'session_id': None,
            'status': 'started',
            'summary': 'Ralph loop started',
            'files': [],
            'checks': [],
            'message_fingerprint': None,
            'reason': None,
        }
        errors = state_store.validate_progress_entry(entry)
        self.assertIn('ts must be a non-empty ISO8601 string', errors)

    def test_validate_progress_entry_rejects_unknown_status(self) -> None:
        entry = {
            'ts': common.now_iso(),
            'iteration': 0,
            'session_id': None,
            'status': 'unknown',
            'summary': 'legacy entry',
            'files': [],
            'checks': [],
            'message_fingerprint': None,
            'reason': None,
        }

        errors = state_store.validate_progress_entry(entry)

        self.assertTrue(any(error.startswith('status must be one of') for error in errors))


if __name__ == '__main__':
    unittest.main()
