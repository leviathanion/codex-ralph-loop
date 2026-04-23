#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"

if [[ "$#" -ne 0 ]]; then
  echo "usage: cancel_ralph.sh" >&2
  exit 2
fi

python3 "${ROOT_DIR}/hooks/loop_control.py" cancel --cwd "${PWD}"
