from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))

import hook_registry  # noqa: E402


class HookRegistryTests(unittest.TestCase):
    def test_read_hook_registry_rejects_structural_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.write_text(json.dumps({'hooks': {'Stop': {'oops': True}}}) + '\n', encoding='utf-8')

            result = hook_registry.read_hook_registry(hooks_json)

            self.assertEqual(result.status, 'invalid_schema')
            self.assertIn('hooks.Stop must be a list', result.errors)

    def test_register_stop_hook_creates_missing_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            status = hook_registry.register_stop_hook(hooks_json, 'python3 /tmp/stop_continue.py')

            self.assertEqual(status, 'added')
            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            self.assertTrue(hook_registry.stop_hook_registered(saved, 'python3 /tmp/stop_continue.py'))

    def test_register_stop_hook_treats_empty_object_as_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.write_text('{}\n', encoding='utf-8')

            status = hook_registry.register_stop_hook(hooks_json, 'python3 /tmp/stop_continue.py')

            self.assertEqual(status, 'added')
            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            self.assertTrue(hook_registry.stop_hook_registered(saved, 'python3 /tmp/stop_continue.py'))

    def test_register_stop_hook_reports_ok_read_without_registry_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'

            with mock.patch.object(
                hook_registry,
                'read_hook_registry',
                return_value=hook_registry.HookRegistryReadResult(status='ok'),
            ):
                with self.assertRaisesRegex(ValueError, 'internal hook registry read returned no payload'):
                    hook_registry.register_stop_hook(hooks_json, 'python3 /tmp/stop_continue.py')

    def test_read_hook_registry_reports_unreadable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.mkdir()

            result = hook_registry.read_hook_registry(hooks_json)

            self.assertEqual(result.status, 'read_error')
            self.assertIn('unable to read', result.errors[0])

    def test_unregister_stop_hook_preserves_other_stop_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.write_text(json.dumps({
                'hooks': {
                    'Stop': [
                        {
                            'hooks': [
                                {'type': 'command', 'command': 'python3 /tmp/stop_continue.py'},
                                {'type': 'command', 'command': 'python3 /tmp/other.py'},
                            ],
                        },
                    ],
                },
            }) + '\n', encoding='utf-8')

            status = hook_registry.unregister_stop_hook(hooks_json, 'python3 /tmp/stop_continue.py')

            self.assertEqual(status, 'removed')
            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            self.assertFalse(hook_registry.stop_hook_registered(saved, 'python3 /tmp/stop_continue.py'))
            self.assertTrue(hook_registry.stop_hook_registered(saved, 'python3 /tmp/other.py'))

    def test_register_stop_hook_unchanged_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            original = (
                '{\n'
                '  "hooks": {"Stop": [{"hooks": [{"command": "python3 /tmp/caf\\u00e9.py", "type": "command"}]}]}\n'
                '}\n'
            )
            hooks_json.write_text(original, encoding='utf-8')

            status = hook_registry.register_stop_hook(hooks_json, 'python3 /tmp/caf\u00e9.py')

            self.assertEqual(status, 'unchanged')
            self.assertEqual(hooks_json.read_text(encoding='utf-8'), original)

    def test_register_stop_hook_treats_equivalent_path_spellings_as_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            original = (
                '{\n'
                '  "hooks": {\n'
                '    "Stop": [\n'
                '      {\n'
                '        "hooks": [\n'
                '          {"type": "command", "command": "python3 /tmp/.codex//hooks/ralph/stop_continue.py"}\n'
                '        ]\n'
                '      }\n'
                '    ]\n'
                '  }\n'
                '}\n'
            )
            hooks_json.write_text(original, encoding='utf-8')

            status = hook_registry.register_stop_hook(
                hooks_json,
                'python3 /tmp/.codex/hooks/ralph/stop_continue.py',
            )

            self.assertEqual(status, 'unchanged')
            self.assertEqual(hooks_json.read_text(encoding='utf-8'), original)

    def test_unregister_stop_hook_removes_equivalent_path_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.write_text(json.dumps({
                'hooks': {
                    'Stop': [
                        {
                            'hooks': [
                                {'type': 'command', 'command': 'python3 /tmp/.codex//hooks/ralph/stop_continue.py'},
                                {'type': 'command', 'command': 'python3 /tmp/other.py'},
                            ],
                        },
                    ],
                },
            }) + '\n', encoding='utf-8')

            status = hook_registry.unregister_stop_hook(
                hooks_json,
                'python3 /tmp/.codex/hooks/ralph/stop_continue.py',
            )

            self.assertEqual(status, 'removed')
            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            self.assertFalse(
                hook_registry.stop_hook_registered(saved, 'python3 /tmp/.codex/hooks/ralph/stop_continue.py')
            )
            self.assertTrue(hook_registry.stop_hook_registered(saved, 'python3 /tmp/other.py'))

    def test_unregister_stop_hook_unchanged_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_json = Path(tmpdir) / 'hooks.json'
            original = '{"hooks": {"Stop": [], "PostToolUse": []}}\n'
            hooks_json.write_text(original, encoding='utf-8')

            status = hook_registry.unregister_stop_hook(hooks_json, 'python3 /tmp/missing.py')

            self.assertEqual(status, 'unchanged')
            self.assertEqual(hooks_json.read_text(encoding='utf-8'), original)

    def test_register_stop_hook_repairs_legacy_unquoted_space_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / 'home with space'
            home.mkdir()
            stop_script = home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py'
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.write_text(json.dumps({
                'hooks': {
                    'Stop': [
                        {
                            'hooks': [
                                {'type': 'command', 'command': f'python3 {stop_script}'},
                            ],
                        },
                    ],
                },
            }) + '\n', encoding='utf-8')

            stop_command = hook_registry.build_stop_command(stop_script)
            status = hook_registry.register_stop_hook(hooks_json, stop_command)

            self.assertEqual(status, 'updated')
            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            self.assertTrue(hook_registry.stop_hook_registered(saved, stop_command, require_shell_safe=True))
            self.assertEqual(
                [
                    hook['command']
                    for entry in saved.get('hooks', {}).get('Stop', [])
                    for hook in entry.get('hooks', [])
                    if hook.get('type') == 'command'
                ],
                [stop_command],
            )

    def test_register_stop_hook_preserves_symlinked_hooks_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / 'dotfiles' / 'hooks.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}\n', encoding='utf-8')
            hooks_json = root / '.codex' / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, hooks_json)

            status = hook_registry.register_stop_hook(hooks_json, 'python3 /tmp/stop_continue.py')

            self.assertEqual(status, 'added')
            self.assertTrue(hooks_json.is_symlink())
            self.assertEqual(hooks_json.resolve(), target.resolve())
            saved = json.loads(target.read_text(encoding='utf-8'))
            self.assertTrue(hook_registry.stop_hook_registered(saved, 'python3 /tmp/stop_continue.py'))

    def test_register_stop_hook_deduplicates_equivalent_registrations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / 'home with space'
            home.mkdir()
            stop_script = home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py'
            stop_command = hook_registry.build_stop_command(stop_script)
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.write_text(json.dumps({
                'hooks': {
                    'Stop': [
                        {
                            'hooks': [
                                {'type': 'command', 'command': stop_command},
                                {'type': 'command', 'command': f'python3 {stop_script}'},
                            ],
                        },
                    ],
                },
            }) + '\n', encoding='utf-8')

            status = hook_registry.register_stop_hook(hooks_json, stop_command)

            self.assertEqual(status, 'updated')
            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            self.assertTrue(hook_registry.stop_hook_registered(saved, stop_command, require_shell_safe=True))
            self.assertEqual(
                [
                    hook['command']
                    for entry in saved.get('hooks', {}).get('Stop', [])
                    for hook in entry.get('hooks', [])
                    if hook.get('type') == 'command'
                ],
                [stop_command],
            )

    def test_unregister_stop_hook_removes_legacy_unquoted_space_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / 'home with space'
            home.mkdir()
            stop_script = home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py'
            hooks_json = Path(tmpdir) / 'hooks.json'
            hooks_json.write_text(json.dumps({
                'hooks': {
                    'Stop': [
                        {
                            'hooks': [
                                {'type': 'command', 'command': f'python3 {stop_script}'},
                                {'type': 'command', 'command': 'python3 /tmp/other.py'},
                            ],
                        },
                    ],
                },
            }) + '\n', encoding='utf-8')

            status = hook_registry.unregister_stop_hook(hooks_json, hook_registry.build_stop_command(stop_script))

            self.assertEqual(status, 'removed')
            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            self.assertFalse(
                hook_registry.stop_hook_registered(
                    saved,
                    hook_registry.build_stop_command(stop_script),
                    require_shell_safe=True,
                )
            )
            self.assertTrue(hook_registry.stop_hook_registered(saved, 'python3 /tmp/other.py'))

    def test_main_uses_explicit_empty_argv_instead_of_process_arguments(self) -> None:
        stderr = io.StringIO()
        original_argv = sys.argv
        sys.argv = ['hook_registry.py', 'contains', '/tmp/hooks.json', 'python3 /tmp/stop_continue.py']
        try:
            with redirect_stderr(stderr):
                result = hook_registry.main([])
        finally:
            sys.argv = original_argv

        self.assertEqual(result, 2)
        self.assertIn('usage: hook_registry.py', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
