from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))

import toml_feature_flag  # noqa: E402


class TomlFeatureFlagTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['python3', str(HOOKS_DIR / 'toml_feature_flag.py'), *args],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_codex_hooks_enabled_returns_false_when_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            self.assertFalse(toml_feature_flag.codex_hooks_enabled(config_toml))

    def test_ensure_creates_missing_features_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / '.codex' / 'config.toml'

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'created')
            self.assertTrue(toml_feature_flag.codex_hooks_enabled(config_toml))
            self.assertEqual(
                config_toml.read_text(encoding='utf-8'),
                '[features]\ncodex_hooks = true\n',
            )

    def test_ensure_collapses_duplicate_assignments_and_preserves_other_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                '[features]\n'
                'codex_hooks = false\n'
                '; keep this comment\n'
                'codex_hooks = true\n'
                'codex_hooks = false\n'
                '\n'
                '[other]\n'
                'value = 1\n',
                encoding='utf-8',
            )

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)
            contents = config_toml.read_text(encoding='utf-8')

            self.assertEqual(status, 'updated')
            self.assertEqual(contents.count('codex_hooks = true'), 1)
            self.assertNotIn('codex_hooks = false', contents)
            self.assertIn('; keep this comment', contents)
            self.assertIn('[other]\nvalue = 1\n', contents)

    def test_ensure_reports_deduplicated_when_true_entries_are_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                '[features]\n'
                'codex_hooks = true\n'
                '; keep this comment\n'
                'codex_hooks = true\n',
                encoding='utf-8',
            )

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'deduplicated')
            self.assertEqual(config_toml.read_text(encoding='utf-8').count('codex_hooks = true'), 1)

    def test_ensure_preserves_true_assignment_with_inline_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            original = '[features]\ncodex_hooks = true # enabled\n'
            config_toml.write_text(original, encoding='utf-8')

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'unchanged')
            self.assertEqual(config_toml.read_text(encoding='utf-8'), original)

    def test_ensure_preserves_existing_file_when_atomic_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            original = '[features]\ncodex_hooks = false\n'
            config_toml.write_text(original, encoding='utf-8')

            with mock.patch.object(toml_feature_flag.os, 'replace', side_effect=OSError('boom')):
                with self.assertRaisesRegex(OSError, 'boom'):
                    toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(config_toml.read_text(encoding='utf-8'), original)
            self.assertEqual(list(config_toml.parent.glob(f'.{config_toml.name}.*.tmp')), [])

    def test_ensure_preserves_symlinked_config_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / 'dotfiles' / 'config.toml'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('[features]\ncodex_hooks = false\n', encoding='utf-8')
            config_toml = root / '.codex' / 'config.toml'
            config_toml.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, config_toml)

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'updated')
            self.assertTrue(config_toml.is_symlink())
            self.assertEqual(config_toml.resolve(), target.resolve())
            self.assertEqual(
                target.read_text(encoding='utf-8'),
                '[features]\ncodex_hooks = true\n',
            )

    def test_ensure_updates_features_header_with_inline_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                '[features] # keep this comment\n'
                '\n'
                '[other] # keep this one too\n'
                'value = 1\n',
                encoding='utf-8',
            )

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'updated')
            self.assertEqual(
                config_toml.read_text(encoding='utf-8'),
                '[features] # keep this comment\n'
                'codex_hooks = true\n'
                '\n'
                '[other] # keep this one too\n'
                'value = 1\n',
            )

    def test_ensure_ignores_features_header_inside_multiline_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                'text = """\n'
                '[features]\n'
                'hello\n'
                '"""\n',
                encoding='utf-8',
            )

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'created')
            self.assertTrue(toml_feature_flag.codex_hooks_enabled(config_toml))
            self.assertEqual(
                config_toml.read_text(encoding='utf-8'),
                'text = """\n'
                '[features]\n'
                'hello\n'
                '"""\n'
                '\n'
                '[features]\n'
                'codex_hooks = true\n',
            )

    def test_ensure_ignores_codex_hooks_assignment_inside_multiline_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                '[features]\n'
                'notes = """\n'
                'codex_hooks = false\n'
                '"""\n',
                encoding='utf-8',
            )

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'updated')
            self.assertTrue(toml_feature_flag.codex_hooks_enabled(config_toml))
            self.assertEqual(
                config_toml.read_text(encoding='utf-8'),
                '[features]\n'
                'notes = """\n'
                'codex_hooks = false\n'
                '"""\n'
                'codex_hooks = true\n',
            )

    def test_ensure_replaces_multiline_codex_hooks_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                '[features]\n'
                'codex_hooks = """\n'
                'false\n'
                '"""\n'
                'other = 1\n',
                encoding='utf-8',
            )

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'updated')
            self.assertTrue(toml_feature_flag.codex_hooks_enabled(config_toml))
            self.assertEqual(
                config_toml.read_text(encoding='utf-8'),
                '[features]\n'
                'codex_hooks = true\n'
                'other = 1\n',
            )

    def test_ensure_rejects_unterminated_multiline_codex_hooks_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            original = '[features]\ncodex_hooks = """\nfalse\nother = 1\n'
            config_toml.write_text(original, encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'invalid TOML'):
                toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(config_toml.read_text(encoding='utf-8'), original)

    def test_codex_hooks_enabled_rejects_invalid_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                '[features]\n'
                'codex_hooks = true\n'
                'codex_hooks = false\n',
                encoding='utf-8',
            )

            with self.assertRaisesRegex(ValueError, 'invalid TOML'):
                toml_feature_flag.codex_hooks_enabled(config_toml)

    def test_ensure_rejects_unrepairable_invalid_toml_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            original = '[features\ncodex_hooks = true\n'
            config_toml.write_text(original, encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'invalid TOML'):
                toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(config_toml.read_text(encoding='utf-8'), original)

    def test_ensure_rejects_scalar_features_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            original = 'features = "disabled"\n'
            config_toml.write_text(original, encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'not an editable top-level table'):
                toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(config_toml.read_text(encoding='utf-8'), original)

    def test_ensure_appends_explicit_features_table_after_child_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            original = '[features.sub]\nvalue = 1\n'
            config_toml.write_text(original, encoding='utf-8')

            status = toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(status, 'created')
            self.assertEqual(
                config_toml.read_text(encoding='utf-8'),
                '[features.sub]\n'
                'value = 1\n'
                '\n'
                '[features]\n'
                'codex_hooks = true\n',
            )

    def test_ensure_rejects_dotted_features_key_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            original = 'features.sub = 1\n'
            config_toml.write_text(original, encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'not an editable top-level table'):
                toml_feature_flag.ensure_codex_hooks_enabled(config_toml)

            self.assertEqual(config_toml.read_text(encoding='utf-8'), original)

    def test_cli_supports_get_and_ensure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'

            before = self.run_cli('get', str(config_toml))
            self.assertEqual(before.returncode, 0)
            self.assertEqual(before.stdout.strip(), 'false')

            ensure = self.run_cli('ensure', str(config_toml))
            self.assertEqual(ensure.returncode, 0)
            self.assertEqual(ensure.stdout.strip(), 'created')

            after = self.run_cli('get', str(config_toml))
            self.assertEqual(after.returncode, 0)
            self.assertEqual(after.stdout.strip(), 'true')

    def test_cli_get_fails_on_invalid_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_toml = Path(tmpdir) / 'config.toml'
            config_toml.write_text(
                '[features]\n'
                'codex_hooks = true\n'
                'codex_hooks = false\n',
                encoding='utf-8',
            )

            result = self.run_cli('get', str(config_toml))

            self.assertEqual(result.returncode, 1)
            self.assertIn('invalid TOML', result.stderr)

    def test_main_uses_explicit_empty_argv_instead_of_process_arguments(self) -> None:
        stderr = io.StringIO()
        original_argv = sys.argv
        sys.argv = ['toml_feature_flag.py', 'get', '/tmp/config.toml']
        try:
            with redirect_stderr(stderr):
                result = toml_feature_flag.main([])
        finally:
            sys.argv = original_argv

        self.assertEqual(result, 2)
        self.assertIn('usage: toml_feature_flag.py', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
