"""Codex Ralph profile tooling.

This package name intentionally matches the architecture document. Python's stdlib also has a
``profile`` module; proxy missing attributes to that module so placing this repository first on
``sys.path`` does not break stdlib callers such as ``cProfile``.
"""

from __future__ import annotations

import importlib.util
import sysconfig
from pathlib import Path
from typing import Any

_stdlib_profile_path = Path(sysconfig.get_path('stdlib')) / 'profile.py'
_stdlib_spec = importlib.util.spec_from_file_location('_stdlib_profile', _stdlib_profile_path)
if _stdlib_spec is not None and _stdlib_spec.loader is not None:
    _stdlib_profile = importlib.util.module_from_spec(_stdlib_spec)
    _stdlib_spec.loader.exec_module(_stdlib_profile)
    __all__ = tuple(getattr(_stdlib_profile, '__all__', ()))
    for _name in __all__:
        globals()[_name] = getattr(_stdlib_profile, _name)


def __getattr__(name: str) -> Any:
    try:
        return getattr(_stdlib_profile, name)
    except NameError as exc:  # pragma: no cover - stdlib profile.py should always be present.
        raise AttributeError(name) from exc
    except AttributeError as exc:
        raise AttributeError(name) from exc


def __dir__() -> list[str]:
    names = set(globals())
    try:
        names.update(dir(_stdlib_profile))
    except NameError:  # pragma: no cover - stdlib profile.py should always be present.
        pass
    return sorted(names)
