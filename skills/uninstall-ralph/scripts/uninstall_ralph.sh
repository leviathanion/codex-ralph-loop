#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SELF_DIR="$(cd -- "$(dirname -- "${SCRIPT_PATH}")" && pwd)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd)"
source "${ROOT_DIR}/skills/_shared/scripts/ralph_common.sh"

HOME_DIR="${HOME:?HOME is required}"
CODEX_HOME="${CODEX_HOME:-${HOME_DIR}/.codex}"
AGENTS_HOME="${AGENTS_HOME:-${HOME_DIR}/.agents}"

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
  local name path
  for name in "${RALPH_HOOK_NAMES[@]}"; do
    path="${TARGET_HOOKS}/${name}"
    if [[ -f "${path}" ]]; then
      rm -f "${path}"
      CHANGES+=("removed hook file ${path}")
    fi
  done

  if [[ -f "${HOOKS_JSON}" ]]; then
    while IFS= read -r line; do
      [[ -n "${line}" ]] && CHANGES+=("removed ${line} hook registration")
    done < <(ralph_unregister_stop_hook "${HOOKS_JSON}" "${TARGET_HOOKS}/${RALPH_STOP_HOOK_FILE}")
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
