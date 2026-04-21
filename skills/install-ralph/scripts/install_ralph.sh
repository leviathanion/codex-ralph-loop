#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"

HOME_DIR="${HOME:?HOME is required}"
CODEX_HOME="${CODEX_HOME:-${HOME_DIR}/.codex}"
AGENTS_HOME="${AGENTS_HOME:-${HOME_DIR}/.agents}"

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

exec python3 "${ROOT_DIR}/hooks/profile_installer.py" install \
  --root-dir "${ROOT_DIR}" \
  --codex-home "${CODEX_HOME}" \
  --agents-home "${AGENTS_HOME}" \
  --mode "${MODE}"
