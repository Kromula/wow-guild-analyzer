"""Tests for the on-disk per-report store (issue #13 slice 2).

Round-trips ReportFrames through Parquet + meta.json and confirms that
assembling stored-then-loaded frames matches assembling the originals.
"""
from __future__ import annotations

import polars as pl
import pytest

from app import store
from app.config import settings
from app.ingest.fetcher import RawReport, Timeframe
from app.ingest.normalize import assemble, build_dataset, normalize_report

START = 1_000_000
TF = Timeframe(days=7, start_ms=START, end_ms=START + 120_000)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path):
    saved_dir, saved_att = settings.data_dir, settings.min_attendance_pct
    settings.data_dir = str(tmp_path)
    settings.min_attendance_pct = 0.0
    yield
    settings.data_dir, settings.min_attendance_pct = saved_dir, saved_att


def _raw(code, players, fights, deaths_by_fight=None):
    r = RawReport(code=code, title=f"{code}-title", start_time=START, end_time=START + 120_000,
                  zone="VS / DR / MQD", fights=fights, players=players)
    r.deaths_by_fight = deaths_by_fight or {}
    return r


def _fight(fid, friendly=None):
    return {"id": fid, "name": "Voidspire", "encounterID": 9999, "difficulty": 5,
            "kill": False, "startTime": START, "endTime": START + 60_000,
            "friendlyPlayers": friendly or []}


def _deaths(entries):
    return {"data": {"entries": entries}}


def test_round_trip_preserves_frames_and_meta():
    raw = _raw("abc",
               players=[{"id": 1, "name": "Mage"}, {"id": 2, "name": "Healer"}],
               fights=[_fight(10, friendly=[1, 2])],
               deaths_by_fight={10: _deaths([{"name": "Mage", "deathTime": START + 5000,
                                              "ability": {"name": "Fireball"}}])})
    rf = normalize_report(raw)
    store.store_report(rf, fetched_at=123.0)

    loaded = store.load_report("abc")
    assert loaded is not None
    assert (loaded.code, loaded.title, loaded.zone) == ("abc", "abc-title", "VS / DR / MQD")
    assert loaded.start_time == START and loaded.end_time == START + 120_000
    assert loaded.is_raid_night is True
    assert loaded.present == ["Healer", "Mage"]
    # Frames identical after the Parquet round-trip.
    for attr in store._FRAME_FILES:
        assert getattr(loaded, attr).to_dicts() == getattr(rf, attr).to_dicts()


def test_empty_frames_round_trip_with_schema():
    """A report with no deaths/damage still round-trips with correct (empty) schema."""
    raw = _raw("empty", players=[{"id": 1, "name": "Mage"}], fights=[_fight(10, friendly=[1])])
    rf = normalize_report(raw)
    store.store_report(rf, fetched_at=1.0)
    loaded = store.load_report("empty")
    assert loaded.deaths.is_empty()
    assert loaded.deaths.schema == rf.deaths.schema  # dtypes preserved, not Null columns


def test_stored_codes_and_all_meta():
    for code in ("r1", "r2"):
        store.store_report(normalize_report(_raw(code, [{"id": 1, "name": "Mage"}],
                                                 [_fight(10, friendly=[1])])), fetched_at=1.0)
    assert store.stored_codes() == {"r1", "r2"}
    assert {m["code"] for m in store.all_meta()} == {"r1", "r2"}


def test_missing_report_returns_none():
    assert store.load_report("nope") is None
    assert store.report_meta("nope") is None


def test_stale_schema_version_invalidates(monkeypatch):
    store.store_report(normalize_report(_raw("v", [{"id": 1, "name": "Mage"}],
                                             [_fight(10, friendly=[1])])), fetched_at=1.0)
    assert store.load_report("v") is not None
    monkeypatch.setattr(store, "SCHEMA_VERSION", store.SCHEMA_VERSION + 1)
    assert store.report_meta("v") is None      # version mismatch -> treated as absent
    assert store.load_report("v") is None
    assert store.stored_codes() == set()        # excluded from the valid set too


def test_assemble_from_store_matches_direct():
    """Store -> load -> assemble must equal build_dataset over the same raws."""
    raws = [
        _raw("a", [{"id": 1, "name": "Mage"}], [_fight(10, friendly=[1])],
             {10: _deaths([{"name": "Mage", "deathTime": START + 3000, "ability": {"name": "Cleave"}}])}),
        _raw("b", [{"id": 1, "name": "Mage"}, {"id": 2, "name": "Priest"}],
             [_fight(11, friendly=[1, 2])]),
    ]
    direct = build_dataset(raws, TF)
    for r in raws:
        store.store_report(normalize_report(r), fetched_at=1.0)
    from_store = assemble(store.load_reports(["a", "b"]), TF)

    assert from_store.fights.sort("fight_id").to_dicts() == direct.fights.sort("fight_id").to_dicts()
    assert from_store.deaths.sort(["report_code", "death_order"]).to_dicts() == \
        direct.deaths.sort(["report_code", "death_order"]).to_dicts()
    assert sorted(p["player"] for p in from_store.players.to_dicts()) == \
        sorted(p["player"] for p in direct.players.to_dicts())
