#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"
source "${ROOT_DIR}/skills/_shared/scripts/ralph_common.sh"

HOME_DIR="${HOME:?HOME is required}"
CODEX_HOME="$(ralph_profile_path "${CODEX_HOME:-${HOME_DIR}/.codex}" "${HOME_DIR}")"
AGENTS_HOME="$(ralph_profile_path "${AGENTS_HOME:-${HOME_DIR}/.agents}" "${HOME_DIR}")"

if [[ "$#" -gt 1 ]]; then
  echo "usage: doctor_ralph.sh [workspace-root]" >&2
  exit 2
fi

WORKSPACE_ROOT="${1:-${PWD}}"

PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m profile.doctor "${WORKSPACE_ROOT}" "${CODEX_HOME}" "${AGENTS_HOME}"
