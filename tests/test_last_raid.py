"""Tests for the 'Last' timeframe filter (issue #18).

`days == LAST_RAID` (-1) scopes to the most recent raid night: the newest
raid-night report plus any other report overlapping its window (co-loggers of
the same evening), and nothing from earlier nights.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import service, store
from app.config import settings
from app.ingest.fetcher import RawReport
from app.ingest.normalize import normalize_report
from app.service import LAST_RAID, _latest_raid_window, _timeframe

HOUR = 3_600_000
DAY = 86_400_000
T0 = 1_700_000_000_000  # a fixed "newest night" anchor (ms)


def _store(code, start_ms, *, hours=3, zone="VS / DR / MQD", fight_ids=(1,)):
    end = start_ms + hours * HOUR
    fights = [{"id": f, "name": "Voidspire", "encounterID": 9999, "difficulty": 5,
               "kill": False, "startTime": start_ms, "endTime": start_ms + 60_000,
               "friendlyPlayers": [1]} for f in fight_ids]
    raw = RawReport(code=code, title=code, start_time=start_ms, end_time=end,
                    zone=zone, fights=fights, players=[{"id": 1, "name": "Mage"}])
    store.store_report(normalize_report(raw), fetched_at=time.time())


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "min_attendance_pct", 0.0)
    monkeypatch.setattr(settings, "dedupe_overlapping_logs", True)
    monkeypatch.setattr(settings, "cache_boss_panels", False)
    service._cache.clear()
    yield
    service._cache.clear()


def test_empty_store_falls_back_to_all_time():
    assert _latest_raid_window() is None
    assert _timeframe(LAST_RAID).is_all_time      # safe fallback, not a crash


def test_picks_only_the_most_recent_night(monkeypatch):
    monkeypatch.setattr(service, "fetch_dataset",
                        lambda tf: (_ for _ in ()).throw(AssertionError("no live fetch")))
    _store("old1", T0 - 7 * DAY, fight_ids=(1, 2))
    _store("old2", T0 - 3 * DAY, fight_ids=(3,))
    _store("latest", T0, fight_ids=(4, 5, 6))      # newest night

    ds = asyncio.run(service.get_dataset(LAST_RAID))
    assert {r["code"] for r in ds.reports} == {"latest"}
    assert ds.fights.height == 3                    # only the latest night's pulls

    win = _latest_raid_window()
    assert win[0] == T0 and not _timeframe(LAST_RAID).is_all_time


def test_includes_co_logger_overlapping_same_night(monkeypatch):
    monkeypatch.setattr(service, "fetch_dataset",
                        lambda tf: (_ for _ in ()).throw(AssertionError("no live fetch")))
    # Two raiders logged the same night; the co-logger started an hour later and
    # has MORE fights, so dedupe keeps it — but both must be inside the window.
    _store("main", T0, hours=3, fight_ids=(1, 2))
    _store("colog", T0 + HOUR, hours=3, fight_ids=(3, 4, 5))
    _store("prevnight", T0 - 2 * DAY, fight_ids=(9,))

    win = _latest_raid_window()
    assert win[0] == T0                             # window starts at the earliest of the night
    assert win[1] == T0 + HOUR + 3 * HOUR           # ...and ends at the latest

    ds = asyncio.run(service.get_dataset(LAST_RAID))
    # prevnight excluded; the night's canonical (most fights = colog) kept.
    assert "prevnight" not in {r["code"] for r in ds.reports}
    assert "colog" in {r["code"] for r in ds.reports}
