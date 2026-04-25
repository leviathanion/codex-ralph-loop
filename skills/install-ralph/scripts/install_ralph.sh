#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"
source "${ROOT_DIR}/skills/_shared/scripts/ralph_common.sh"

HOME_DIR="${HOME:?HOME is required}"
CODEX_HOME="$(ralph_profile_path "${CODEX_HOME:-${HOME_DIR}/.codex}" "${HOME_DIR}")"
AGENTS_HOME="$(ralph_profile_path "${AGENTS_HOME:-${HOME_DIR}/.agents}" "${HOME_DIR}")"

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

exec env PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m profile.installer install \
  --root-dir "${ROOT_DIR}" \
  --codex-home "${CODEX_HOME}" \
  --agents-home "${AGENTS_HOME}" \
  --mode "${MODE}"
