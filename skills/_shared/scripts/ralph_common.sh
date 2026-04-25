#!/usr/bin/env bash

_ralph_manifest_shell="$(PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 - "${ROOT_DIR}" <<'PY'
import shlex
import sys

from profile.package_manifest import RUNTIME_PACKAGE_DIRS, SKILL_NAMES, STOP_HOOK_FILE, STOP_HOOK_FILES


def emit_array(name: str, values) -> None:
    quoted = ' '.join(shlex.quote(v) for v in values)
    print(f'{name}=({quoted})')


emit_array('RALPH_SKILL_NAMES', SKILL_NAMES)
emit_array('RALPH_STOP_HOOK_FILES', STOP_HOOK_FILES)
emit_array('RALPH_RUNTIME_PACKAGE_DIRS', RUNTIME_PACKAGE_DIRS)
print(f'RALPH_STOP_HOOK_FILE={shlex.quote(STOP_HOOK_FILE)}')
PY
)" || { echo "failed to load Ralph manifest from ${ROOT_DIR}/profile/package_manifest.py" >&2; exit 1; }
eval "${_ralph_manifest_shell}"
unset _ralph_manifest_shell

ralph_realpath() {
  python3 - "$1" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

ralph_profile_path() {
  python3 - "$1" "$2" <<'PY'
import sys
from pathlib import Path

raw_path = Path(sys.argv[1]).expanduser()
home_dir = Path(sys.argv[2]).expanduser().resolve(strict=False)

if raw_path.is_absolute():
    print(raw_path.resolve(strict=False))
else:
    print((home_dir / raw_path).resolve(strict=False))
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
  PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m profile.hook_registry register "${hooks_json}" "${stop_command}"
}

ralph_unregister_stop_hook() {
  local hooks_json="$1"
  local stop_hook_script="$2"
  local stop_command
  stop_command="$(ralph_stop_hook_command "${stop_hook_script}")"
  PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m profile.hook_registry unregister "${hooks_json}" "${stop_command}"
}
