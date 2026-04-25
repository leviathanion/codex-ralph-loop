from __future__ import annotations

from ralph_core import model, protocol, storage


class RalphCommonFacade:
    def __getattr__(self, name: str):
        for module in (storage, protocol, model):
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)


common = RalphCommonFacade()

