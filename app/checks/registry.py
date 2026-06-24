"""Auto-discovering registry for checks.

`@register` records a Check subclass. `discover()` imports every module under
`app/checks/builtin/` so their `@register` decorators fire. `run_all()` executes
every registered check against a dataset and returns their results.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Type

from app.checks.base import Check, CheckResult
from app.ingest.normalize import AnalysisDataset

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Type[Check]] = {}
_discovered = False


def register(cls: Type[Check]) -> Type[Check]:
    if not cls.id:
        raise ValueError(f"Check {cls.__name__} must define a non-empty `id`.")
    if cls.id in _REGISTRY:
        raise ValueError(f"Duplicate check id '{cls.id}' ({cls.__name__}).")
    _REGISTRY[cls.id] = cls
    return cls


def discover() -> None:
    global _discovered
    if _discovered:
        return
    import app.checks.builtin as builtin_pkg

    for mod in pkgutil.iter_modules(builtin_pkg.__path__):
        importlib.import_module(f"{builtin_pkg.__name__}.{mod.name}")
    _discovered = True
    logger.info("Discovered %d checks: %s", len(_REGISTRY), ", ".join(sorted(_REGISTRY)))


def list_checks() -> list[dict]:
    discover()
    items = []
    for cls in sorted(_REGISTRY.values(), key=lambda c: (c.order, c.id)):
        items.append({
            "id": cls.id,
            "name": cls.name,
            "description": cls.description,
            "category": cls.category.value,
        })
    return items


def run_all(ds: AnalysisDataset, only: list[str] | None = None) -> list[CheckResult]:
    discover()
    results: list[CheckResult] = []
    selected = sorted(_REGISTRY.values(), key=lambda c: (c.order, c.id))
    for cls in selected:
        if only and cls.id not in only:
            continue
        try:
            results.append(cls().run(ds))
        except Exception:  # one bad check shouldn't kill the dashboard
            logger.exception("Check '%s' failed", cls.id)
    return results
