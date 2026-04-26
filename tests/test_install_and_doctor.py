from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / 'scripts' / 'install_ralph.sh'
DOCTOR_SCRIPT = REPO_ROOT / 'skills' / 'doctor-ralph' / 'scripts' / 'doctor_ralph.sh'
UNINSTALL_SCRIPT = REPO_ROOT / 'skills' / 'uninstall-ralph' / 'scripts' / 'uninstall_ralph.sh'
START_SCRIPT = REPO_ROOT / 'skills' / 'ralph-loop' / 'scripts' / 'start_ralph.sh'
CONTINUE_SCRIPT = REPO_ROOT / 'skills' / 'continue-ralph-loop' / 'scripts' / 'continue_ralph.sh'
CANCEL_SCRIPT = REPO_ROOT / 'skills' / 'cancel-ralph' / 'scripts' / 'cancel_ralph.sh'


class InstallAndDoctorTests(unittest.TestCase):
    def run_script(
        self,
        script: Path,
        *,
        cwd: Path,
        env: dict[str, str],
        args: list[str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['bash', str(script), *(args or [])],
            cwd=str(cwd),
            env=env,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )

    def make_env(self, home: Path) -> dict[str, str]:
        env = os.environ.copy()
        env['HOME'] = str(home)
        env.pop('CODEX_HOME', None)
        env.pop('AGENTS_HOME', None)
        return env

    def make_env_without_readlink(self, home: Path, fake_bin: Path) -> dict[str, str]:
        env = self.make_env(home)
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_readlink = fake_bin / 'readlink'
        fake_readlink.write_text(
            '#!/usr/bin/env bash\n'
            'echo "readlink should not be called by Ralph scripts" >&2\n'
            'exit 97\n',
            encoding='utf-8',
        )
        fake_readlink.chmod(0o755)
        env['PATH'] = f'{fake_bin}:{env["PATH"]}'
        return env

    def test_install_is_idempotent_and_doctor_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)

            first = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(first.returncode, 0)
            self.assertIn('Installed Codex Ralph:', first.stdout)

            second = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(second.returncode, 0)
            self.assertIn('Codex Ralph is already installed.', second.stdout)

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 0)
            self.assertIn('[OK] Skills:', doctor.stdout)
            self.assertIn('[OK] Hooks:', doctor.stdout)
            self.assertIn('[OK] Progress: no progress ledger present', doctor.stdout)
            self.assertFalse((workspace / '.codex').exists())

    def test_install_script_bootstraps_without_install_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)

            install = self.run_script(INSTALL_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertIn('Installed Codex Ralph:', install.stdout)
            self.assertFalse((home / '.agents' / 'skills' / 'install-ralph').exists())
            self.assertTrue((home / '.agents' / 'skills' / 'uninstall-ralph').is_symlink())

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(doctor.returncode, 0, doctor.stdout)
            self.assertIn('[OK] Skills:', doctor.stdout)
            self.assertIn('[OK] Hooks:', doctor.stdout)

            uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(uninstall.returncode, 0, uninstall.stdout)
            self.assertFalse((home / '.agents' / 'skills' / 'uninstall-ralph').exists())

    def test_install_handles_non_ascii_home_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_root) / 'caf\u00e9-home'
            home.mkdir()
            workspace = Path(tmp_workspace)
            env = self.make_env(home)

            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(install.returncode, 0)
            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 0)
            self.assertIn('[OK] Hook Registry:', doctor.stdout)

    def test_install_quotes_stop_hook_command_when_home_has_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_root) / 'home with space'
            home.mkdir()
            workspace = Path(tmp_workspace)
            env = self.make_env(home)

            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(install.returncode, 0)
            hooks_json = json.loads((home / '.codex' / 'hooks.json').read_text(encoding='utf-8'))
            stop_commands = [
                hook['command']
                for entry in hooks_json.get('hooks', {}).get('Stop', [])
                for hook in entry.get('hooks', [])
                if hook.get('type') == 'command'
            ]
            expected_path = str(home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py')
            self.assertEqual(len(stop_commands), 1)
            self.assertEqual(shlex.split(stop_commands[0]), ['python3', expected_path])
            self.assertNotEqual(stop_commands[0], f'python3 {expected_path}')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 0)
            self.assertIn('[OK] Hook Registry:', doctor.stdout)

    def test_install_treats_empty_hooks_json_as_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            hooks_json = home / '.codex' / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            hooks_json.write_text('{}\n', encoding='utf-8')

            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertIn('registered Stop hook', install.stdout)

            saved = json.loads(hooks_json.read_text(encoding='utf-8'))
            stop_commands = [
                hook['command']
                for entry in saved.get('hooks', {}).get('Stop', [])
                for hook in entry.get('hooks', [])
                if hook.get('type') == 'command'
            ]
            self.assertEqual(len(stop_commands), 1)

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 0)
            self.assertIn('[OK] Hook Registry:', doctor.stdout)

    def test_install_rolls_back_when_hook_registry_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            hooks_json = home / '.codex' / 'hooks.json'
            hooks_json.parent.mkdir(parents=True, exist_ok=True)
            original = json.dumps({'hooks': {'Stop': {'oops': True}}}) + '\n'
            hooks_json.write_text(original, encoding='utf-8')

            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(install.returncode, 1)
            self.assertEqual(hooks_json.read_text(encoding='utf-8'), original)
            self.assertFalse((home / '.agents' / 'skills' / 'ralph-loop').exists())
            self.assertFalse((home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py').exists())

    def test_path_spelling_changes_do_not_duplicate_or_hide_stop_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            install_env = self.make_env(home)
            install_env['CODEX_HOME'] = f'{home / ".codex"}/'

            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=install_env)
            self.assertEqual(install.returncode, 0)

            normalized_env = self.make_env(home)
            reinstall = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=normalized_env)
            self.assertEqual(reinstall.returncode, 0)

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=normalized_env)
            self.assertEqual(doctor.returncode, 0)
            self.assertIn('[OK] Hook Registry:', doctor.stdout)

            hooks_json = json.loads((home / '.codex' / 'hooks.json').read_text(encoding='utf-8'))
            stop_commands = [
                hook['command']
                for entry in hooks_json.get('hooks', {}).get('Stop', [])
                for hook in entry.get('hooks', [])
                if hook.get('type') == 'command'
            ]
            self.assertEqual(
                stop_commands,
                [f'python3 {home / ".codex" / "hooks" / "ralph" / "stop_continue.py"}'],
            )

            uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=normalized_env)
            self.assertEqual(uninstall.returncode, 0)

            hooks_json_after = json.loads((home / '.codex' / 'hooks.json').read_text(encoding='utf-8'))
            self.assertNotIn('Stop', hooks_json_after.get('hooks', {}))

    def test_doctor_and_uninstall_normalize_tilde_home_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            env['CODEX_HOME'] = '~/.codex-custom'
            env['AGENTS_HOME'] = '~/.agents-custom'

            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(install.returncode, 0, install.stderr)
            codex_home = home / '.codex-custom'
            agents_home = home / '.agents-custom'
            self.assertTrue((codex_home / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            self.assertTrue((agents_home / 'skills' / 'doctor-ralph').exists())

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(doctor.returncode, 0, doctor.stdout)
            self.assertIn('[OK] Hooks:', doctor.stdout)
            self.assertIn('[OK] Skills:', doctor.stdout)

            uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(uninstall.returncode, 0, uninstall.stdout)
            self.assertIn('removed hook file', uninstall.stdout)
            self.assertIn('removed skill link', uninstall.stdout)
            self.assertFalse((codex_home / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            self.assertFalse((agents_home / 'skills' / 'doctor-ralph').exists())

    def test_install_doctor_and_uninstall_share_home_anchored_relative_profile_overrides(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_workspace,
            tempfile.TemporaryDirectory() as tmp_install_cwd,
        ):
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            install_cwd = Path(tmp_install_cwd)
            env = self.make_env(home)
            env['CODEX_HOME'] = '.codex-relative'
            env['AGENTS_HOME'] = '.agents-relative'

            install = self.run_script(INSTALL_SCRIPT, cwd=install_cwd, env=env)

            self.assertEqual(install.returncode, 0, install.stderr)
            codex_home = home / '.codex-relative'
            agents_home = home / '.agents-relative'
            self.assertTrue((codex_home / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            self.assertTrue((agents_home / 'skills' / 'doctor-ralph').is_symlink())
            self.assertFalse((install_cwd / '.codex-relative').exists())
            self.assertFalse((install_cwd / '.agents-relative').exists())

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(doctor.returncode, 0, doctor.stdout)
            self.assertIn('[OK] Hooks:', doctor.stdout)
            self.assertIn('[OK] Skills:', doctor.stdout)

            uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(uninstall.returncode, 0, uninstall.stdout)
            self.assertIn('removed hook file', uninstall.stdout)
            self.assertIn('removed skill link', uninstall.stdout)
            self.assertFalse((codex_home / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            self.assertFalse((agents_home / 'skills' / 'doctor-ralph').exists())
            self.assertFalse((workspace / '.codex-relative').exists())
            self.assertFalse((workspace / '.agents-relative').exists())

    def test_uninstall_pristine_profile_reports_nothing_to_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)

            uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertIn('Nothing to uninstall.', uninstall.stdout)
            self.assertNotIn('Uninstalled Codex Ralph:', uninstall.stdout)

    def test_uninstall_skills_leaves_foreign_skill_symlink_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            skills_dir = home / '.agents' / 'skills'
            foreign_skill = home / 'foreign-skill'
            foreign_skill.mkdir(parents=True, exist_ok=True)
            target = skills_dir / 'doctor-ralph'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(foreign_skill)

            uninstall = self.run_script(
                UNINSTALL_SCRIPT,
                cwd=workspace,
                env=env,
                args=['--skills-only'],
            )

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), foreign_skill.resolve())
            self.assertIn('left skill link unchanged', uninstall.stdout)

    def test_doctor_does_not_mutate_clean_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(doctor.returncode, 0)
            self.assertIn('without mutating the workspace', doctor.stdout)
            self.assertFalse((workspace / '.codex').exists())

    def test_doctor_fails_on_missing_skill_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            (home / '.agents' / 'skills' / 'doctor-ralph').unlink()

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Skills:', doctor.stdout)

    def test_doctor_fails_on_non_symlink_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            skill_path = home / '.agents' / 'skills' / 'doctor-ralph'
            skill_path.unlink()
            skill_path.mkdir()

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('doctor-ralph is not a symlink', doctor.stdout)

    def test_doctor_fails_on_missing_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            (home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py').unlink()

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hooks:', doctor.stdout)

    def test_doctor_fails_on_corrupted_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hook_path = home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py'
            hook_path.write_text('not python anymore\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('content does not match packaged hook', doctor.stdout)

    def test_doctor_fails_on_symlinked_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hook_path = home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py'
            external_hook = home / 'external_common.py'
            external_hook.write_bytes(hook_path.read_bytes())
            hook_path.unlink()
            os.symlink(external_hook, hook_path)

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hooks:', doctor.stdout)
            self.assertIn('installed hook must be a regular file, not a symlink', doctor.stdout)

    def test_doctor_fails_on_nested_runtime_package_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            runtime_module = home / '.codex' / 'hooks' / 'ralph' / 'ralph_core' / 'model.py'
            external_module = home / 'external_model.py'
            external_module.write_bytes(runtime_module.read_bytes())
            runtime_module.unlink()
            os.symlink(external_module, runtime_module)

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hooks:', doctor.stdout)
            self.assertIn('runtime package does not match packaged source', doctor.stdout)

    def test_doctor_fails_on_invalid_state_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            (ralph_dir / 'state.json').write_text(json.dumps({'active': 'yes'}) + '\n', encoding='utf-8')
            (ralph_dir / 'progress.jsonl').write_text('not-json\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] State:', doctor.stdout)
            self.assertIn('[FAIL] Progress:', doctor.stdout)

    def test_doctor_fails_on_structurally_invalid_hook_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hooks_json = home / '.codex' / 'hooks.json'
            hooks_json.write_text(json.dumps({'hooks': {'Stop': {'oops': True}}}) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hook Registry:', doctor.stdout)
            self.assertIn('hooks.Stop must be a list', doctor.stdout)

    def test_doctor_fails_on_legacy_unquoted_space_path_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_root) / 'home with space'
            home.mkdir()
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hooks_json_path = home / '.codex' / 'hooks.json'
            hooks_json = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            broken_command = f'python3 {home / ".codex" / "hooks" / "ralph" / "stop_continue.py"}'
            hooks_json['hooks']['Stop'][0]['hooks'][0]['command'] = broken_command
            hooks_json_path.write_text(json.dumps(hooks_json, indent=2) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hook Registry:', doctor.stdout)
            self.assertIn('command is malformed', doctor.stdout)

    def test_doctor_flags_and_install_repairs_duplicate_equivalent_stop_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_root) / 'home with space'
            home.mkdir()
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hooks_json_path = home / '.codex' / 'hooks.json'
            hooks_json = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            stop_script = home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py'
            hooks_json['hooks']['Stop'][0]['hooks'].append({
                'type': 'command',
                'command': f'python3 {stop_script}',
            })
            hooks_json_path.write_text(json.dumps(hooks_json, indent=2) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hook Registry:', doctor.stdout)
            self.assertIn('equivalent Ralph Stop hook registrations', doctor.stdout)

            reinstall = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(reinstall.returncode, 0)
            self.assertIn('repaired Stop hook registration', reinstall.stdout)

            hooks_json_after = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            stop_commands = [
                hook['command']
                for entry in hooks_json_after.get('hooks', {}).get('Stop', [])
                for hook in entry.get('hooks', [])
                if hook.get('type') == 'command'
            ]
            expected_path = str(stop_script)
            self.assertEqual(stop_commands, [shlex.join(['python3', expected_path])])

            doctor_after = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor_after.returncode, 0)
            self.assertIn('[OK] Hook Registry:', doctor_after.stdout)

    def test_doctor_flags_and_install_repairs_missing_stop_hook_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hooks_json_path = home / '.codex' / 'hooks.json'
            hooks_json = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            hooks_json['hooks']['Stop'][0]['hooks'][0].pop('timeout')
            hooks_json_path.write_text(json.dumps(hooks_json, indent=2) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hook Registry:', doctor.stdout)
            self.assertIn('must set timeout = 30', doctor.stdout)

            reinstall = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(reinstall.returncode, 0)
            self.assertIn('repaired Stop hook registration', reinstall.stdout)
            repaired = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            self.assertEqual(repaired['hooks']['Stop'][0]['hooks'][0]['timeout'], 30)

            doctor_after = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor_after.returncode, 0)

    def test_doctor_fails_on_truncated_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            (ralph_dir / 'state.json').write_text(json.dumps({'active': True}) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] State:', doctor.stdout)
            self.assertIn('prompt must be a string', doctor.stdout)
            self.assertIn('iteration must be an integer', doctor.stdout)

    def test_doctor_fails_on_progress_entry_missing_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            (ralph_dir / 'progress.jsonl').write_text(json.dumps({
                'iteration': 1,
                'session_id': None,
                'status': 'progress',
                'summary': 'missing ts',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('ts must be a non-empty ISO8601 string', doctor.stdout)

    def test_doctor_fails_on_truncated_progress_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            (ralph_dir / 'progress.jsonl').write_text(
                json.dumps({
                    'ts': '2026-04-20T00:00:00Z',
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

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Progress:', doctor.stdout)
            self.assertIn('line 2: invalid JSON', doctor.stdout)

    def test_doctor_fails_on_legacy_style_progress_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            (ralph_dir / 'progress.jsonl').write_text(json.dumps({
                'status': 'cancelled',
                'summary': 'Ralph loop cancelled manually',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': 'manual_cancel',
            }) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Progress:', doctor.stdout)
            self.assertIn('iteration must be an integer', doctor.stdout)

    def test_doctor_fails_cleanly_when_progress_path_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            (ralph_dir / 'progress.jsonl').mkdir()

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Progress:', doctor.stdout)
            self.assertIn('unable to read', doctor.stdout)

    def test_doctor_fails_on_dangling_progress_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            os.symlink(ralph_dir / 'missing-progress.jsonl', ralph_dir / 'progress.jsonl')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Progress:', doctor.stdout)
            self.assertIn('unable to read', doctor.stdout)
            self.assertNotIn('[OK] Progress: no progress ledger present', doctor.stdout)

    def test_doctor_fails_on_live_progress_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            external_progress = home / 'external-progress.jsonl'
            external_progress.write_text(json.dumps({
                'ts': '2026-04-20T00:00:00Z',
                'iteration': 0,
                'session_id': None,
                'status': 'started',
                'summary': 'Ralph loop started',
                'files': [],
                'checks': [],
                'message_fingerprint': None,
                'reason': None,
            }) + '\n', encoding='utf-8')
            os.symlink(external_progress, ralph_dir / 'progress.jsonl')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Progress:', doctor.stdout)
            self.assertIn('path component is a symlink', doctor.stdout)
            self.assertNotIn('[OK] Progress:', doctor.stdout)

    def test_doctor_fails_when_dot_codex_is_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            (workspace / '.codex').write_text('not a directory\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('path component is not a directory', doctor.stdout)

    def test_doctor_fails_when_dot_codex_is_a_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            os.symlink(workspace / 'missing-codex-dir', workspace / '.codex')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Workspace:', doctor.stdout)
            self.assertIn('path component is a dangling symlink', doctor.stdout)
            self.assertNotIn('[OK] Workspace:', doctor.stdout)

    def test_doctor_fails_when_dot_codex_is_a_live_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            external_codex = home / 'external-codex'
            external_codex.mkdir(parents=True, exist_ok=True)
            os.symlink(external_codex, workspace / '.codex')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Workspace:', doctor.stdout)
            self.assertIn('path component is a symlink', doctor.stdout)
            self.assertNotIn('[OK] Workspace:', doctor.stdout)

    def test_doctor_fails_when_ralph_path_is_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            codex_dir = workspace / '.codex'
            codex_dir.mkdir(parents=True, exist_ok=True)
            (codex_dir / 'ralph').write_text('not a directory\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('path component is not a directory', doctor.stdout)

    def test_doctor_fails_when_ralph_path_is_a_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            codex_dir = workspace / '.codex'
            codex_dir.mkdir(parents=True, exist_ok=True)
            os.symlink(workspace / 'missing-ralph-dir', codex_dir / 'ralph')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Workspace:', doctor.stdout)
            self.assertIn('path component is a dangling symlink', doctor.stdout)
            self.assertNotIn('[OK] Workspace:', doctor.stdout)

    def test_doctor_fails_when_ralph_path_is_a_live_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            codex_dir = workspace / '.codex'
            codex_dir.mkdir(parents=True, exist_ok=True)
            external_ralph = home / 'external-ralph'
            external_ralph.mkdir(parents=True, exist_ok=True)
            os.symlink(external_ralph, codex_dir / 'ralph')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Workspace:', doctor.stdout)
            self.assertIn('path component is a symlink', doctor.stdout)
            self.assertNotIn('[OK] Workspace:', doctor.stdout)

    def test_doctor_fails_on_symlinked_hook_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(install.returncode, 0, install.stderr)

            target_hooks = home / '.codex' / 'hooks' / 'ralph'
            external_hooks = home / 'external-hooks'
            external_hooks.mkdir(parents=True, exist_ok=True)
            for hook_name in ('stop_continue.py',):
                (external_hooks / hook_name).write_bytes((REPO_ROOT / 'hooks' / hook_name).read_bytes())

            for hook_path in target_hooks.iterdir():
                if hook_path.is_dir() and not hook_path.is_symlink():
                    shutil.rmtree(hook_path)
                else:
                    hook_path.unlink()
            target_hooks.rmdir()
            os.symlink(external_hooks, target_hooks)

            hooks_json_path = home / '.codex' / 'hooks.json'
            hooks_json = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            hooks_json['hooks']['Stop'][0]['hooks'][0]['command'] = f'python3 {external_hooks / "stop_continue.py"}'
            hooks_json_path.write_text(json.dumps(hooks_json, indent=2) + '\n', encoding='utf-8')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Hooks:', doctor.stdout)
            self.assertIn('hook directory path component is a symlink', doctor.stdout)

    def test_scripts_work_when_readlink_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace, tempfile.TemporaryDirectory() as tmp_bin:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env_without_readlink(home, Path(tmp_bin))

            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(install.returncode, 0, install.stderr)
            reinstall = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)

            start = self.run_script(START_SCRIPT, cwd=workspace, env=env, input_text='Ship the feature\n')
            self.assertEqual(start.returncode, 0, start.stderr)
            self.assertEqual(json.loads(start.stdout)['status'], 'started')

            resume = self.run_script(CONTINUE_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(resume.returncode, 0, resume.stderr)
            self.assertEqual(json.loads(resume.stdout)['status'], 'resumed')

            cancel = self.run_script(CANCEL_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(cancel.returncode, 0, cancel.stderr)
            self.assertEqual(json.loads(cancel.stdout)['status'], 'cleared')

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn('[OK] Hook Registry:', doctor.stdout)

    def test_start_script_rejects_workspace_override_but_allows_safe_loop_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            rejected = self.run_script(
                START_SCRIPT,
                cwd=workspace,
                env=env,
                args=['--cwd', str(REPO_ROOT)],
                input_text='Ship the feature\n',
            )

            self.assertEqual(rejected.returncode, 2)
            self.assertIn('always uses the current workspace', rejected.stderr)
            self.assertFalse((workspace / '.codex').exists())

            started = self.run_script(
                START_SCRIPT,
                cwd=workspace,
                env=env,
                args=['--max-iterations', '3', '--completion-token', '<done/>'],
                input_text='Ship the feature\n',
            )

            self.assertEqual(started.returncode, 0, started.stderr)
            payload = json.loads(started.stdout)
            self.assertEqual(payload['status'], 'started')
            self.assertEqual(payload['max_iterations'], 3)
            self.assertEqual(payload['completion_token'], '<done/>')

    def test_continue_script_rejects_workspace_override_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_root:
            home = Path(tmp_home)
            root = Path(tmp_root)
            workspace_a = root / 'workspace-a'
            workspace_b = root / 'workspace-b'
            workspace_a.mkdir()
            workspace_b.mkdir()
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            started = self.run_script(START_SCRIPT, cwd=workspace_a, env=env, input_text='Ship the feature\n')
            self.assertEqual(started.returncode, 0, started.stderr)

            rejected = self.run_script(
                CONTINUE_SCRIPT,
                cwd=workspace_b,
                env=env,
                args=['--cwd', str(workspace_a)],
            )

            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stderr.strip(), 'usage: continue_ralph.sh')
            self.assertFalse((workspace_b / '.codex').exists())

            state_path = workspace_a / '.codex' / 'ralph' / 'state.json'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertTrue(state['active'])
            self.assertEqual(state['phase'], 'running')
            self.assertEqual(state['iteration'], 0)

    def test_cancel_script_rejects_workspace_override_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_root:
            home = Path(tmp_home)
            root = Path(tmp_root)
            workspace_a = root / 'workspace-a'
            workspace_b = root / 'workspace-b'
            workspace_a.mkdir()
            workspace_b.mkdir()
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            started = self.run_script(START_SCRIPT, cwd=workspace_a, env=env, input_text='Ship the feature\n')
            self.assertEqual(started.returncode, 0, started.stderr)

            rejected = self.run_script(
                CANCEL_SCRIPT,
                cwd=workspace_b,
                env=env,
                args=['--cwd', str(workspace_a)],
            )

            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stderr.strip(), 'usage: cancel_ralph.sh')
            self.assertFalse((workspace_b / '.codex').exists())
            self.assertTrue((workspace_a / '.codex' / 'ralph' / 'state.json').exists())

    def test_cancel_script_clears_dangling_state_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            state_file = workspace / '.codex' / 'ralph' / 'state.json'
            state_file.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(workspace / 'missing-state.json', state_file)

            cancel = self.run_script(CANCEL_SCRIPT, cwd=workspace, env=env)

            self.assertEqual(cancel.returncode, 0, cancel.stderr)
            self.assertEqual(json.loads(cancel.stdout)['status'], 'cleared_invalid_state')
            self.assertFalse(state_file.exists())
            self.assertFalse(state_file.is_symlink())

    def test_install_repairs_legacy_unquoted_space_path_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_root) / 'home with space'
            home.mkdir()
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hooks_json_path = home / '.codex' / 'hooks.json'
            hooks_json = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            broken_command = f'python3 {home / ".codex" / "hooks" / "ralph" / "stop_continue.py"}'
            hooks_json['hooks']['Stop'][0]['hooks'][0]['command'] = broken_command
            hooks_json_path.write_text(json.dumps(hooks_json, indent=2) + '\n', encoding='utf-8')

            reinstall = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            self.assertEqual(reinstall.returncode, 0)
            self.assertIn('repaired Stop hook registration', reinstall.stdout)
            hooks_json_after = json.loads(hooks_json_path.read_text(encoding='utf-8'))
            stop_commands = [
                hook['command']
                for entry in hooks_json_after.get('hooks', {}).get('Stop', [])
                for hook in entry.get('hooks', [])
                if hook.get('type') == 'command'
            ]
            expected_path = str(home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py')
            self.assertEqual(stop_commands, [shlex.join(['python3', expected_path])])

            doctor = self.run_script(DOCTOR_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(doctor.returncode, 0)
            self.assertIn('[OK] Hook Registry:', doctor.stdout)

    def test_doctor_checks_explicit_workspace_when_invoked_from_skill_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            ralph_dir = workspace / '.codex' / 'ralph'
            ralph_dir.mkdir(parents=True, exist_ok=True)
            (ralph_dir / 'state.json').write_text(json.dumps({'active': 'yes'}) + '\n', encoding='utf-8')

            doctor = self.run_script(
                DOCTOR_SCRIPT,
                cwd=DOCTOR_SCRIPT.parent.parent,
                env=env,
                args=[str(workspace)],
            )

            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] State:', doctor.stdout)
            self.assertNotIn('no active state file present', doctor.stdout)

    def test_doctor_expands_quoted_tilde_in_explicit_workspace_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home:
            home = Path(tmp_home)
            workspace = home / 'my workspace'
            workspace.mkdir()
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            doctor = self.run_script(
                DOCTOR_SCRIPT,
                cwd=REPO_ROOT,
                env=env,
                args=['~/my workspace'],
            )

            self.assertEqual(doctor.returncode, 0)
            self.assertIn('[OK] Workspace:', doctor.stdout)
            self.assertNotIn('workspace path does not exist: ~/my workspace', doctor.stdout)

    def test_doctor_fails_on_missing_explicit_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            missing_workspace = workspace.parent / 'missing-workspace'
            doctor = self.run_script(
                DOCTOR_SCRIPT,
                cwd=workspace,
                env=env,
                args=[str(missing_workspace)],
            )

            self.assertEqual(doctor.returncode, 1)
            self.assertIn('[FAIL] Workspace:', doctor.stdout)
            self.assertIn(f'workspace path does not exist: {missing_workspace}', doctor.stdout)

    def test_uninstall_keeps_cleaning_when_hook_registry_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)

            hooks_json = home / '.codex' / 'hooks.json'
            hooks_json.write_text('{invalid\n', encoding='utf-8')

            uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)
            self.assertEqual(uninstall.returncode, 0)
            self.assertIn('left hooks.json unchanged', uninstall.stdout)
            self.assertFalse((home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            self.assertFalse((home / '.agents' / 'skills' / 'ralph-loop').exists())

    def test_uninstall_fails_closed_when_hook_registry_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(install.returncode, 0, install.stderr)

            hooks_json = home / '.codex' / 'hooks.json'
            original_registry = json.loads(hooks_json.read_text(encoding='utf-8'))
            os.chmod(hooks_json, 0)
            try:
                uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)
            finally:
                os.chmod(hooks_json, 0o600)

            self.assertNotEqual(uninstall.returncode, 0)
            self.assertIn('unable to verify Ralph Stop hook registration', uninstall.stderr)
            self.assertTrue((home / '.agents' / 'skills' / 'ralph-loop').is_symlink())
            self.assertTrue((home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            self.assertEqual(json.loads(hooks_json.read_text(encoding='utf-8')), original_registry)

    def test_uninstall_rolls_back_when_hook_registry_cannot_be_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(install.returncode, 0, install.stderr)

            hooks_json = home / '.codex' / 'hooks.json'
            readonly_dir = home / 'readonly-registry'
            readonly_dir.mkdir(parents=True, exist_ok=True)
            readonly_target = readonly_dir / 'hooks.json'
            readonly_target.write_text(hooks_json.read_text(encoding='utf-8'), encoding='utf-8')
            hooks_json.unlink()
            os.symlink(readonly_target, hooks_json)

            os.chmod(readonly_dir, 0o500)
            try:
                uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)
            finally:
                os.chmod(readonly_dir, 0o700)

            self.assertNotEqual(uninstall.returncode, 0)
            self.assertIn('unable to write', uninstall.stderr)
            self.assertTrue((home / '.agents' / 'skills' / 'ralph-loop').is_symlink())
            self.assertTrue((home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            registry = json.loads(readonly_target.read_text(encoding='utf-8'))
            self.assertIn('Stop', registry.get('hooks', {}))

    def test_uninstall_rolls_back_when_hook_file_removal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(install.returncode, 0, install.stderr)

            target_hooks = home / '.codex' / 'hooks' / 'ralph'
            os.chmod(target_hooks, 0o500)
            try:
                uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env)
            finally:
                os.chmod(target_hooks, 0o700)

            self.assertNotEqual(uninstall.returncode, 0)
            self.assertIn('Permission denied', uninstall.stderr)
            self.assertTrue((home / '.agents' / 'skills' / 'ralph-loop').is_symlink())
            self.assertTrue((home / '.codex' / 'hooks' / 'ralph' / 'stop_continue.py').exists())
            registry = json.loads((home / '.codex' / 'hooks.json').read_text(encoding='utf-8'))
            self.assertIn('Stop', registry.get('hooks', {}))

    def test_uninstall_hooks_only_fails_closed_on_symlinked_hook_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_workspace:
            home = Path(tmp_home)
            workspace = Path(tmp_workspace)
            env = self.make_env(home)
            install = self.run_script(INSTALL_SCRIPT, cwd=REPO_ROOT, env=env)
            self.assertEqual(install.returncode, 0, install.stderr)

            target_hooks = home / '.codex' / 'hooks' / 'ralph'
            external_hooks = home / 'external-hooks'
            external_hooks.mkdir(parents=True, exist_ok=True)
            for hook_name in ('stop_continue.py',):
                (external_hooks / hook_name).write_bytes((REPO_ROOT / 'hooks' / hook_name).read_bytes())

            for hook_path in target_hooks.iterdir():
                if hook_path.is_dir() and not hook_path.is_symlink():
                    shutil.rmtree(hook_path)
                else:
                    hook_path.unlink()
            target_hooks.rmdir()
            os.symlink(external_hooks, target_hooks)

            uninstall = self.run_script(UNINSTALL_SCRIPT, cwd=workspace, env=env, args=['--hooks-only'])

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertIn('removed Stop hook registration', uninstall.stdout)
            self.assertIn('left hook files unchanged', uninstall.stdout)
            self.assertIn('path component is a symlink', uninstall.stdout)
            for hook_name in ('stop_continue.py',):
                self.assertTrue((external_hooks / hook_name).exists())

            hooks_json = json.loads((home / '.codex' / 'hooks.json').read_text(encoding='utf-8'))
            self.assertNotIn('Stop', hooks_json.get('hooks', {}))


if __name__ == '__main__':
    unittest.main()
