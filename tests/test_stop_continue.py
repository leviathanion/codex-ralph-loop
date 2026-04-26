from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / 'tests'
HOOKS_DIR = REPO_ROOT / 'hooks'
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_ROOT))
sys.path.insert(0, str(HOOKS_DIR))

from ralph_core import control as loop_control  # noqa: E402
from ralph_core import storage as state_store  # noqa: E402
from ralph_test_helpers import common  # noqa: E402

STOP_SCRIPT = HOOKS_DIR / 'stop_continue.py'


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

    def start_loop(self, workspace: Path, *, max_iterations: int = 5) -> None:
        loop_control.start_loop(
            cwd=str(workspace),
            prompt='Ship the feature',
            max_iterations=max_iterations,
        )

    def test_missing_state_is_noop_without_creating_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'ignored',
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertFalse((workspace / '.codex').exists())

    def test_pending_complete_clears_state_and_records_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace)
            loop_control.report_loop(
                cwd=str(workspace),
                session_id='session-1',
                status='complete',
                summary='wrapped up the task',
                files=['README.md'],
                checks=['passed:python3 -m unittest'],
            )

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'done',
            })

            self.assertEqual(result.returncode, 0)
            self.assertFalse(common.state_path(str(workspace)).exists())
            entries = self.read_progress(workspace)
            self.assertEqual(entries[-1]['status'], 'complete')
            self.assertEqual(entries[-1]['files'], ['README.md'])

    def test_pending_blocked_pauses_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace)
            loop_control.report_loop(
                cwd=str(workspace),
                session_id='session-1',
                status='blocked',
                summary='waiting on review',
                reason='needs user approval',
            )

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'blocked here',
            })

            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertIn('reported a blocking dependency', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')
            self.assertEqual(self.read_progress(workspace)[-1]['status'], 'blocked')

    def test_pending_failed_pauses_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace)
            loop_control.report_loop(
                cwd=str(workspace),
                session_id='session-1',
                status='failed',
                summary='test suite is red',
                reason='pytest -q failed',
            )

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'cannot proceed',
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(self.read_state(workspace)['phase'], 'failed')
            self.assertEqual(self.read_progress(workspace)[-1]['status'], 'failed')

    def test_pending_progress_continues_and_increments_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace)
            loop_control.report_loop(
                cwd=str(workspace),
                session_id='session-1',
                status='progress',
                summary='updated docs',
                files=['README.md'],
                checks=['passed:unit'],
            )

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'more work to do',
            })

            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertEqual(response['decision'], 'block')
            state = self.read_state(workspace)
            self.assertEqual(state['phase'], 'running')
            self.assertEqual(state['iteration'], 1)
            self.assertIsNone(state['pending_update'])
            self.assertEqual(self.read_progress(workspace)[-1]['summary'], 'updated docs')

    def test_missing_pending_update_uses_message_fallback_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace)

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'edited docs and ran tests',
            })

            self.assertEqual(result.returncode, 0)
            state = self.read_state(workspace)
            self.assertEqual(state['iteration'], 1)
            self.assertEqual(self.read_progress(workspace)[-1]['summary'], 'edited docs and ran tests')

    def test_same_message_three_times_pauses_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace, max_iterations=10)
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'still working',
            }

            self.run_hook(workspace, payload)
            self.run_hook(workspace, payload)
            result = self.run_hook(workspace, payload)

            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertIn('same assistant response three times', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')
            self.assertEqual(self.read_progress(workspace)[-1]['reason'], 'repeated_response')

    def test_distinct_progress_reports_with_same_message_do_not_pause_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace, max_iterations=10)
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'still working',
            }

            for index, summary in enumerate(('updated docs', 'wired tests', 'handled edge case')):
                loop_control.report_loop(
                    cwd=str(workspace),
                    session_id='session-1',
                    status='progress',
                    summary=summary,
                    files=[f'file-{index}.md'],
                )
                result = self.run_hook(workspace, payload)
                self.assertEqual(result.returncode, 0)
                response = json.loads(result.stdout)
                self.assertEqual(response['decision'], 'block')

            state = self.read_state(workspace)
            self.assertEqual(state['phase'], 'running')
            self.assertEqual(state['iteration'], 3)
            self.assertEqual(state['repeat_count'], 1)
            self.assertEqual(self.read_progress(workspace)[-1]['summary'], 'handled edge case')

    def test_same_progress_report_and_message_still_pauses_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace, max_iterations=10)
            payload = {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'still working',
            }

            for _ in range(2):
                loop_control.report_loop(
                    cwd=str(workspace),
                    session_id='session-1',
                    status='progress',
                    summary='same progress',
                    files=['README.md'],
                )
                self.run_hook(workspace, payload)

            loop_control.report_loop(
                cwd=str(workspace),
                session_id='session-1',
                status='progress',
                summary='same progress',
                files=['README.md'],
            )
            result = self.run_hook(workspace, payload)

            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertIn('same assistant response three times', response['systemMessage'])
            self.assertEqual(self.read_state(workspace)['phase'], 'blocked')
            self.assertEqual(self.read_progress(workspace)[-1]['reason'], 'repeated_response')

    def test_stale_session_report_cannot_complete_claimed_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace, max_iterations=10)

            first_result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-a',
                'last_assistant_message': 'claimed by session a',
            })
            self.assertEqual(first_result.returncode, 0)

            with self.assertRaisesRegex(ValueError, 'claimed by a different Codex session'):
                loop_control.report_loop(
                    cwd=str(workspace),
                    session_id='session-b',
                    status='complete',
                    summary='stale completion',
                )

            second_result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-a',
                'last_assistant_message': 'still working',
            })

            self.assertEqual(second_result.returncode, 0)
            self.assertTrue(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_state(workspace)['iteration'], 2)
            self.assertNotEqual(self.read_progress(workspace)[-1]['status'], 'complete')

    def test_max_iterations_stops_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace, max_iterations=0 + 1)
            state = self.read_state(workspace)
            state['iteration'] = 1
            common.save_state(state, str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'still working',
            })

            self.assertEqual(result.returncode, 0)
            self.assertFalse(common.state_path(str(workspace)).exists())
            response = json.loads(result.stdout)
            self.assertIn('max_iterations=1', response['systemMessage'])
            self.assertEqual(self.read_progress(workspace)[-1]['reason'], 'max_iterations')

    def test_stale_pending_update_pauses_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace)
            state = self.read_state(workspace)
            state['claimed_session_id'] = 'session-1'
            state['pending_update'] = {
                'iteration': 99,
                'session_id': 'session-1',
                'status': 'progress',
                'summary': 'stale write',
                'files': [],
                'checks': [],
                'reason': None,
                'updated_at': common.now_iso(),
            }
            common.state_path(str(workspace)).write_text(json.dumps(state) + '\n', encoding='utf-8')

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-1',
                'last_assistant_message': 'ignored',
            })

            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertIn('Ralph state is invalid', response['systemMessage'])

    def test_session_mismatch_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.start_loop(workspace)
            state = self.read_state(workspace)
            state['claimed_session_id'] = 'session-a'
            common.save_state(state, str(workspace))

            result = self.run_hook(workspace, {
                'cwd': str(workspace),
                'session_id': 'session-b',
                'last_assistant_message': 'ignored',
            })

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertEqual(self.read_state(workspace)['claimed_session_id'], 'session-a')
