from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))

import common  # noqa: E402
import state_store  # noqa: E402
import stop_continue  # noqa: E402

STOP_SCRIPT = HOOKS_DIR / 'stop_continue.py'


def make_state(**overrides: object) -> dict[str, object]:
    state = state_store.default_state()
    state.update({
        'active': True,
        'prompt': 'Ship the feature',
        'started_at': common.now_iso(),
        'updated_at': common.now_iso(),
    })
    state.update(overrides)
    return state


class StopContinueHookTests(unittest.TestCase):
    def run_hook(self, workspace: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['python3', str(STOP_SCRIPT)],
            cwd=str(REPO_ROOT),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )

    def read_progress(self, workspace: Path) -> list[dict[str, object]]:
        ledger = common.progress_path(str(workspace))
        if not ledger.exists():
            return []
        return [
            json.loads(line)
            for line in ledger.read_text(encoding='utf-8').splitlines()
            if line.strip()
        ]

    def read_state(self, workspace: Path) -> dict[str, object]:
        return json.loads(common.state_path(str(workspace)).read_text(encoding='utf-8'))

    def write_delayed_read_state_worker(self, root: Path, delay_seconds: float) -> Path:
        worker = root / '.tmp_stop_continue_worker.py'
        worker.write_text(
            textwrap.dedent(
                f'''\
                import io
                import json
                import sys
                import time
                from pathlib import Path

                root = Path(sys.argv[1])
                sys.path.insert(0, str(root / 'hooks'))

                import stop_continue

                original_read_state = stop_continue.read_state

                def delayed_read_state(cwd=None):
                    result = original_read_state(cwd)
                    time.sleep({delay_seconds!r})
                    return result

                stop_continue.read_state = delayed_read_state
                payload = json.loads(sys.argv[2])
                sys.stdin = io.StringIO(json.dumps(payload))
                raise SystemExit(stop_continue.main())
                '''
            ),
            encoding='utf-8',
        )
        return worker

    def test_completion_token_clears_state_and_records_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    'done\n'
                    '---RALPH_STATUS---\n'
                    'STATUS: complete\n'
                    'SUMMARY: wrapped up the task\n'
                    'FILES: hooks/common.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    '<promise>DONE</promise>'
                ),
            }

            result = self.run_hook(workspace, payload)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(common.state_path(str(workspace)).exists())

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'complete')
            self.assertEqual(entries[-1]['files'], ['hooks/common.py'])

    def test_missing_state_is_noop_without_creating_storage_in_read_only_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            os.chmod(workspace, 0o500)
            try:
                result = self.run_hook(workspace, {
                    'cwd': str(workspace),
                    'session_id': 'session-1',
                    'last_assistant_message': 'ignored',
                })
            finally:
                os.chmod(workspace, 0o700)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertFalse((workspace / '.codex').exists())

    def test_missing_state_ignores_session_payload_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': None,
                'last_assistant_message': ['not', 'a', 'string'],
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            self.assertEqual(result.stdout, '')
            self.assertFalse((workspace / '.codex').exists())

    def test_completion_token_without_status_block_still_records_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'wrapped up the task\n<promise>DONE</promise>',
            })

            self.assertEqual(result.returncode, 0)
            self.assertFalse(common.state_path(str(workspace)).exists())
            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'complete')
            self.assertEqual(entries[-1]['files'], [])

    def test_completion_token_allows_inline_status_marker_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    'Updated README to explain ---RALPH_STATUS--- and ---END_RALPH_STATUS--- markers.\n'
                    '<promise>DONE</promise>'
                ),
            })

            self.assertEqual(result.returncode, 0)
            self.assertFalse(common.state_path(str(workspace)).exists())

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'complete')
            self.assertEqual(
                entries[-1]['summary'],
                'Updated README to explain ---RALPH_STATUS--- and ---END_RALPH_STATUS--- markers. '
                '<promise>DONE</promise>',
            )

    def test_completion_token_ignores_quoted_status_example_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    'Document the required format for future contributors.\n'
                    '```text\n'
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: example only\n'
                    'FILES:\n'
                    'CHECKS:\n'
                    '---END_RALPH_STATUS---\n'
                    '```\n'
                    '<promise>DONE</promise>'
                ),
            })

            self.assertEqual(result.returncode, 0)
            self.assertFalse(common.state_path(str(workspace)).exists())
            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'complete')

    def test_completion_token_ignores_indented_status_example_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    'Document the required format for future contributors.\n'
                    '    ---RALPH_STATUS---\n'
                    '    STATUS: progress\n'
                    '    SUMMARY: example only\n'
                    '    FILES:\n'
                    '    CHECKS:\n'
                    '    ---END_RALPH_STATUS---\n'
                    '<promise>DONE</promise>'
                ),
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertFalse(common.state_path(str(workspace)).exists())
            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'complete')

    def test_completion_token_prefers_terminal_status_block_over_earlier_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
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
                    'SUMMARY: wrapped up the task\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    '<promise>DONE</promise>'
                ),
            })

            self.assertEqual(result.returncode, 0)
            self.assertFalse(common.state_path(str(workspace)).exists())
            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'complete')
            self.assertEqual(entries[-1]['files'], ['hooks/stop_continue.py'])

    def test_inline_completion_token_reference_does_not_complete_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    'Reminder: never print <promise>DONE</promise> until the task is done.\n'
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: still working\n'
                    'FILES: hooks/common.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertEqual(response['decision'], 'block')
            state = self.read_state(workspace)
            self.assertEqual(state['iteration'], 1)

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'progress')
            self.assertTrue(common.state_path(str(workspace)).exists())

    def test_unfinished_turn_rejects_trailing_fenced_code_after_status_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: still working\n'
                    'FILES: hooks/common.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    '```text\n'
                    'extra trailing content\n'
                    '```\n'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('final non-whitespace content', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_completion_token_rejects_trailing_indented_content_after_status_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: complete\n'
                    'SUMMARY: wrapped up the task\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    '    extra trailing content\n'
                    '<promise>DONE</promise>'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('non-terminal RALPH_STATUS markup', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_legacy_state_shape_continues_and_is_upgraded_on_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 0,
                'max_iterations': 3,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': None,
            }) + '\n', encoding='utf-8')

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: still working\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertEqual(response['decision'], 'block')
            state = self.read_state(workspace)
            self.assertEqual(state['iteration'], 1)
            self.assertEqual(state['phase'], 'running')
            self.assertEqual(state['repeat_count'], 1)

    def test_completion_token_conflicting_with_noncomplete_status_pauses_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: says progress but emits token\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    '<promise>DONE</promise>'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('STATUS=progress', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'completion_status_mismatch')

    def test_completion_token_with_malformed_status_block_pauses_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS complete\n'
                    'SUMMARY: malformed status line\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    '<promise>DONE</promise>'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('RALPH_STATUS block was malformed', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_completion_token_rejects_nonterminal_status_block_before_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: not actually done yet\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    'Extra text after the status block.\n'
                    '<promise>DONE</promise>'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('non-terminal RALPH_STATUS markup', response['systemMessage'])
            self.assertIn('final non-whitespace content', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_blocked_status_stops_loop_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: blocked\n'
                    'SUMMARY: waiting for credentials\n'
                    'FILES: \n'
                    'CHECKS: failed:missing credentials\n'
                    '---END_RALPH_STATUS---'
                ),
            }

            result = self.run_hook(workspace, payload)
            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertIn('STATUS=blocked', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'blocked')
            self.assertEqual(entries[-1]['reason'], 'awaiting_user_input')

    def test_max_iterations_preempts_blocked_status_and_clears_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(iteration=2, max_iterations=2), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: blocked\n'
                    'SUMMARY: waiting for credentials\n'
                    'FILES:\n'
                    'CHECKS: failed:missing credentials\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('max_iterations=2', response['systemMessage'])
            self.assertFalse(common.state_path(str(workspace)).exists())

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'max_iterations')

    def test_missing_status_block_stops_loop_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'still working on the task',
            })

            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertIn('valid RALPH_STATUS block', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_indented_status_example_block_does_not_continue_unfinished_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    'Document the required format for future contributors.\n'
                    '    ---RALPH_STATUS---\n'
                    '    STATUS: progress\n'
                    '    SUMMARY: example only\n'
                    '    FILES:\n'
                    '    CHECKS:\n'
                    '    ---END_RALPH_STATUS---\n'
                ),
            })

            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertIn('valid RALPH_STATUS block', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_status_block_must_be_final_on_unfinished_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: almost there\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---\n'
                    'extra text'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('valid RALPH_STATUS block', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_invalid_status_value_stops_loop_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: cancelled\n'
                    'SUMMARY: not a user-facing status\n'
                    'FILES:\n'
                    'CHECKS:\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('STATUS must be one of', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_unknown_status_field_stops_loop_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: still working\n'
                    'FILES:\n'
                    'CHECKS:\n'
                    'EXTRA: should_fail\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('unknown EXTRA field', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_overlong_summary_stops_loop_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    f'SUMMARY: {"x" * (common.SUMMARY_LIMIT + 1)}\n'
                    'FILES:\n'
                    'CHECKS:\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('SUMMARY must be <=', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'invalid_status_block')

    def test_status_complete_without_completion_token_stops_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: complete\n'
                    'SUMMARY: says complete without token\n'
                    'FILES: hooks/common.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('STATUS=complete without emitting', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['reason'], 'missing_completion_token')

    def test_repeated_responses_trip_circuit_after_three_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: still trying the same thing\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---'
                ),
            }

            first = self.run_hook(workspace, payload)
            self.assertEqual(json.loads(first.stdout)['decision'], 'block')
            second = self.run_hook(workspace, payload)
            self.assertEqual(json.loads(second.stdout)['decision'], 'block')
            third = self.run_hook(workspace, payload)
            response = json.loads(third.stdout)
            self.assertIn('same assistant response three times', response['systemMessage'])
            self.assertTrue(common.state_path(str(workspace)).exists())
            state = self.read_state(workspace)
            self.assertEqual(state['phase'], 'blocked')
            self.assertEqual(state['repeat_count'], 3)

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'repeated_response')

    def test_concurrent_sessions_do_not_double_claim_same_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))
            worker = self.write_delayed_read_state_worker(REPO_ROOT, 0.4)

            try:
                processes: list[subprocess.Popen[str]] = []
                for session_id, summary in (
                    ('session-a', 'summary from A'),
                    ('session-b', 'summary from B'),
                ):
                    payload = {
                        'cwd': str(workspace),
                        'session_id': session_id,
                        'last_assistant_message': (
                            '---RALPH_STATUS---\n'
                            'STATUS: progress\n'
                            f'SUMMARY: {summary}\n'
                            'FILES: hooks/stop_continue.py\n'
                            'CHECKS: passed:python3 -m unittest\n'
                            '---END_RALPH_STATUS---'
                        ),
                    }
                    processes.append(
                        subprocess.Popen(
                            ['python3', str(worker), str(REPO_ROOT), json.dumps(payload)],
                            cwd=str(REPO_ROOT),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                    )

                results = [process.communicate() for process in processes]
            finally:
                worker.unlink(missing_ok=True)

            self.assertEqual([process.returncode for process in processes], [0, 0])
            self.assertTrue(all(not stderr for _, stderr in results))

            non_empty_stdout = [stdout for stdout, _ in results if stdout]
            self.assertEqual(len(non_empty_stdout), 1)
            response = json.loads(non_empty_stdout[0])
            self.assertEqual(response['decision'], 'block')

            entries = self.read_progress(workspace)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['status'], 'progress')

            state = self.read_state(workspace)
            self.assertEqual(state['iteration'], 1)
            self.assertEqual(state['claimed_session_id'], entries[0]['session_id'])
            self.assertIn(state['claimed_session_id'], {'session-a', 'session-b'})

    def test_max_iterations_preempts_repeated_response_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            message = (
                '---RALPH_STATUS---\n'
                'STATUS: progress\n'
                'SUMMARY: still trying the same thing\n'
                'FILES: hooks/stop_continue.py\n'
                'CHECKS: passed:python3 -m unittest\n'
                '---END_RALPH_STATUS---'
            )
            state_store.save_state(make_state(
                iteration=2,
                max_iterations=2,
                last_message_fingerprint=common.fingerprint_message(message),
                repeat_count=2,
            ), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': message,
            })

            response = json.loads(result.stdout)
            self.assertIn('max_iterations=2', response['systemMessage'])
            self.assertFalse(common.state_path(str(workspace)).exists())

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'max_iterations')

    def test_progress_write_failure_returns_storage_error_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))
            common.progress_path(str(workspace)).mkdir(parents=True, exist_ok=True)

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: blocked\n'
                    'SUMMARY: waiting for credentials\n'
                    'FILES:\n'
                    'CHECKS: failed:missing credentials\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('storage is unavailable', response['systemMessage'])
            self.assertIn('progress ledger is invalid', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')
            self.assertTrue(common.progress_path(str(workspace)).is_dir())

    def test_progress_write_failure_does_not_advance_loop_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            initial_state = make_state()
            state_store.save_state(initial_state, str(workspace))
            common.progress_path(str(workspace)).mkdir(parents=True, exist_ok=True)

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: still working\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('storage is unavailable', response['systemMessage'])
            self.assertIn('progress ledger is invalid', response['systemMessage'])
            self.assertEqual(self.read_state(workspace), initial_state)

    def test_invalid_existing_progress_ledger_returns_storage_error_without_appending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            initial_state = make_state()
            state_store.save_state(initial_state, str(workspace))
            ledger = common.progress_path(str(workspace))
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text('not-json\n', encoding='utf-8')

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: still working\n'
                    'FILES: hooks/stop_continue.py\n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            response = json.loads(result.stdout)
            self.assertIn('storage is unavailable', response['systemMessage'])
            self.assertIn('progress ledger is invalid', response['systemMessage'])
            self.assertEqual(self.read_state(workspace), initial_state)
            self.assertEqual(ledger.read_text(encoding='utf-8'), 'not-json\n')

    def test_pause_loop_with_reason_does_not_append_progress_when_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            details = {
                'summary': 'need input',
                'files': [],
                'checks': ['failed:waiting for user input'],
            }

            with mock.patch.object(
                stop_continue,
                'save_state',
                side_effect=state_store.StorageError('boom'),
            ) as save_mock, mock.patch.object(stop_continue, 'append_progress_entry') as append_mock:
                with self.assertRaises(state_store.StorageError):
                    stop_continue.pause_loop_with_reason(
                        state=make_state(),
                        cwd=str(workspace),
                        iteration=1,
                        session_id='session-1',
                        message_fingerprint='sha256:test',
                        details=details,
                        reason='invalid_status_block',
                        message='stop here',
                    )

            save_mock.assert_called_once()
            append_mock.assert_not_called()

    def test_paused_loop_is_noop_until_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(phase='blocked'), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: should be ignored while paused\n'
                    'FILES:\n'
                    'CHECKS:\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            self.assertEqual(result.stdout, '')
            self.assertEqual(self.read_progress(workspace), [])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')

    def test_session_mismatch_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(claimed_session_id='session-a'), str(workspace))
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-b',
                'last_assistant_message': 'anything',
            }

            result = self.run_hook(workspace, payload)
            self.assertEqual(result.stdout, '')
            self.assertEqual(self.read_progress(workspace), [])

    def test_missing_session_id_payload_stops_cleanly_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            original_state = make_state()
            state_store.save_state(original_state, str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': None,
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: progress\n'
                    'SUMMARY: should be rejected without a session id\n'
                    'FILES:\n'
                    'CHECKS:\n'
                    '---END_RALPH_STATUS---'
                ),
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            response = json.loads(result.stdout)
            self.assertIn('session_id must be a non-empty string', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace), [])
            self.assertEqual(self.read_state(workspace), original_state)

    def test_invalid_state_json_returns_error_without_progress_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text('{invalid\n', encoding='utf-8')

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'ignored',
            })

            response = json.loads(result.stdout)
            self.assertIn('invalid JSON', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace), [])

    def test_unreadable_state_path_returns_storage_error_without_progress_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.mkdir(parents=True, exist_ok=True)

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'ignored',
            })

            response = json.loads(result.stdout)
            self.assertIn('storage is unavailable', response['systemMessage'])
            self.assertIn('unable to read', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace), [])

    def test_invalid_state_schema_returns_error_without_progress_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 'oops',
                'max_iterations': 3,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': None,
                'phase': 'running',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
                'last_message_fingerprint': None,
                'repeat_count': 0,
            }) + '\n', encoding='utf-8')

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'ignored',
            })

            response = json.loads(result.stdout)
            self.assertIn('Ralph state is invalid.', response['systemMessage'])
            self.assertIn('iteration must be an integer', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace), [])

    def test_invalid_phase_schema_returns_error_without_silent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 1,
                'max_iterations': 3,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': None,
                'phase': 'oops',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
                'last_message_fingerprint': None,
                'repeat_count': 0,
            }) + '\n', encoding='utf-8')

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'ignored',
            })

            response = json.loads(result.stdout)
            self.assertIn('phase must be one of', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace), [])

    def test_invalid_cwd_payload_stops_cleanly_without_traceback(self) -> None:
        result = subprocess.run(
            ['python3', str(STOP_SCRIPT)],
            cwd=str(REPO_ROOT),
            input=json.dumps({
                'cwd': 123,
                'session_id': 'session-1',
                'last_assistant_message': None,
            }),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')
        response = json.loads(result.stdout)
        self.assertIn('cwd must be a non-empty string', response['systemMessage'])

    def test_invalid_json_payload_stops_cleanly_without_traceback(self) -> None:
        result = subprocess.run(
            ['python3', str(STOP_SCRIPT)],
            cwd=str(REPO_ROOT),
            input='{invalid\n',
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')
        response = json.loads(result.stdout)
        self.assertIn('invalid JSON payload', response['systemMessage'])

    def test_empty_session_id_payload_stops_cleanly_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            original_state = make_state()
            state_store.save_state(original_state, str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': '',
                'last_assistant_message': 'ignored',
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            response = json.loads(result.stdout)
            self.assertIn('session_id must be a non-empty string', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace), [])
            self.assertEqual(self.read_state(workspace), original_state)

    def test_invalid_last_assistant_message_payload_stops_cleanly_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(), str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': ['not', 'a', 'string'],
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            response = json.loads(result.stdout)
            self.assertIn('last_assistant_message must be a string or null', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace), [])

    def test_missing_workspace_path_returns_storage_error_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / 'missing-workspace'

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': None,
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            response = json.loads(result.stdout)
            self.assertIn('workspace path does not exist', response['systemMessage'])
            self.assertFalse(workspace.exists())

    def test_max_iterations_stops_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_store.save_state(make_state(iteration=2, max_iterations=2), str(workspace))
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': (
                    '---RALPH_STATUS---\n'
                    'STATUS: no_progress\n'
                    'SUMMARY: no change this turn\n'
                    'FILES: \n'
                    'CHECKS: passed:python3 -m unittest\n'
                    '---END_RALPH_STATUS---'
                ),
            }

            result = self.run_hook(workspace, payload)
            response = json.loads(result.stdout)
            self.assertIn('max_iterations=2', response['systemMessage'])
            self.assertFalse(common.state_path(str(workspace)).exists())

            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'stopped')
            self.assertEqual(entries[-1]['reason'], 'max_iterations')


if __name__ == '__main__':
    unittest.main()
