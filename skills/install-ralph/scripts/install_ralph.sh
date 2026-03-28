#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SELF_DIR="$(cd -- "$(dirname -- "${SCRIPT_PATH}")" && pwd)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd)"
source "${ROOT_DIR}/skills/_shared/scripts/ralph_common.sh"

HOME_DIR="${HOME:?HOME is required}"
CODEX_HOME="${CODEX_HOME:-${HOME_DIR}/.codex}"
AGENTS_HOME="${AGENTS_HOME:-${HOME_DIR}/.agents}"

SKILLS_SOURCE="${ROOT_DIR}/skills"
HOOKS_SOURCE="${ROOT_DIR}/hooks"

USER_SKILLS="${AGENTS_HOME}/skills"
TARGET_HOOKS="${CODEX_HOME}/hooks/ralph"
HOOKS_JSON="${CODEX_HOME}/hooks.json"
CONFIG_TOML="${CODEX_HOME}/config.toml"

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

install_skills() {
  mkdir -p "${USER_SKILLS}"
  local skill_name source target resolved_target resolved_source
  for skill_name in "${RALPH_SKILL_NAMES[@]}"; do
    source="${SKILLS_SOURCE}/${skill_name}"
    target="${USER_SKILLS}/${skill_name}"
    if [[ -L "${target}" || -e "${target}" ]]; then
      if [[ -L "${target}" ]]; then
        resolved_target="$(readlink -f -- "${target}")"
        resolved_source="$(readlink -f -- "${source}")"
        if [[ "${resolved_target}" == "${resolved_source}" ]]; then
          continue
        fi
      fi
      echo "skill target already exists and is not the expected symlink: ${target}" >&2
      exit 1
    fi
    ln -s "${source}" "${target}"
    CHANGES+=("linked skill ${skill_name} -> ${target}")
  done
}

install_hooks() {
  mkdir -p "${TARGET_HOOKS}"
  local name
  for name in "${RALPH_HOOK_NAMES[@]}"; do
    cp "${HOOKS_SOURCE}/${name}" "${TARGET_HOOKS}/${name}"
    CHANGES+=("copied hook ${name}")
  done

  local stop_hook_script stop_command
  stop_hook_script="${TARGET_HOOKS}/${RALPH_STOP_HOOK_FILE}"
  stop_command="$(ralph_stop_hook_command "${stop_hook_script}")"

  ralph_register_stop_hook "${HOOKS_JSON}" "${stop_hook_script}"

  if ! grep -Fq "\"command\": \"${stop_command}\"" "${HOOKS_JSON}"; then
    echo "failed to register Stop hook" >&2
    exit 1
  fi

  CHANGES+=("registered Stop hook")
}

ensure_feature_flag() {
  mkdir -p "$(dirname -- "${CONFIG_TOML}")"
  if [[ -f "${CONFIG_TOML}" ]] && grep -Fq "codex_hooks = true" "${CONFIG_TOML}"; then
    return
  fi

  if [[ -f "${CONFIG_TOML}" ]] && grep -Fq "[features]" "${CONFIG_TOML}"; then
    python3 - "${CONFIG_TOML}" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
for idx, line in enumerate(lines):
    if line.strip() == "[features]":
        insert_at = idx + 1
        while insert_at < len(lines) and not lines[insert_at].startswith("["):
            insert_at += 1
        lines.insert(insert_at, "codex_hooks = true")
        path.write_text("\n".join(lines).rstrip() + "\n")
        break
PY
    CHANGES+=("enabled codex_hooks feature flag")
    return
  fi

  {
    if [[ -f "${CONFIG_TOML}" ]]; then
      cat "${CONFIG_TOML}"
      if [[ -s "${CONFIG_TOML}" ]]; then
        printf '\n'
      fi
    fi
    printf '[features]\n'
    printf 'codex_hooks = true\n'
  } > "${CONFIG_TOML}.tmp"
  mv "${CONFIG_TOML}.tmp" "${CONFIG_TOML}"
  CHANGES+=("created [features] section with codex_hooks = true")
}

case "${MODE}" in
  skills-only)
    install_skills
    ;;
  hooks-only)
    install_hooks
    ensure_feature_flag
    ;;
  all)
    install_skills
    install_hooks
    ensure_feature_flag
    ;;
esac

if [[ "${#CHANGES[@]}" -eq 0 ]]; then
  echo "Codex Ralph is already installed."
  exit 0
fi

echo "Installed Codex Ralph:"
for change in "${CHANGES[@]}"; do
  echo "- ${change}"
done
