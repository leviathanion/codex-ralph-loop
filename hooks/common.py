from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

STATE_RELATIVE_PATH = Path('.codex/ralph/state.json')
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_COMPLETION_TOKEN = '<promise>DONE</promise>'


def workspace_root() -> Path:
    return Path(os.environ.get('PWD') or os.getcwd())


def state_path(cwd: str | None = None) -> Path:
    root = Path(cwd) if cwd else workspace_root()
    return root / STATE_RELATIVE_PATH


def default_state() -> Dict[str, Any]:
    return {
        'active': False,
        'prompt': '',
        'iteration': 0,
        'max_iterations': DEFAULT_MAX_ITERATIONS,
        'completion_token': DEFAULT_COMPLETION_TOKEN,
        'claimed_session_id': None,
    }


def load_state(cwd: str | None = None) -> Dict[str, Any]:
    path = state_path(cwd)
    if not path.exists():
        return default_state()
    try:
        data = json.loads(path.read_text())
    except Exception:
        state = default_state()
        state['parse_error'] = True
        return state
    state = default_state()
    state.update(data if isinstance(data, dict) else {})
    return state


def save_state(state: Dict[str, Any], cwd: str | None = None) -> Path:
    path = state_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True) + '\n')
    return path


def clear_state(cwd: str | None = None) -> None:
    path = state_path(cwd)
    if path.exists():
        path.unlink()


def completion_token(state: Dict[str, Any]) -> str:
    token = state.get('completion_token')
    return token if isinstance(token, str) and token else DEFAULT_COMPLETION_TOKEN
