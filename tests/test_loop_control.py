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
import loop_control  # noqa: E402
import state_store  # noqa: E402


class LoopControlTests(unittest.TestCase):
    def test_start_loop_writes_state_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            result = loop_control.start_loop(
                cwd=str(workspace),
                prompt='Ship the feature',
                max_iterations=5,
                completion_token='<promise>DONE</promise>',
            )

            self.assertEqual(result['status'], 'started')
            state_result = state_store.read_state(str(workspace))
            self.assertEqual(state_result.status, 'ok')
            assert state_result.value is not None
            self.assertTrue(state_result.value['active'])
            self.assertEqual(state_result.value['prompt'], 'Ship the feature')
            self.assertEqual(state_result.value['iteration'], 0)
            self.assertEqual(state_result.value['max_iterations'], 5)
            self.assertEqual(state_result.value['phase'], 'running')

            entries = [
                json.loads(line)
                for line in common.progress_path(str(workspace)).read_text(encoding='utf-8').splitlines()
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['status'], 'started')
            self.assertEqual(entries[0]['summary'], 'Ralph loop started')

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
                    completion_token='<promise>DONE</promise>',
                )

            self.assertFalse(common.state_path(str(workspace)).exists())

    def test_start_loop_restores_prior_state_when_progress_append_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            original_state = state_store.default_state()
            original_state.update({
                'active': True,
                'prompt': 'Finish the blocked task',
                'iteration': 4,
                'max_iterations': 6,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': 'session-a',
                'phase': 'blocked',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
                'last_message_fingerprint': 'sha256:stale',
                'repeat_count': 3,
            })
            state_store.save_state(original_state, str(workspace))
            ledger = common.progress_path(str(workspace))
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text('not-json\n', encoding='utf-8')

            with self.assertRaises(state_store.StorageError):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=3,
                    completion_token='<promise>DONE</promise>',
                )

            restored_state = state_store.read_state(str(workspace))
            self.assertEqual(restored_state.status, 'ok')
            self.assertEqual(restored_state.value, original_state)

    def test_resume_loop_reclaims_unclaimed_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 2,
                'max_iterations': 5,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': None,
                'phase': 'running',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
                'last_message_fingerprint': 'sha256:stale',
                'repeat_count': 2,
            })
            state_store.save_state(state, str(workspace))

            result = loop_control.resume_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'resumed')
            resumed_state = state_store.read_state(str(workspace))
            self.assertEqual(resumed_state.status, 'ok')
            assert resumed_state.value is not None
            self.assertIsNone(resumed_state.value['claimed_session_id'])
            self.assertEqual(resumed_state.value['phase'], 'running')
            self.assertIsNone(resumed_state.value['last_message_fingerprint'])
            self.assertEqual(resumed_state.value['repeat_count'], 0)

            entries = [
                json.loads(line)
                for line in common.progress_path(str(workspace)).read_text(encoding='utf-8').splitlines()
            ]
            self.assertEqual(entries[-1]['status'], 'resumed')
            self.assertEqual(entries[-1]['reason'], 'orphaned_running_state')

    def test_resume_loop_reclaims_running_session_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 2,
                'max_iterations': 5,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': 'session-a',
                'phase': 'running',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
                'last_message_fingerprint': 'sha256:stale',
                'repeat_count': 2,
            })
            state_store.save_state(state, str(workspace))

            result = loop_control.resume_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'resumed')
            resumed_state = state_store.read_state(str(workspace))
            self.assertEqual(resumed_state.status, 'ok')
            assert resumed_state.value is not None
            self.assertIsNone(resumed_state.value['claimed_session_id'])
            self.assertEqual(resumed_state.value['phase'], 'running')
            self.assertIsNone(resumed_state.value['last_message_fingerprint'])
            self.assertEqual(resumed_state.value['repeat_count'], 0)

            entries = [
                json.loads(line)
                for line in common.progress_path(str(workspace)).read_text(encoding='utf-8').splitlines()
            ]
            self.assertEqual(entries[-1]['status'], 'resumed')
            self.assertEqual(entries[-1]['reason'], 'session_reclaimed')

    def test_resume_loop_reclaims_legacy_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 2,
                'max_iterations': 5,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': 'session-a',
            }) + '\n', encoding='utf-8')

            result = loop_control.resume_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'resumed')
            restored_state = state_store.read_state(str(workspace))
            self.assertEqual(restored_state.status, 'ok')
            assert restored_state.value is not None
            self.assertEqual(restored_state.value['phase'], 'running')
            self.assertIsNone(restored_state.value['claimed_session_id'])
            self.assertEqual(restored_state.value['repeat_count'], 0)

    def test_resume_loop_resets_repeat_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            original_updated_at = common.now_iso()
            state = state_store.default_state()
            state.update({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 2,
                'max_iterations': 5,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': 'session-a',
                'phase': 'blocked',
                'started_at': common.now_iso(),
                'updated_at': original_updated_at,
                'last_message_fingerprint': 'sha256:stale',
                'repeat_count': 2,
            })
            state_store.save_state(state, str(workspace))

            result = loop_control.resume_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'resumed')
            self.assertEqual(result['prompt'], 'Ship the feature')
            resumed_state = state_store.read_state(str(workspace))
            self.assertEqual(resumed_state.status, 'ok')
            assert resumed_state.value is not None
            self.assertIsNone(resumed_state.value['claimed_session_id'])
            self.assertEqual(resumed_state.value['phase'], 'running')
            self.assertIsNone(resumed_state.value['last_message_fingerprint'])
            self.assertEqual(resumed_state.value['repeat_count'], 0)
            self.assertNotEqual(resumed_state.value['updated_at'], original_updated_at)

            entries = [
                json.loads(line)
                for line in common.progress_path(str(workspace)).read_text(encoding='utf-8').splitlines()
            ]
            self.assertEqual(entries[-1]['status'], 'resumed')
            self.assertEqual(entries[-1]['iteration'], 2)

    def test_resume_loop_restores_prior_state_when_progress_append_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'active': True,
                'prompt': 'Ship the feature',
                'iteration': 2,
                'max_iterations': 5,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': 'session-a',
                'phase': 'blocked',
                'started_at': common.now_iso(),
                'updated_at': common.now_iso(),
                'last_message_fingerprint': 'sha256:stale',
                'repeat_count': 2,
            })
            state_store.save_state(state, str(workspace))
            ledger = common.progress_path(str(workspace))
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text('not-json\n', encoding='utf-8')

            with self.assertRaises(state_store.StorageError):
                loop_control.resume_loop(cwd=str(workspace))

            restored_state = state_store.read_state(str(workspace))
            self.assertEqual(restored_state.status, 'ok')
            self.assertEqual(restored_state.value, state)

    def test_cancel_loop_clears_invalid_state_without_progress_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text('{invalid\n', encoding='utf-8')

            result = loop_control.cancel_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'cleared_invalid_state')
            self.assertFalse(state_file.exists())
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_cancel_loop_clears_dangling_state_symlink_without_progress_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = common.state_path(str(workspace))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(workspace / 'missing-state.json', state_file)

            result = loop_control.cancel_loop(cwd=str(workspace))

            self.assertEqual(result['status'], 'cleared_invalid_state')
            self.assertFalse(state_file.exists())
            self.assertFalse(state_file.is_symlink())
            self.assertFalse(common.progress_path(str(workspace)).exists())


if __name__ == '__main__':
    unittest.main()
