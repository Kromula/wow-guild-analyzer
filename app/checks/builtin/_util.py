"""Shared helpers for built-in checks."""
from __future__ import annotations

import polars as pl

from app.ingest.normalize import AnalysisDataset
# Survival ability name lists + classifier live in app.survival_config (shared with
# the ingest layer, which resolves consumable spell ids from the same patterns).
# Re-exported so existing `from app.checks.builtin._util import classify_ability`
# call sites keep working.
from app.survival_config import CONSUMABLES, DEFENSIVES, classify_ability  # noqa: F401


def fmt_num(n: float) -> str:
    """Compact human number: 1234567 -> '1.23M'."""
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.2f}{unit}"
    return f"{n:.0f}"


def fmt_rate(n: float, suffix: str) -> str:
    return f"{fmt_num(n)} {suffix}"


def _role_names(ds: AnalysisDataset, role: str) -> set[str]:
    """Players whose primary role across the window matches `role`, from WCL's own
    classification (the playerDetails buckets parsed in normalize). Returns empty
    when role data is unavailable, so callers can fall back gracefully."""
    if ds.players.is_empty() or "role" not in ds.players.columns:
        return set()
    return set(ds.players.filter(pl.col("role") == role).get_column("player").to_list())


def tank_names(ds: AnalysisDataset) -> set[str]:
    """Tanks — they don't compete on damage and soak mechanics by design, so they
    distort damage-done and damage-taken rankings alike."""
    return _role_names(ds, "tank")


def healer_names(ds: AnalysisDataset) -> set[str]:
    """Healers — used to keep the Top Healers grid to actual healers rather than
    DPS/tank off-healing or hybrid (e.g. Augmentation) output."""
    return _role_names(ds, "healer")
