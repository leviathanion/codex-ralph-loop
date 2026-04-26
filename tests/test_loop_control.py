from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ralph_core import control as loop_control  # noqa: E402
from ralph_core import storage as state_store  # noqa: E402
from ralph_test_helpers import common  # noqa: E402


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

    def test_restore_file_snapshot_requires_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with self.assertRaisesRegex(state_store.StorageError, 'file snapshot is missing contents'):
                loop_control.restore_state(loop_control.StateSnapshot(kind='file'), str(workspace))

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
                    completion_token='<promise>DONE</promise>',
                )

            self.assertEqual(result['status'], 'started')
            validate_mock.assert_called_once_with('Ship the feature', 5, '<promise>DONE</promise>')

    def test_start_loop_reports_ok_read_without_state_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with mock.patch.object(
                loop_control,
                'read_state',
                return_value=state_store.StateReadResult(status='ok'),
            ):
                with self.assertRaisesRegex(state_store.StorageError, 'internal state read returned no payload'):
                    loop_control.start_loop(
                        cwd=str(workspace),
                        prompt='Ship the feature',
                        max_iterations=5,
                        completion_token='<promise>DONE</promise>',
                    )

    def test_start_loop_succeeds_when_directory_fsync_fails_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with mock.patch.object(state_store, 'fsync_directory', side_effect=OSError('boom')):
                result = loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=5,
                    completion_token='<promise>DONE</promise>',
                )

            self.assertEqual(result['status'], 'started')
            state_result = state_store.read_state(str(workspace))
            self.assertEqual(state_result.status, 'ok')
            self.assertTrue(common.progress_path(str(workspace)).exists())

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
                'active': False,
                'prompt': 'Finished task kept for rollback coverage',
                'iteration': 4,
                'max_iterations': 6,
                'completion_token': '<promise>DONE</promise>',
                'claimed_session_id': None,
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

    def test_start_loop_rejects_existing_active_loop_without_mutation(self) -> None:
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

            with self.assertRaisesRegex(ValueError, 'An active Ralph loop already exists'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=3,
                    completion_token='<promise>DONE</promise>',
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
                    completion_token='<promise>DONE</promise>',
                )

            self.assertEqual(state_file.read_text(encoding='utf-8'), original)
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_start_loop_rejects_missing_workspace_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / 'missing-workspace'

            with self.assertRaisesRegex(state_store.StorageError, 'workspace path does not exist'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=3,
                    completion_token='<promise>DONE</promise>',
                )

            self.assertFalse(workspace.exists())

    def test_start_loop_rejects_blank_prompt_without_creating_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with self.assertRaisesRegex(ValueError, 'prompt must not be empty'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='   ',
                    max_iterations=3,
                    completion_token='<promise>DONE</promise>',
                )

            self.assertFalse((workspace / common.LOCK_RELATIVE_PATH).exists())
            self.assertFalse(common.state_path(str(workspace)).exists())
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_start_loop_rejects_invalid_max_iterations_without_creating_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with self.assertRaisesRegex(ValueError, 'max_iterations must be >= 1'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=0,
                    completion_token='<promise>DONE</promise>',
                )

            self.assertFalse((workspace / common.LOCK_RELATIVE_PATH).exists())
            self.assertFalse(common.state_path(str(workspace)).exists())
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_start_loop_rejects_empty_completion_token_without_creating_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            with self.assertRaisesRegex(ValueError, 'completion_token must be a non-empty string'):
                loop_control.start_loop(
                    cwd=str(workspace),
                    prompt='Ship the feature',
                    max_iterations=3,
                    completion_token='',
                )

            self.assertFalse((workspace / common.LOCK_RELATIVE_PATH).exists())
            self.assertFalse(common.state_path(str(workspace)).exists())
            self.assertFalse(common.progress_path(str(workspace)).exists())

    def test_start_loop_rejects_unmatchable_completion_token_without_creating_lock_file(self) -> None:
        invalid_tokens = (' <done/>', '<done/> ', '<done/>\n')
        for token in invalid_tokens:
            with self.subTest(token=token):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace = Path(tmpdir)

                    with self.assertRaisesRegex(ValueError, 'single-line string without leading or trailing whitespace'):
                        loop_control.start_loop(
                            cwd=str(workspace),
                            prompt='Ship the feature',
                            max_iterations=3,
                            completion_token=token,
                        )

                    self.assertFalse((workspace / common.LOCK_RELATIVE_PATH).exists())
                    self.assertFalse(common.state_path(str(workspace)).exists())
                    self.assertFalse(common.progress_path(str(workspace)).exists())

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

    def test_resume_loop_missing_state_is_noop_without_creating_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            result = loop_control.resume_loop(cwd=str(workspace))

            self.assertEqual(result, {
                'status': 'missing',
                'message': 'No active Ralph loop state exists in this workspace.',
            })
            self.assertFalse((workspace / common.LOCK_RELATIVE_PATH).exists())
            self.assertFalse((workspace / '.codex').exists())

    def test_resume_loop_missing_state_succeeds_in_read_only_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            os.chmod(workspace, 0o500)
            try:
                result = loop_control.resume_loop(cwd=str(workspace))
            finally:
                os.chmod(workspace, 0o700)

            self.assertEqual(result, {
                'status': 'missing',
                'message': 'No active Ralph loop state exists in this workspace.',
            })
            self.assertFalse((workspace / '.codex').exists())

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

    def test_resume_loop_rejects_legacy_running_state_without_schema_version(self) -> None:
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

            self.assertEqual(result['status'], 'invalid_schema')
            self.assertIn('schema_version must be 1', result['errors'])

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

    def test_cancel_loop_missing_state_is_noop_without_creating_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            result = loop_control.cancel_loop(cwd=str(workspace))

            self.assertEqual(result, {
                'status': 'missing',
                'message': 'No Ralph loop state was present.',
            })
            self.assertFalse((workspace / common.LOCK_RELATIVE_PATH).exists())
            self.assertFalse((workspace / '.codex').exists())

    def test_cancel_loop_missing_state_succeeds_in_read_only_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            os.chmod(workspace, 0o500)
            try:
                result = loop_control.cancel_loop(cwd=str(workspace))
            finally:
                os.chmod(workspace, 0o700)

            self.assertEqual(result, {
                'status': 'missing',
                'message': 'No Ralph loop state was present.',
            })
            self.assertFalse((workspace / '.codex').exists())

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

    def test_cancel_loop_rejects_non_directory_workspace_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / 'workspace-file'
            workspace.write_text('not a directory', encoding='utf-8')

            with self.assertRaisesRegex(state_store.StorageError, 'workspace path is not a directory'):
                loop_control.cancel_loop(cwd=str(workspace))

            self.assertEqual(workspace.read_text(encoding='utf-8'), 'not a directory')
            self.assertFalse((workspace / '.codex').exists())


if __name__ == '__main__':
    unittest.main()
