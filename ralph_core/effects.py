from __future__ import annotations

from ralph_core import storage
from ralph_core.model import RuntimeEffect


def apply_effects(effects: tuple[RuntimeEffect, ...], cwd: str) -> None:
    for effect in effects:
        if effect.kind == 'append_progress':
            if effect.progress is None:
                raise ValueError('append_progress effect requires progress payload')
            storage.append_progress_entry(effect.progress, cwd)
            continue
        if effect.kind == 'save_state':
            if effect.state is None:
                raise ValueError('save_state effect requires state payload')
            storage.save_state(effect.state, cwd)
            continue
        if effect.kind == 'clear_state':
            storage.clear_state(cwd)
            continue
        raise ValueError(f'unknown Ralph effect kind: {effect.kind!r}')

