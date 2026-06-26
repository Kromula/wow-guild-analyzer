"""Tests for the per-report / cross-report normalization split (issue #13 slice 1).

`build_dataset` is now `assemble([normalize_report(r) for r in raws], tf)`. These
pin the new seam: per-report frames are pure and self-contained, and the
cross-report assembly (attendance filter, primary-role pick, concatenation)
behaves as before.
"""
from __future__ import annotations

import polars as pl
import pytest

from app.config import settings
from app.ingest.fetcher import RawReport, Timeframe
from app.ingest.normalize import assemble, build_dataset, normalize_report

START = 1_000_000
TF = Timeframe(days=7, start_ms=START, end_ms=START + 120_000)


def _raw(code: str, players, fights, deaths_by_fight=None, player_details=None):
    r = RawReport(code=code, title=code, start_time=START, end_time=START + 120_000,
                  zone="VS / DR / MQD", fights=fights, players=players)
    r.deaths_by_fight = deaths_by_fight or {}
    r.player_details = player_details or {}
    return r


def _fight(fid, enc=9999, diff=5, kill=False, friendly=None):
    return {"id": fid, "name": "Voidspire", "encounterID": enc, "difficulty": diff,
            "kill": kill, "startTime": START, "endTime": START + 60_000,
            "friendlyPlayers": friendly or []}


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = settings.min_attendance_pct
    yield
    settings.min_attendance_pct = saved


def test_normalize_report_is_self_contained():
    settings.min_attendance_pct = 0.0
    raw = _raw("r1",
               players=[{"id": 1, "name": "Mage"}, {"id": 2, "name": "Healer"}],
               fights=[_fight(10, friendly=[1, 2])])
    rf = normalize_report(raw)
    assert rf.code == "r1" and rf.zone == "VS / DR / MQD"
    assert rf.is_raid_night is True
    assert rf.present == ["Healer", "Mage"]  # sorted, mapped from friendlyPlayers
    assert rf.fights.height == 1
    assert set(rf.players["player"].to_list()) == {"Mage", "Healer"}


def test_build_dataset_delegates_to_assemble():
    """build_dataset must equal assembling the per-report frames directly."""
    settings.min_attendance_pct = 0.0
    raws = [
        _raw("r1", players=[{"id": 1, "name": "Mage"}], fights=[_fight(10, friendly=[1])]),
        _raw("r2", players=[{"id": 1, "name": "Mage"}], fights=[_fight(11, friendly=[1])]),
    ]
    via_build = build_dataset(raws, TF)
    via_assemble = assemble([normalize_report(r) for r in raws], TF)
    assert via_build.fights.sort("fight_id").to_dicts() == via_assemble.fights.sort("fight_id").to_dicts()
    assert via_build.reports == via_assemble.reports
    assert via_build.fights.height == 2  # concatenated across both reports


def test_assemble_attendance_filter_drops_low_attendance():
    """A player present on only 1 of 3 nights is filtered when the threshold is 0.5
    (cutoff = 0.5 * 3 = 1.5 nights)."""
    settings.min_attendance_pct = 0.5
    raws = [
        _raw("n1", players=[{"id": 1, "name": "Regular"}, {"id": 2, "name": "Pug"}],
             fights=[_fight(10, friendly=[1, 2])]),
        _raw("n2", players=[{"id": 1, "name": "Regular"}], fights=[_fight(11, friendly=[1])]),
        _raw("n3", players=[{"id": 1, "name": "Regular"}], fights=[_fight(12, friendly=[1])]),
    ]
    ds = build_dataset(raws, TF)
    players = set(ds.players["player"].to_list())
    assert "Regular" in players      # present all 3 nights
    assert "Pug" not in players      # present 1 of 3 nights, below 0.5


def test_assemble_primary_role_picked_across_reports():
    """Role counts accumulate across reports; the most-played role wins."""
    settings.min_attendance_pct = 0.0
    details_tank = {"data": {"playerDetails": {"tanks": [{"name": "Flex", "specs": [{"count": 1}]}]}}}
    details_dps = {"data": {"playerDetails": {"dps": [{"name": "Flex", "specs": [{"count": 5}]}]}}}
    raws = [
        _raw("r1", players=[{"id": 1, "name": "Flex"}], fights=[_fight(10, friendly=[1])],
             player_details=details_tank),
        _raw("r2", players=[{"id": 1, "name": "Flex"}], fights=[_fight(11, friendly=[1])],
             player_details=details_dps),
    ]
    ds = build_dataset(raws, TF)
    role = ds.players.filter(pl.col("player") == "Flex")["role"].to_list()[0]
    assert role == "dps"  # 5 dps fights vs 1 tank fight
