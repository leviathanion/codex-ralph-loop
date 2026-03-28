#!/usr/bin/env bash

RALPH_SKILL_NAMES=(
  ralph-loop
  continue-ralph-loop
  ralph-help
  cancel-ralph
  install-ralph
  uninstall-ralph
)

RALPH_HOOK_NAMES=(common.py stop_continue.py)
RALPH_STOP_HOOK_FILE="stop_continue.py"

ralph_stop_hook_command() {
  printf 'python3 %s' "$1"
}

ralph_register_stop_hook() {
  local hooks_json="$1"
  local stop_hook_script="$2"
  local stop_command
  stop_command="$(ralph_stop_hook_command "${stop_hook_script}")"

  python3 - "${hooks_json}" "${stop_command}" <<'PY'
import json
import sys
from pathlib import Path

hooks_path = Path(sys.argv[1])
stop_command = sys.argv[2]

try:
    data = json.loads(hooks_path.read_text()) if hooks_path.exists() else {"hooks": {}}
except Exception:
    data = {"hooks": {}}

registry = data.setdefault("hooks", {})
entries = registry.setdefault("Stop", [])
found = any(
    hook.get("type") == "command" and hook.get("command") == stop_command
    for entry in entries
    for hook in entry.get("hooks", [])
)
if not found:
    entries.append({"hooks": [{"type": "command", "command": stop_command}]})

hooks_path.parent.mkdir(parents=True, exist_ok=True)
hooks_path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
print("unchanged" if found else "added")
PY
}

ralph_unregister_stop_hook() {
  local hooks_json="$1"
  local stop_hook_script="$2"
  local stop_command
  stop_command="$(ralph_stop_hook_command "${stop_hook_script}")"

  python3 - "${hooks_json}" "${stop_command}" <<'PY'
import json
import sys
from pathlib import Path

hooks_path = Path(sys.argv[1])
stop_command = sys.argv[2]

try:
    data = json.loads(hooks_path.read_text())
except Exception:
    raise SystemExit(0)

registry = data.get("hooks", {})

changed = []
entries = registry.get("Stop", [])
filtered = []
removed_any = False
for entry in entries:
    remaining_hooks = [
        hook for hook in entry.get("hooks", [])
        if not (hook.get("type") == "command" and hook.get("command") == stop_command)
    ]
    if len(remaining_hooks) != len(entry.get("hooks", [])):
        removed_any = True
    if remaining_hooks:
        entry = dict(entry)
        entry["hooks"] = remaining_hooks
        filtered.append(entry)
if filtered:
    registry["Stop"] = filtered
elif "Stop" in registry:
    registry.pop("Stop", None)
if removed_any:
    changed.append("Stop")

hooks_path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
print("\n".join(changed))
PY
}
