from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / 'tests'
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from ralph_core import control as loop_control  # noqa: E402
from ralph_core import storage as state_store  # noqa: E402
from ralph_test_helpers import common  # noqa: E402


class LoopControlTests(unittest.TestCase):
    def read_progress(self, workspace: Path) -> list[dict[str, object]]:
        ledger = common.progress_path(str(workspace))
        if not ledger.exists():
            return []
        return [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_start_loop_writes_state_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            result = loop_control.start_loop(
                cwd=str(workspace),
                prompt='Ship the feature',
                max_iterations=5,
            )

            self.assertEqual(result['status'], 'started')
            state_result = state_store.read_state(str(workspace))
            self.assertEqual(state_result.status, 'ok')
            assert state_result.value is not None
            self.assertEqual(state_result.value['prompt'], 'Ship the feature')
            self.assertEqual(state_result.value['iteration'], 0)
            self.assertEqual(state_result.value['max_iterations'], 5)
            self.assertEqual(state_result.value['phase'], 'running')
            self.assertIsNone(state_result.value['pending_update'])

            entries = self.read_progress(workspace)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['status'], 'started')
            self.assertEqual(entries[0]['summary'], 'Ralph loop started')

    def test_start_loop_validates_request_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with mock.patch.object(
                loop_control,
                '_validate_start_request',
                wraps=loop_control._validate_start_request,
            ) as validate_mock:
                result = loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=5,
                )

            self.assertEqual(result['status'], 'started')
            validate_mock.assert_called_once_with('Ship the feature', 5)

    def test_start_loop_rolls_back_state_when_progress_append_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            ledger = common.progress_path(str(workspace))
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text('not-json\n', encoding='utf-8')

            with self.assertRaises(state_store.StorageError):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=3,
                )

            self.assertFalse(common.state_path(str(workspace)).exists())

    def test_start_loop_rejects_existing_state_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            original_state = state_store.default_state()
            original_state.update({
                'prompt': 'Finish the blocked task',
                'iteration': 4,
                'max_iterations': 6,
                'claimed_session_id': 'session-a',
                'phase': 'blocked',
            })
            state_store.save_state(original_state, str(workspace))

            with self.assertRaisesRegex(ValueError, 'A Ralph loop state already exists'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=3,
                )

            restored_state = state_store.read_state(str(workspace))
            self.assertEqual(restored_state.status, 'ok')
            self.assertEqual(restored_state.value, original_state)
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_start_loop_rejects_invalid_existing_state_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            original = '{invalid\n'
            state_file.write_text(original, encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'Ralph state is invalid JSON'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=3,
                )

            self.assertEqual(state_file.read_text(encoding='utf-8'), original)
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_start_loop_rejects_blank_prompt_without_creating_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with self.assertRaisesRegex(ValueError, 'prompt must not be empty'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='   ',
                    max_iterations=3,
                )

            self.assertFalse((workspace / common.LOCK_RELATIVE_PATH).exists())
            self.assertFalse(common.state_path(str(workspace)).exists())
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_report_loop_writes_pending_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            loop_control.start_loop(cwd=str(workspace), prompt='Ship it', max_iterations=5)

            result = loop_control.report_loop(
                cwd=str(workspace),
                session_id='session-1',
                status='progress',
                summary='updated docs',
                files=['README.md'],
                checks=['passed:unit'],
            )

            self.assertEqual(result['status'], 'reported')
            state = state_store.read_state(str(workspace)).value
            assert state is not None
            assert state['pending_update'] is not None
            self.assertEqual(state['pending_update']['status'], 'progress')
            self.assertEqual(state['pending_update']['session_id'], 'session-1')
            self.assertEqual(state['pending_update']['files'], ['README.md'])
            self.assertEqual(state['claimed_session_id'], 'session-1')

    def test_report_loop_rejects_different_claimed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'prompt': 'Ship it',
                'claimed_session_id': 'session-a',
            })
            state_store.save_state(state, str(workspace))

            with self.assertRaisesRegex(ValueError, 'claimed by a different Codex session'):
                loop_control.report_loop(
                    cwd=str(workspace),
                    session_id='session-b',
                    status='complete',
                    summary='stale completion',
                )

            unchanged_state = state_store.read_state(str(workspace)).value
            assert unchanged_state is not None
            self.assertIsNone(unchanged_state['pending_update'])
            self.assertEqual(unchanged_state['claimed_session_id'], 'session-a')

    def test_report_loop_requires_reason_for_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            loop_control.start_loop(cwd=str(workspace), prompt='Ship it', max_iterations=5)

            with self.assertRaisesRegex(ValueError, 'reason is required'):
                loop_control.report_loop(
                    cwd=str(workspace),
                    session_id='session-1',
                    status='blocked',
                    summary='waiting on approval',
                )

    def test_report_loop_rejects_non_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'prompt': 'Ship it',
                'phase': 'blocked',
            })
            state_store.save_state(state, str(workspace))

            with self.assertRaisesRegex(ValueError, 'is not currently running'):
                loop_control.report_loop(
                    cwd=str(workspace),
                    session_id='session-1',
                    status='progress',
                    summary='updated docs',
                )

    def test_resume_loop_clears_pending_update_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'prompt': 'Ship it',
                'phase': 'blocked',
                'claimed_session_id': 'session-a',
                'pending_update': loop_control.build_pending_update(
                    iteration=0,
                    session_id='session-a',
                    status='blocked',
                    summary='waiting on approval',
                    reason='needs approval',
                ),
                'repeat_count': 2,
                'last_message_fingerprint': 'sha256:stale',
            })
            state_store.save_state(state, str(workspace))

            result = loop_control.resume_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'resumed')
            resumed_state = state_store.read_state(str(workspace)).value
            assert resumed_state is not None
            self.assertEqual(resumed_state['phase'], 'running')
            self.assertIsNone(resumed_state['pending_update'])
            self.assertIsNone(resumed_state['claimed_session_id'])
            self.assertEqual(resumed_state['repeat_count'], 0)
            self.assertIsNone(resumed_state['last_message_fingerprint'])
            self.assertEqual(self.read_progress(workspace)[-1]['status'], 'resumed')

    def test_cancel_loop_clears_state_and_appends_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            loop_control.start_loop(cwd=str(workspace), prompt='Ship it', max_iterations=5)

            result = loop_control.cancel_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'cleared')
            self.assertFalse(common.state_path(str(workspace)).exists())
            self.assertEqual(self.read_progress(workspace)[-1]['status'], 'cancelled')

    def test_snapshot_state_rejects_state_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            target = workspace / 'external-state.json'
            state_file.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}\n', encoding='utf-8')
            os.symlink(target, state_file)

            with self.assertRaisesRegex(state_store.StorageError, 'path component is a symlink'):
                loop_control.snapshot_state(str(workspace))
