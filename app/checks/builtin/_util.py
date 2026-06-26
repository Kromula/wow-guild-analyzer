"""Shared helpers for built-in checks."""
from __future__ import annotations

import json
from pathlib import Path

# Survival ability name lists (consumables + personal defensives). Editable
# without code changes — see survival_abilities.json. Matching is case-insensitive
# substring on the cast ability name.
_ABIL = json.loads((Path(__file__).parent / "survival_abilities.json").read_text(encoding="utf-8"))
CONSUMABLES = tuple(s.lower() for s in _ABIL.get("consumables", []))
DEFENSIVES = tuple(s.lower() for s in _ABIL.get("personal_defensives", []))


def classify_ability(name: str) -> str | None:
    """Tag a cast ability name as 'consumable', 'defensive', or None.

    Excludes "create" casts so a Warlock's *Create Healthstone* isn't counted as
    *using* a healthstone — only consumption/usage counts.
    """
    low = (name or "").lower()
    if "create" in low:
        return None
    if any(p in low for p in CONSUMABLES):
        return "consumable"
    if any(p in low for p in DEFENSIVES):
        return "defensive"
    return None


def fmt_num(n: float) -> str:
    """Compact human number: 1234567 -> '1.23M'."""
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.2f}{unit}"
    return f"{n:.0f}"


def fmt_rate(n: float, suffix: str) -> str:
    return f"{fmt_num(n)} {suffix}"
