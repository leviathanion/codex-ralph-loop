from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))

import profile_installer  # noqa: E402

SHELL_SCRIPTS = sorted((REPO_ROOT / 'skills').glob('**/scripts/*.sh'))
BANNED_CONSTRUCTS = (
    'declare -A',
    '[[ -v',
    'cp -a',
    'readlink -f',
)


class ProfileInstallerTests(unittest.TestCase):
    def test_copy_file_atomic_fsyncs_temp_file_and_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source.txt'
            destination = root / 'destination.txt'
            source.write_text('payload\n', encoding='utf-8')

            with mock.patch.object(profile_installer.os, 'fsync', wraps=profile_installer.os.fsync) as fsync_mock:
                profile_installer.copy_file_atomic(source, destination)

            self.assertEqual(destination.read_text(encoding='utf-8'), 'payload\n')
            self.assertGreaterEqual(fsync_mock.call_count, 2)

    def test_install_profile_rolls_back_files_symlinks_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'

            hooks_json = codex_home / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            hooks_json.write_text('{}\n', encoding='utf-8')
            original_hooks_json = hooks_json.read_text(encoding='utf-8')

            config_toml = codex_home / 'config.toml'
            config_toml.mkdir()

            external_hook = home / 'external_common.py'
            external_hook.write_text('external\n', encoding='utf-8')
            target_hooks = codex_home / 'hooks' / 'ralph'
            target_hooks.mkdir(parents=True, exist_ok=True)
            common_hook = target_hooks / 'common.py'
            os.symlink(external_hook, common_hook)
            original_symlink_target = os.readlink(common_hook)

            with self.assertRaises(OSError):
                profile_installer.install_profile(
                    root_dir=REPO_ROOT,
                    codex_home=codex_home,
                    agents_home=agents_home,
                )

            self.assertEqual(hooks_json.read_text(encoding='utf-8'), original_hooks_json)
            self.assertTrue(common_hook.is_symlink())
            self.assertEqual(os.readlink(common_hook), original_symlink_target)
            self.assertTrue(config_toml.is_dir())
            self.assertFalse((agents_home / 'skills').exists())
            self.assertFalse((target_hooks / 'stop_continue.py').exists())

    def test_install_profile_repairs_dangling_skill_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'
            broken_target = agents_home / 'skills' / 'doctor-ralph'
            broken_target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(home / 'missing-doctor-skill', broken_target)

            changes = profile_installer.install_profile(
                root_dir=REPO_ROOT,
                codex_home=codex_home,
                agents_home=agents_home,
                mode='skills-only',
            )

            self.assertTrue(broken_target.is_symlink())
            self.assertEqual(
                broken_target.resolve(),
                (REPO_ROOT / 'skills' / 'doctor-ralph').resolve(),
            )
            self.assertIn(f'relinked broken skill doctor-ralph -> {broken_target}', changes)

    def test_install_profile_rejects_missing_skill_source_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'
            root_dir = home / 'package-root'
            (root_dir / 'skills').mkdir(parents=True, exist_ok=True)

            with mock.patch.object(profile_installer, 'SKILL_NAMES', ('doctor-ralph',)):
                with self.assertRaisesRegex(ValueError, 'missing skill source'):
                    profile_installer.install_profile(
                        root_dir=root_dir,
                        codex_home=codex_home,
                        agents_home=agents_home,
                        mode='skills-only',
                    )

            self.assertFalse((agents_home / 'skills').exists())

    def test_install_profile_replaces_symlinked_hook_file_even_when_contents_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'
            source_hook = REPO_ROOT / 'hooks' / 'common.py'
            external_hook = home / 'external_common.py'
            external_hook.write_bytes(source_hook.read_bytes())

            target_hook = codex_home / 'hooks' / 'ralph' / 'common.py'
            target_hook.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(external_hook, target_hook)

            changes = profile_installer.install_profile(
                root_dir=REPO_ROOT,
                codex_home=codex_home,
                agents_home=agents_home,
                mode='hooks-only',
            )

            self.assertIn('copied hook common.py', changes)
            self.assertFalse(target_hook.is_symlink())
            self.assertEqual(target_hook.read_bytes(), source_hook.read_bytes())

    def test_install_profile_rejects_live_symlinked_hook_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'
            external_hooks = home / 'external-hooks'
            external_hooks.mkdir(parents=True, exist_ok=True)
            (codex_home / 'hooks').mkdir(parents=True, exist_ok=True)
            os.symlink(external_hooks, codex_home / 'hooks' / 'ralph')

            with self.assertRaisesRegex(ValueError, 'hook directory path component is a symlink'):
                profile_installer.install_profile(
                    root_dir=REPO_ROOT,
                    codex_home=codex_home,
                    agents_home=agents_home,
                    mode='hooks-only',
                )

            self.assertTrue((codex_home / 'hooks' / 'ralph').is_symlink())
            self.assertFalse((external_hooks / 'stop_continue.py').exists())

    def test_install_profile_preserves_symlinked_profile_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'
            dotfiles = home / 'dotfiles'
            dotfiles.mkdir(parents=True, exist_ok=True)

            hooks_target = dotfiles / 'hooks.json'
            hooks_target.write_text('{}\n', encoding='utf-8')
            hooks_json = codex_home / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(hooks_target, hooks_json)

            config_target = dotfiles / 'config.toml'
            config_target.write_text('[features]\ncodex_hooks = false\n', encoding='utf-8')
            config_toml = codex_home / 'config.toml'
            os.symlink(config_target, config_toml)

            changes = profile_installer.install_profile(
                root_dir=REPO_ROOT,
                codex_home=codex_home,
                agents_home=agents_home,
                mode='hooks-only',
            )

            self.assertTrue(hooks_json.is_symlink())
            self.assertEqual(hooks_json.resolve(), hooks_target.resolve())
            saved_registry = json.loads(hooks_target.read_text(encoding='utf-8'))
            self.assertIn('Stop', saved_registry.get('hooks', {}))

            self.assertTrue(config_toml.is_symlink())
            self.assertEqual(config_toml.resolve(), config_target.resolve())
            self.assertEqual(
                config_target.read_text(encoding='utf-8'),
                '[features]\ncodex_hooks = true\n',
            )
            self.assertIn('registered Stop hook', changes)
            self.assertIn('enabled codex_hooks feature flag', changes)

    def test_install_profile_rolls_back_symlink_target_updates_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'
            dotfiles = home / 'dotfiles'
            dotfiles.mkdir(parents=True, exist_ok=True)

            hooks_target = dotfiles / 'hooks.json'
            hooks_target_original = '{}\n'
            hooks_target.write_text(hooks_target_original, encoding='utf-8')
            hooks_json = codex_home / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(hooks_target, hooks_json)

            config_target = dotfiles / 'config.toml'
            config_target_original = '[features]\ncodex_hooks = false\n'
            config_target.write_text(config_target_original, encoding='utf-8')
            config_toml = codex_home / 'config.toml'
            os.symlink(config_target, config_toml)

            with mock.patch.object(profile_installer, 'record_feature_flag_change', side_effect=OSError('boom')):
                with self.assertRaisesRegex(OSError, 'boom'):
                    profile_installer.install_profile(
                        root_dir=REPO_ROOT,
                        codex_home=codex_home,
                        agents_home=agents_home,
                        mode='hooks-only',
                    )

            self.assertTrue(hooks_json.is_symlink())
            self.assertEqual(hooks_target.read_text(encoding='utf-8'), hooks_target_original)
            self.assertTrue(config_toml.is_symlink())
            self.assertEqual(config_target.read_text(encoding='utf-8'), config_target_original)
            self.assertFalse((codex_home / 'hooks' / 'ralph' / 'stop_continue.py').exists())

    def test_install_profile_rolls_back_created_symlink_target_parent_dirs_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'
            dotfiles = home / 'dotfiles'
            dotfiles.mkdir(parents=True, exist_ok=True)

            hooks_target = dotfiles / 'nested' / 'hooks.json'
            hooks_json = codex_home / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(hooks_target, hooks_json)

            config_target = dotfiles / 'config.toml'
            config_target.write_text('[features]\ncodex_hooks = false\n', encoding='utf-8')
            config_toml = codex_home / 'config.toml'
            os.symlink(config_target, config_toml)

            self.assertFalse(hooks_target.parent.exists())

            with mock.patch.object(profile_installer, 'record_feature_flag_change', side_effect=OSError('boom')):
                with self.assertRaisesRegex(OSError, 'boom'):
                    profile_installer.install_profile(
                        root_dir=REPO_ROOT,
                        codex_home=codex_home,
                        agents_home=agents_home,
                        mode='hooks-only',
                    )

            self.assertFalse(hooks_target.exists())
            self.assertFalse(hooks_target.parent.exists())
            self.assertTrue(hooks_json.is_symlink())
            self.assertEqual(config_target.read_text(encoding='utf-8'), '[features]\ncodex_hooks = false\n')
            self.assertFalse((codex_home / 'hooks' / 'ralph' / 'stop_continue.py').exists())


class ShellPortabilityTests(unittest.TestCase):
    def test_shell_scripts_parse_with_bash_n(self) -> None:
        for script in SHELL_SCRIPTS:
            result = subprocess.run(
                ['bash', '-n', str(script)],
                cwd=str(REPO_ROOT),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, f'{script}: {result.stderr}')

    def test_shell_scripts_avoid_known_nonportable_constructs(self) -> None:
        for script in SHELL_SCRIPTS:
            contents = script.read_text(encoding='utf-8')
            for needle in BANNED_CONSTRUCTS:
                self.assertNotIn(needle, contents, f'{script} should not contain {needle!r}')


if __name__ == '__main__':
    unittest.main()
