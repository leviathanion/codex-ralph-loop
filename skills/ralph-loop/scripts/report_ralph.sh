#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"

SESSION_ARGS=()
RALPH_DETECTED_SESSION_ID="${RALPH_SESSION_ID:-${CODEX_SESSION_ID:-${CODEX_THREAD_ID:-}}}"
if [[ -n "${RALPH_DETECTED_SESSION_ID}" ]]; then
  SESSION_ARGS=(--session-id "${RALPH_DETECTED_SESSION_ID}")
fi

PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m ralph_core.control report --cwd "${PWD}" "${SESSION_ARGS[@]}" "$@"
