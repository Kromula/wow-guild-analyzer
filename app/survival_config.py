"""Shared survival-ability config: consumable/defensive name patterns and the
cast-name classifier.

Lives at the app root (not under app/checks) so both the ingest layer
(fetcher resolving consumable spell ids) and the checks layer (_util classifying
casts) can import it without an ingest -> checks import cycle. The data itself is
still edited in app/checks/builtin/survival_abilities.json.
"""
from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).parent / "checks" / "builtin" / "survival_abilities.json"
_DATA = json.loads(_PATH.read_text(encoding="utf-8"))

# Case-insensitive substring patterns matched against an ability's name.
CONSUMABLES = tuple(s.lower() for s in _DATA.get("consumables", []))
DEFENSIVES = tuple(s.lower() for s in _DATA.get("personal_defensives", []))


def classify_ability(name: str) -> str | None:
    """Tag an ability name as 'consumable', 'defensive', or None.

    Excludes "create"/"soulburn" casts so a Warlock conjuring a Healthstone (or
    Soulburn: Healthstone) isn't counted as *using* one — only consumption counts.
    """
    low = (name or "").lower()
    if "create" in low or "soulburn" in low:
        return None
    if any(p in low for p in CONSUMABLES):
        return "consumable"
    if any(p in low for p in DEFENSIVES):
        return "defensive"
    return None
