"""Tests for per-encounter (boss-panel) storage and store-backed boss panels (#13)."""
from __future__ import annotations

import asyncio

import pytest

from app import service, store
from app.config import settings
from app.ingest.boss import boss_summary_from_frames
from app.ingest.fetcher import RawReport
from app.ingest.normalize import normalize_report

START = 1_700_000_000_000


def _fight(fid, enc=9999, kill=False, pct=None, dur_s=60):
    return {"id": fid, "name": "Voidspire", "encounterID": enc, "difficulty": 5, "kill": kill,
            "startTime": START, "endTime": START + dur_s * 1000, "fightPercentage": pct,
            "friendlyPlayers": [1]}


def _raw(code, fights, deaths_by_fight=None):
    r = RawReport(code=code, title=code, start_time=START, end_time=START + 3_600_000,
                  zone="VS / DR / MQD", fights=fights, players=[{"id": 1, "name": "Mage"}])
    r.deaths_by_fight = deaths_by_fight or {}
    return r


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "min_attendance_pct", 0.0)
    monkeypatch.setattr(settings, "dedupe_overlapping_logs", False)
    service._boss_cache.clear()
    yield
    service._boss_cache.clear()


def test_boss_summary_from_frames():
    rf = normalize_report(_raw("r", [_fight(1, kill=True, dur_s=50), _fight(2, kill=False, pct=12.5)]))
    s = boss_summary_from_frames([rf], 9999)
    assert s["pulls"] == 2 and s["kills"] == 1 and s["wipes"] == 1
    assert s["best_kill_s"] == 50.0
    assert s["best_wipe_pct"] == 12.5
    assert s["zone"] == "VS / DR / MQD"


def test_encounter_store_round_trip():
    rf = normalize_report(_raw("r", [_fight(1, kill=True)]))
    store.store_report(rf, fetched_at=1.0, encounters={9999: rf})
    assert store.encounter_is_cached(9999) is True
    assert store.encounter_is_cached(1234) is False
    loaded = store.load_encounter("r", 9999)
    assert loaded is not None
    assert loaded.fights.to_dicts() == rf.fights.to_dicts()
    assert store.load_encounter("r", 1234) is None
    assert len(store.load_encounter_frames(9999)) == 1


def test_attach_encounters_adds_to_existing_report():
    rf = normalize_report(_raw("r", [_fight(1)]))
    store.store_report(rf, fetched_at=1.0)            # aggregate only
    assert store.encounter_is_cached(9999) is False
    store.attach_encounters("r", {9999: rf})          # add boss frames later
    assert store.encounter_is_cached(9999) is True
    assert store.load_encounter("r", 9999) is not None


def test_boss_panel_served_from_store(monkeypatch):
    async def boom(tf, enc):
        raise AssertionError("analyze_boss (live) must not be called when cached")
    monkeypatch.setattr(service, "analyze_boss", boom)

    rf = normalize_report(_raw("r", [_fight(1, kill=True, dur_s=40), _fight(2, pct=8.0)]))
    store.store_report(rf, fetched_at=1.0, encounters={9999: rf})
    panel = asyncio.run(service.boss_panel(0, 9999))
    assert panel["boss"]["pulls"] == 2 and panel["boss"]["kills"] == 1
    assert isinstance(panel["checks"], list)
    assert panel["timeframe_days"] == 0


def test_boss_panel_falls_back_to_live_when_uncached(monkeypatch):
    async def fake_live(tf, enc):
        return {"boss": {"name": "Live", "pulls": 1}, "checks": []}
    monkeypatch.setattr(service, "analyze_boss", fake_live)
    # Nothing stored for this encounter -> live path.
    panel = asyncio.run(service.boss_panel(0, 9999))
    assert panel["boss"]["name"] == "Live"
