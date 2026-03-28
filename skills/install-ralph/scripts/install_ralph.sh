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
  local name source target register_status
  for name in "${RALPH_HOOK_NAMES[@]}"; do
    source="${HOOKS_SOURCE}/${name}"
    target="${TARGET_HOOKS}/${name}"
    if [[ ! -f "${target}" ]] || ! cmp -s "${source}" "${target}"; then
      cp "${source}" "${target}"
      CHANGES+=("copied hook ${name}")
    fi
  done

  local stop_hook_script stop_command
  stop_hook_script="${TARGET_HOOKS}/${RALPH_STOP_HOOK_FILE}"
  stop_command="$(ralph_stop_hook_command "${stop_hook_script}")"

  register_status="$(ralph_register_stop_hook "${HOOKS_JSON}" "${stop_hook_script}")"

  if ! grep -Fq "\"command\": \"${stop_command}\"" "${HOOKS_JSON}"; then
    echo "failed to register Stop hook" >&2
    exit 1
  fi

  if [[ "${register_status}" == "added" ]]; then
    CHANGES+=("registered Stop hook")
  fi
}

ensure_feature_flag() {
  mkdir -p "$(dirname -- "${CONFIG_TOML}")"
  local feature_status
  feature_status="$(python3 - "${CONFIG_TOML}" <<'PY'
import sys
from pathlib import Path
import re

path = Path(sys.argv[1])
header_pattern = re.compile(r"^\s*\[[^][]+\]\s*$")
assignment_pattern = re.compile(r"^\s*codex_hooks\s*=")

if path.exists():
    lines = path.read_text().splitlines()
else:
    lines = []

section_start = None
for idx, line in enumerate(lines):
    if line.strip() == "[features]":
        section_start = idx
        break

if section_start is None:
    new_lines = list(lines)
    if new_lines and new_lines[-1] != "":
        new_lines.append("")
    new_lines.extend(["[features]", "codex_hooks = true"])
    path.write_text("\n".join(new_lines).rstrip() + "\n")
    print("created")
    raise SystemExit(0)

section_end = len(lines)
for idx in range(section_start + 1, len(lines)):
    if header_pattern.match(lines[idx]):
        section_end = idx
        break

section_lines = lines[section_start + 1:section_end]
active_indexes = []
for idx, line in enumerate(section_lines):
    stripped = line.lstrip()
    if stripped.startswith("#") or stripped.startswith(";"):
        continue
    if assignment_pattern.match(line):
        active_indexes.append(idx)

updated_section = list(section_lines)
if not active_indexes:
    updated_section.append("codex_hooks = true")
    status = "updated"
else:
    first = active_indexes[0]
    updated_section[first] = "codex_hooks = true"
    for idx in reversed(active_indexes[1:]):
        updated_section.pop(idx)
    status = "unchanged" if section_lines == updated_section else "updated"

if status == "unchanged":
    print(status)
    raise SystemExit(0)

new_lines = lines[:section_start + 1] + updated_section + lines[section_end:]
path.write_text("\n".join(new_lines).rstrip() + "\n")
print(status)
PY
)"

  case "${feature_status}" in
    unchanged)
      ;;
    created)
      CHANGES+=("created [features] section with codex_hooks = true")
      ;;
    updated)
      CHANGES+=("enabled codex_hooks feature flag")
      ;;
    *)
      echo "unexpected feature flag update status: ${feature_status}" >&2
      exit 1
      ;;
  esac
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
