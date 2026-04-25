from __future__ import annotations

import json
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from profile import installer as profile_installer  # noqa: E402
from profile import doctor as profile_doctor  # noqa: E402

SHELL_SCRIPTS = sorted((REPO_ROOT / 'skills').glob('**/scripts/*.sh'))
BANNED_CONSTRUCTS = (
    'declare -A',
    '[[ -v',
    'cp -a',
    'readlink -f',
)


class ProfileInstallerTests(unittest.TestCase):
    def test_profile_package_preserves_stdlib_cprofile_run(self) -> None:
        import cProfile
        import profile

        with tempfile.TemporaryDirectory() as tmpdir:
            stats_path = Path(tmpdir) / 'profile.stats'

            # The project package is intentionally named "profile"; cProfile imports that
            # stdlib module by name and expects private helpers such as _Utils to exist.
            self.assertTrue(callable(profile._Utils))
            cProfile.run('sum(range(5))', str(stats_path))

            self.assertTrue(stats_path.is_file())

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

    def test_copy_file_atomic_treats_directory_fsync_failure_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source.txt'
            destination = root / 'destination.txt'
            source.write_text('payload\n', encoding='utf-8')

            with mock.patch.object(profile_installer, 'fsync_directory', side_effect=OSError('unsupported')):
                profile_installer.copy_file_atomic(source, destination)

            self.assertEqual(destination.read_text(encoding='utf-8'), 'payload\n')

    def test_copy_file_atomic_rejects_source_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            external = root / 'external.txt'
            source = root / 'source.txt'
            destination = root / 'destination.txt'
            external.write_text('payload\n', encoding='utf-8')
            os.symlink(external, source)

            with self.assertRaisesRegex(ValueError, 'unsupported symlink'):
                profile_installer.copy_file_atomic(source, destination)

            self.assertFalse(destination.exists())

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix-domain sockets are required')
    def test_copy_file_atomic_rejects_special_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'profile.sock'
            destination = root / 'destination.txt'
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(source))

                with self.assertRaisesRegex(ValueError, 'regular file'):
                    profile_installer.copy_file_atomic(source, destination)

            self.assertFalse(destination.exists())

    def test_copy_directory_treats_directory_fsync_failure_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source'
            destination = root / 'destination'
            source.mkdir()
            (source / 'payload.txt').write_text('payload\n', encoding='utf-8')

            with mock.patch.object(profile_installer, 'fsync_directory', side_effect=OSError('unsupported')):
                profile_installer.copy_directory(source, destination)

            self.assertEqual((destination / 'payload.txt').read_text(encoding='utf-8'), 'payload\n')

    def test_copy_directory_rejects_source_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source'
            destination = root / 'destination'
            external = root / 'external.py'
            source.mkdir()
            external.write_text('payload\n', encoding='utf-8')
            os.symlink(external, source / 'module.py')

            with self.assertRaisesRegex(ValueError, 'unsupported symlink'):
                profile_installer.copy_directory(source, destination)

            self.assertFalse(destination.exists())

    def test_copy_directory_rejects_symlinked_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            real_source = root / 'real-source'
            source = root / 'source'
            destination = root / 'destination'
            real_source.mkdir()
            (real_source / 'module.py').write_text('payload\n', encoding='utf-8')
            os.symlink(real_source, source)

            with self.assertRaisesRegex(ValueError, 'unsupported symlink'):
                profile_installer.copy_directory(source, destination)

            self.assertFalse(destination.exists())

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix-domain sockets are required')
    def test_copy_directory_rejects_special_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source'
            destination = root / 'destination'
            source.mkdir()
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(source / 'profile.sock'))

                with self.assertRaisesRegex(ValueError, 'unsupported symlink or special file'):
                    profile_installer.copy_directory(source, destination)

            self.assertFalse(destination.exists())

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix-domain sockets are required')
    def test_snapshot_path_rejects_special_file_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / 'profile.sock'
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(socket_path))
                with profile_installer.InstallTransaction() as transaction:
                    with self.assertRaisesRegex(ValueError, 'unsupported special file'):
                        transaction.snapshot_path(socket_path)

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix-domain sockets are required')
    def test_remove_path_unlinks_special_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / 'profile.sock'
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(socket_path))
                profile_installer.InstallTransaction._remove_path(socket_path)

            self.assertFalse(socket_path.exists())

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix-domain sockets are required')
    def test_directories_match_rejects_special_destination_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source'
            destination = root / 'destination'
            source.mkdir()
            destination.mkdir()
            (source / 'module.py').write_text('payload\n', encoding='utf-8')
            (destination / 'module.py').write_text('payload\n', encoding='utf-8')
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(destination / 'profile.sock'))

                self.assertFalse(profile_installer.directories_match(source, destination))

    def test_directories_match_rejects_nested_symlinked_runtime_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source'
            destination = root / 'destination'
            source.mkdir()
            destination.mkdir()
            source_file = source / 'module.py'
            source_file.write_text('payload\n', encoding='utf-8')
            os.symlink(source_file, destination / 'module.py')

            self.assertFalse(profile_installer.directories_match(source, destination))

    def test_directories_match_rejects_symlink_inside_ignored_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / 'source'
            destination = root / 'destination'
            source.mkdir()
            destination.mkdir()
            (source / 'module.py').write_text('payload\n', encoding='utf-8')
            (destination / 'module.py').write_text('payload\n', encoding='utf-8')
            cache_dir = destination / '__pycache__'
            cache_dir.mkdir()
            os.symlink(source / 'module.py', cache_dir / 'module.cpython-311.pyc')

            self.assertFalse(profile_installer.directories_match(source, destination))

    def test_install_profile_rejects_unknown_mode_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'

            with self.assertRaisesRegex(ValueError, 'mode must be one of'):
                profile_installer.install_profile(
                    root_dir=REPO_ROOT,
                    codex_home=codex_home,
                    agents_home=agents_home,
                    mode='skills',
                )

            self.assertFalse(codex_home.exists())
            self.assertFalse(agents_home.exists())

    def test_doctor_main_rejects_extra_arguments(self) -> None:
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = profile_doctor.main(['workspace', 'codex-home', 'agents-home', 'extra'])

        self.assertEqual(result, 2)
        self.assertIn('usage: doctor.py', stderr.getvalue())

    def test_install_profile_rolls_back_files_symlinks_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'

            hooks_json = codex_home / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            hooks_json.write_text('{}\n', encoding='utf-8')
            original_hooks_json = hooks_json.read_text(encoding='utf-8')

            external_hook = home / 'external_common.py'
            external_hook.write_text('external\n', encoding='utf-8')
            target_hooks = codex_home / 'hooks' / 'ralph'
            target_hooks.mkdir(parents=True, exist_ok=True)
            common_hook = target_hooks / 'common.py'
            os.symlink(external_hook, common_hook)
            original_symlink_target = os.readlink(common_hook)

            with mock.patch.object(profile_installer, 'validate_stop_hook_registration', side_effect=OSError('boom')):
                with self.assertRaises(OSError):
                    profile_installer.install_profile(
                        root_dir=REPO_ROOT,
                        codex_home=codex_home,
                        agents_home=agents_home,
                    )

            self.assertEqual(hooks_json.read_text(encoding='utf-8'), original_hooks_json)
            self.assertTrue(common_hook.is_symlink())
            self.assertEqual(os.readlink(common_hook), original_symlink_target)
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
            source_hook = REPO_ROOT / 'hooks' / 'stop_continue.py'
            external_hook = home / 'external_common.py'
            external_hook.write_bytes(source_hook.read_bytes())

            target_hook = codex_home / 'hooks' / 'ralph' / 'stop_continue.py'
            target_hook.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(external_hook, target_hook)

            changes = profile_installer.install_profile(
                root_dir=REPO_ROOT,
                codex_home=codex_home,
                agents_home=agents_home,
                mode='hooks-only',
            )

            self.assertIn('copied hook stop_continue.py', changes)
            self.assertFalse(target_hook.is_symlink())
            self.assertEqual(target_hook.read_bytes(), source_hook.read_bytes())

    def test_install_profile_repairs_nested_runtime_symlink_even_when_contents_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            codex_home = home / '.codex'
            agents_home = home / '.agents'

            profile_installer.install_profile(
                root_dir=REPO_ROOT,
                codex_home=codex_home,
                agents_home=agents_home,
                mode='hooks-only',
            )

            source_module = REPO_ROOT / 'ralph_core' / 'model.py'
            installed_module = codex_home / 'hooks' / 'ralph' / 'ralph_core' / 'model.py'
            installed_module.unlink()
            os.symlink(source_module, installed_module)

            changes = profile_installer.install_profile(
                root_dir=REPO_ROOT,
                codex_home=codex_home,
                agents_home=agents_home,
                mode='hooks-only',
            )

            self.assertIn('copied runtime package ralph_core', changes)
            self.assertFalse(installed_module.is_symlink())
            self.assertEqual(installed_module.read_bytes(), source_module.read_bytes())

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
            self.assertIn('registered Stop hook', changes)

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

            with mock.patch.object(profile_installer, 'validate_stop_hook_registration', side_effect=OSError('boom')):
                with self.assertRaisesRegex(OSError, 'boom'):
                    profile_installer.install_profile(
                        root_dir=REPO_ROOT,
                        codex_home=codex_home,
                        agents_home=agents_home,
                        mode='hooks-only',
                    )

            self.assertTrue(hooks_json.is_symlink())
            self.assertEqual(hooks_target.read_text(encoding='utf-8'), hooks_target_original)
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

            self.assertFalse(hooks_target.parent.exists())

            with mock.patch.object(profile_installer, 'validate_stop_hook_registration', side_effect=OSError('boom')):
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
