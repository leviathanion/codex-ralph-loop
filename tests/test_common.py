from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
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


class CommonModuleTests(unittest.TestCase):
    def test_read_state_rejects_legacy_state_shape_without_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = workspace / '.codex' / 'ralph' / 'state.json'
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                'prompt': 'finish the task',
                'iteration': 2,
                'max_iterations': 5,
                'phase': 'running',
            }) + '\n', encoding='utf-8')

            result = state_store.read_state(str(workspace))

            self.assertEqual(result.status, 'invalid_schema')
            self.assertIn('schema_version must be 3', result.errors)

    def test_read_state_requires_complete_v3_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state_file = workspace / '.codex' / 'ralph' / 'state.json'
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({'schema_version': 3}) + '\n', encoding='utf-8')

            result = state_store.read_state(str(workspace))

            self.assertEqual(result.status, 'invalid_schema')
            self.assertIn('prompt must be a non-empty string', result.errors)
            self.assertTrue(any('phase must be one of' in error for error in result.errors))

    def test_read_state_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'prompt': 'Ship the feature',
                'future_flag': True,
            })
            common.state_path(str(workspace)).parent.mkdir(parents=True, exist_ok=True)
            common.state_path(str(workspace)).write_text(json.dumps(state) + '\n', encoding='utf-8')

            result = state_store.read_state(str(workspace))

            self.assertEqual(result.status, 'invalid_schema')
            self.assertIn('unknown state field(s): future_flag', result.errors)

    def test_validate_pending_update_requires_reason_for_blocked_and_failed(self) -> None:
        blocked_errors = state_store.validate_pending_update({
            'iteration': 0,
            'session_id': 'session-1',
            'status': 'blocked',
            'summary': 'need approval',
            'files': [],
            'checks': [],
            'reason': None,
            'updated_at': common.now_iso(),
        })
        failed_errors = state_store.validate_pending_update({
            'iteration': 0,
            'session_id': 'session-1',
            'status': 'failed',
            'summary': 'tests failed',
            'files': [],
            'checks': [],
            'reason': '',
            'updated_at': common.now_iso(),
        })

        self.assertIn('pending_update.reason must be a non-empty string', blocked_errors)
        self.assertIn('pending_update.reason must be a non-empty string', failed_errors)

    def test_validate_state_rejects_stale_pending_iteration(self) -> None:
        state = state_store.default_state()
        state.update({
            'prompt': 'Ship it',
            'iteration': 3,
            'claimed_session_id': 'session-1',
            'pending_update': {
                'iteration': 2,
                'session_id': 'session-1',
                'status': 'progress',
                'summary': 'updated docs',
                'files': ['README.md'],
                'checks': [],
                'reason': None,
                'updated_at': common.now_iso(),
            },
        })

        errors = state_store.validate_state_payload(state)

        self.assertIn('pending_update.iteration must match state iteration', errors)

    def test_validate_state_rejects_unbound_pending_update(self) -> None:
        state = state_store.default_state()
        state.update({
            'prompt': 'Ship it',
            'pending_update': {
                'iteration': 0,
                'session_id': 'session-1',
                'status': 'progress',
                'summary': 'updated docs',
                'files': [],
                'checks': [],
                'reason': None,
                'updated_at': common.now_iso(),
            },
        })

        errors = state_store.validate_state_payload(state)

        self.assertIn('pending_update requires claimed_session_id', errors)

    def test_validate_state_rejects_pending_session_mismatch(self) -> None:
        state = state_store.default_state()
        state.update({
            'prompt': 'Ship it',
            'claimed_session_id': 'session-a',
            'pending_update': {
                'iteration': 0,
                'session_id': 'session-b',
                'status': 'progress',
                'summary': 'updated docs',
                'files': [],
                'checks': [],
                'reason': None,
                'updated_at': common.now_iso(),
            },
        })

        errors = state_store.validate_state_payload(state)

        self.assertIn('pending_update.session_id must match claimed_session_id', errors)

    def test_save_state_round_trips_complete_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            state = state_store.default_state()
            state.update({
                'prompt': 'finish the task',
            })

            state_store.save_state(state, str(workspace))
            loaded = state_store.read_state(str(workspace))

            self.assertEqual(loaded.status, 'ok')
            self.assertEqual(loaded.value, state)

    def test_workspace_lock_times_out_when_another_process_holds_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            lock_path = workspace / common.LOCK_RELATIVE_PATH
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            holder = subprocess.Popen(
                [
                    'python3',
                    '-c',
                    (
                        'import fcntl, sys, time\n'
                        'path = sys.argv[1]\n'
                        'handle = open(path, "a+", encoding="utf-8")\n'
                        'fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n'
                        'print("ready", flush=True)\n'
                        'time.sleep(5)\n'
                    ),
                    str(lock_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), 'ready')

                started = time.monotonic()
                with self.assertRaisesRegex(state_store.StorageError, 'timed out waiting for Ralph control lock'):
                    with state_store.workspace_lock(str(workspace), timeout_seconds=0.05):
                        pass

                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                holder.terminate()
                holder.communicate(timeout=2)

    def test_atomic_write_text_treats_post_replace_directory_fsync_failure_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'sample.txt'
            path.write_text('old\n', encoding='utf-8')

            with mock.patch.object(state_store, 'fsync_directory', side_effect=OSError('boom')):
                common.atomic_write_text(path, 'new\n')

            self.assertEqual(path.read_text(encoding='utf-8'), 'new\n')

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

    def test_build_pending_update_normalizes_lists_and_summary(self) -> None:
        update = loop_control.build_pending_update(
            iteration=2,
            session_id='session-1',
            status='progress',
            summary='  shipped   docs  ',
            files=[' README.md ', '', 'docs/plan.md'],
            checks=[' passed:unit ', ''],
        )

        self.assertEqual(update['summary'], 'shipped docs')
        self.assertEqual(update['session_id'], 'session-1')
        self.assertEqual(update['files'], ['README.md', 'docs/plan.md'])
        self.assertEqual(update['checks'], ['passed:unit'])

    def test_fingerprint_message_is_whitespace_insensitive(self) -> None:
        self.assertEqual(
            common.fingerprint_message('hello   world'),
            common.fingerprint_message('hello world'),
        )
