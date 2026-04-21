#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"
source "${ROOT_DIR}/skills/_shared/scripts/ralph_common.sh"

HOME_DIR="${HOME:?HOME is required}"
CODEX_HOME="$(ralph_realpath "${CODEX_HOME:-${HOME_DIR}/.codex}")"
AGENTS_HOME="$(ralph_realpath "${AGENTS_HOME:-${HOME_DIR}/.agents}")"

USER_SKILLS="${AGENTS_HOME}/skills"
TARGET_HOOKS="${CODEX_HOME}/hooks/ralph"
HOOKS_JSON="${CODEX_HOME}/hooks.json"

MODE="all"
for arg in "$@"; do
  case "$arg" in
    --skills-only|--hooks-only)
      if [[ "${MODE}" != "all" ]]; then
        echo "choose at most one of --skills-only or --hooks-only" >&2
        exit 1
      fi
      MODE="${arg#--}"
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

declare -a CHANGES=()

managed_path_symlink_error() {
  python3 - "${ROOT_DIR}" "$1" "$2" <<'PY'
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
sys.path.insert(0, str(root_dir / 'hooks'))

from common import symlink_component_error

error = symlink_component_error(Path(sys.argv[2]), Path(sys.argv[3]))
if error is not None:
    print(error)
PY
}

uninstall_skills() {
  local skill_name target
  for skill_name in "${RALPH_SKILL_NAMES[@]}"; do
    target="${USER_SKILLS}/${skill_name}"
    if [[ -L "${target}" ]]; then
      rm -f "${target}"
      CHANGES+=("removed skill link ${target}")
    fi
  done
}

uninstall_hooks() {
  local name path hooks_dir_error
  hooks_dir_error="$(managed_path_symlink_error "${CODEX_HOME}" "hooks/ralph")"
  if [[ -n "${hooks_dir_error}" ]]; then
    # Trade-off: once the managed hook directory is path-aliased, even "cleanup" starts
    # depending on an external tree. Leave both files and hook registration untouched so
    # uninstall fails closed instead of following the symlink into somebody else's files.
    CHANGES+=("left hook files and registration unchanged (hook directory ${hooks_dir_error})")
    return
  fi

  for name in "${RALPH_HOOK_NAMES[@]}"; do
    path="${TARGET_HOOKS}/${name}"
    if [[ -f "${path}" ]]; then
      rm -f "${path}"
      CHANGES+=("removed hook file ${path}")
    fi
  done

  if [[ -f "${HOOKS_JSON}" ]]; then
    local unregister_status
    # Uninstall should keep cleaning local files even if hooks.json is already damaged.
    if unregister_status="$(ralph_unregister_stop_hook "${HOOKS_JSON}" "${TARGET_HOOKS}/${RALPH_STOP_HOOK_FILE}" 2>&1)"; then
      if [[ "${unregister_status}" == "removed" ]]; then
        CHANGES+=("removed Stop hook registration")
      fi
    elif [[ -n "${unregister_status}" ]]; then
      CHANGES+=("left hooks.json unchanged (${unregister_status})")
    fi
  fi
}

case "${MODE}" in
  skills-only)
    uninstall_skills
    ;;
  hooks-only)
    uninstall_hooks
    ;;
  all)
    uninstall_skills
    uninstall_hooks
    ;;
esac

if [[ "${#CHANGES[@]}" -eq 0 ]]; then
  echo "Nothing to uninstall."
  exit 0
fi

echo "Uninstalled Codex Ralph:"
for change in "${CHANGES[@]}"; do
  echo "- ${change}"
done
