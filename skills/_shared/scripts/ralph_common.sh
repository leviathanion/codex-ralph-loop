#!/usr/bin/env bash

_ralph_manifest_shell="$(python3 - "${ROOT_DIR}" <<'PY'
import shlex
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
sys.path.insert(0, str(root_dir / 'hooks'))

from package_manifest import HOOK_NAMES, SKILL_NAMES, STOP_HOOK_FILE


def emit_array(name: str, values) -> None:
    quoted = ' '.join(shlex.quote(v) for v in values)
    print(f'{name}=({quoted})')


emit_array('RALPH_SKILL_NAMES', SKILL_NAMES)
emit_array('RALPH_HOOK_NAMES', HOOK_NAMES)
print(f'RALPH_STOP_HOOK_FILE={shlex.quote(STOP_HOOK_FILE)}')
PY
)" || { echo "failed to load Ralph manifest from ${ROOT_DIR}/hooks/package_manifest.py" >&2; exit 1; }
eval "${_ralph_manifest_shell}"
unset _ralph_manifest_shell

ralph_realpath() {
  python3 - "$1" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

ralph_stop_hook_command() {
  local script_path
  script_path="$(ralph_realpath "$1")"
  python3 - "${script_path}" <<'PY'
import shlex
import sys

print(shlex.join(['python3', sys.argv[1]]))
PY
}

ralph_register_stop_hook() {
  local hooks_json="$1"
  local stop_hook_script="$2"
  local stop_command
  stop_command="$(ralph_stop_hook_command "${stop_hook_script}")"
  python3 "${ROOT_DIR}/hooks/hook_registry.py" register "${hooks_json}" "${stop_command}"
}

ralph_unregister_stop_hook() {
  local hooks_json="$1"
  local stop_hook_script="$2"
  local stop_command
  stop_command="$(ralph_stop_hook_command "${stop_hook_script}")"
  python3 "${ROOT_DIR}/hooks/hook_registry.py" unregister "${hooks_json}" "${stop_command}"
}
