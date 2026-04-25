#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"

FORWARDED_ARGS=()
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --max-iterations|--completion-token)
      if [[ "$#" -lt 2 ]]; then
        echo "missing value for $1" >&2
        exit 2
      fi
      FORWARDED_ARGS+=("$1" "$2")
      shift 2
      ;;
    --max-iterations=*|--completion-token=*)
      FORWARDED_ARGS+=("$1")
      shift
      ;;
    --cwd|--cwd=*|--prompt|--prompt=*)
      echo "start_ralph.sh always uses the current workspace and stdin prompt; $1 is not supported" >&2
      exit 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m ralph_core.control start --cwd "${PWD}" "${FORWARDED_ARGS[@]}"
