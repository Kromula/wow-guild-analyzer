"""Tests for the Consumables Used metric (issue #16).

Healthstone/potion usage is item casts that WCL's aggregate Casts *table* omits,
so it's counted from the cast-EVENT stream filtered to consumable spell ids
resolved per report. These pin: name classification, id resolution, per-player
event counting, the fold into the casts frame, and the ascending ranking.
"""
from __future__ import annotations

import asyncio

import pytest

from app.checks.builtin.survival import Consumables
from app.config import settings
from app.ingest import fetcher
from app.ingest.fetcher import RawReport, Timeframe, _assign_consumables, _consumable_ids
from app.ingest.normalize import build_dataset, normalize_report
from app.survival_config import classify_ability

START = 1_000_000
TF = Timeframe(days=7, start_ms=START, end_ms=START + 120_000)


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = (settings.min_attendance_pct, settings.dedupe_overlapping_logs)
    settings.min_attendance_pct = 0.0          # include everyone in synthetic logs
    settings.dedupe_overlapping_logs = False
    yield
    settings.min_attendance_pct, settings.dedupe_overlapping_logs = saved


def test_classify_consumable_names():
    # Current-tier names resolve as consumables (the old list missed "Health Potion").
    assert classify_ability("Healthstone") == "consumable"
    assert classify_ability("Silvermoon Health Potion") == "consumable"
    assert classify_ability("Potent Healing Potion") == "consumable"
    # Making a healthstone is not using one.
    assert classify_ability("Create Healthstone") is None
    assert classify_ability("Soulburn: Healthstone") is None
    # Unrelated casts don't match.
    assert classify_ability("Frostbolt") is None


def test_consumable_ids_resolution():
    raw = RawReport(code="r", title="r", start_time=START, end_time=START, zone="z",
                    abilities=[
                        {"gameID": 6262, "name": "Healthstone"},
                        {"gameID": 1234768, "name": "Silvermoon Health Potion"},
                        {"gameID": 6201, "name": "Create Healthstone"},   # excluded
                        {"gameID": 116, "name": "Frostbolt"},             # not a consumable
                    ])
    assert _consumable_ids(raw) == {6262: "Healthstone", 1234768: "Silvermoon Health Potion"}


def test_assign_consumables_counts_cast_events(monkeypatch):
    """Per-player counts come from `type: cast` events, mapped sourceID -> player;
    begincast and pet/NPC sources are ignored."""
    raw = RawReport(code="r", title="r", start_time=START, end_time=START, zone="z",
                    players=[{"id": 1, "name": "Mage"}, {"id": 2, "name": "Priest"}],
                    abilities=[{"gameID": 6262, "name": "Healthstone"}])

    async def fake_events(client, code, data_type, start, end, fight_ids, ability_id=None, **kw):
        assert data_type == "Casts" and ability_id == 6262
        return [
            {"type": "cast", "sourceID": 1},
            {"type": "cast", "sourceID": 1},
            {"type": "begincast", "sourceID": 1},   # ignored (not a completed use)
            {"type": "cast", "sourceID": 2},
            {"type": "cast", "sourceID": 99},        # pet/NPC — not a player actor
        ]

    monkeypatch.setattr(fetcher, "_fetch_events", fake_events)

    async def guarded(coro):
        return await coro

    asyncio.run(_assign_consumables(guarded, object(), raw, 0.0, 1.0, [1]))
    by_player = {r["player"]: r["hits"] for r in raw.consumable_casts}
    assert by_player == {"Mage": 2.0, "Priest": 1.0}
    assert all(r["ability_name"] == "Healthstone" for r in raw.consumable_casts)


def _raw_with_consumables(code, consumable_casts):
    fights = [{"id": 1, "name": "Voidspire", "encounterID": 9999, "difficulty": 5,
               "kill": False, "startTime": START, "endTime": START + 60_000,
               "friendlyPlayers": [1, 2, 3]}]
    raw = RawReport(code=code, title=code, start_time=START, end_time=START + 120_000,
                    zone="VS / DR / MQD", fights=fights,
                    players=[{"id": 1, "name": "Mage", "subType": "Mage"},
                             {"id": 2, "name": "Priest", "subType": "Priest"},
                             {"id": 3, "name": "Rogue", "subType": "Rogue"}])
    raw.consumable_casts = consumable_casts
    return raw


def test_consumables_folded_into_casts_and_ranked():
    """End-to-end: consumable_casts land in ds.casts and the check ranks fewest-first,
    with the full roster shown (zero-users included and surfaced at the top)."""
    raw = _raw_with_consumables("r1", [
        {"player": "Mage", "ability_id": 6262, "ability_name": "Healthstone", "hits": 3.0},
        {"player": "Priest", "ability_id": 1234768, "ability_name": "Silvermoon Health Potion", "hits": 1.0},
        # Rogue used nothing.
    ])
    ds = build_dataset([raw], TF)

    consumable_casts = ds.casts.filter(ds.casts["ability_name"] == "Healthstone")
    assert consumable_casts.height == 1  # folded in

    res = Consumables().run(ds).to_dict()
    ranked = [(r["player"], r["value"]) for r in res["rows"]]
    assert ranked[0] == ("Rogue", 0.0)      # zero-user first
    assert ("Mage", 3.0) in ranked and ("Priest", 1.0) in ranked
    assert res["rows"][0]["detail"]          # zero-user flagged
