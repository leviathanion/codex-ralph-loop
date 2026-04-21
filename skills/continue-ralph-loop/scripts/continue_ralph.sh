#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "${SELF_DIR}/../../.." && pwd -P)"

python3 "${ROOT_DIR}/hooks/loop_control.py" resume --cwd "${PWD}" "$@"
