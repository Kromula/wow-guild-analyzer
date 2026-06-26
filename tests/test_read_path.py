"""Tests for the store-backed read path (issue #13 slice 5).

get_dataset serves the overall analysis from the local store with zero WCL
calls; it falls back to a live fetch only when the store is empty.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import service, store
from app.config import settings
from app.ingest.fetcher import RawReport
from app.ingest.normalize import normalize_report

NOW_MS = int(time.time() * 1000)
DAY = 86_400_000


def _store_report(code, age_days, fight_ids=(1,)):
    start = NOW_MS - int(age_days * DAY)
    fights = [{"id": f, "name": "Voidspire", "encounterID": 9999, "difficulty": 5,
               "kill": False, "startTime": start, "endTime": start + 60_000,
               "friendlyPlayers": [1]} for f in fight_ids]
    raw = RawReport(code=code, title=code, start_time=start, end_time=start + 3_600_000,
                    zone="VS / DR / MQD", fights=fights, players=[{"id": 1, "name": "Mage"}])
    store.store_report(normalize_report(raw), fetched_at=time.time())


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "min_attendance_pct", 0.0)
    monkeypatch.setattr(settings, "dedupe_overlapping_logs", False)
    service._cache.clear()
    service._boss_list_cache.clear()
    service._boss_cache.clear()
    yield
    service._cache.clear()


def test_get_dataset_reads_store_without_wcl(monkeypatch):
    async def boom(tf):
        raise AssertionError("fetch_dataset must not be called when the store has data")
    monkeypatch.setattr(service, "fetch_dataset", boom)

    _store_report("a", age_days=1, fight_ids=(1, 2))
    _store_report("b", age_days=3, fight_ids=(3,))
    ds = asyncio.run(service.get_dataset(0))   # all-time
    assert ds.fights.height == 3
    assert {r["code"] for r in ds.reports} == {"a", "b"}


def test_window_filters_old_reports(monkeypatch):
    monkeypatch.setattr(service, "fetch_dataset",
                        lambda tf: (_ for _ in ()).throw(AssertionError("no fetch")))
    _store_report("recent", age_days=2, fight_ids=(1,))
    _store_report("old", age_days=40, fight_ids=(2,))
    ds7 = asyncio.run(service.get_dataset(7))   # last 7 days
    assert {r["code"] for r in ds7.reports} == {"recent"}
    ds_all = asyncio.run(service.get_dataset(0))
    assert {r["code"] for r in ds_all.reports} == {"recent", "old"}


def test_empty_store_falls_back_to_live(monkeypatch):
    called = {}

    async def fake_fetch(tf):
        called["yes"] = True
        return []  # build_dataset over no raws -> empty dataset

    monkeypatch.setattr(service, "fetch_dataset", fake_fetch)
    ds = asyncio.run(service.get_dataset(0))
    assert called.get("yes") is True
    assert ds.fights.height == 0


def test_sync_invalidates_dataset_cache(monkeypatch):
    # Prime the cache with a stale dataset object.
    service._cache[0] = (time.time(), "STALE")

    async def fake_list(tf):
        return [{"code": "new", "title": "new", "startTime": NOW_MS, "endTime": NOW_MS + 1,
                 "zone": {"id": 46, "name": "VS / DR / MQD"}}]

    async def fake_reports(metas):
        return [RawReport(code="new", title="new", start_time=NOW_MS, end_time=NOW_MS + 1,
                          zone="VS / DR / MQD",
                          fights=[{"id": 1, "name": "V", "encounterID": 9999, "difficulty": 5,
                                   "kill": False, "startTime": NOW_MS, "endTime": NOW_MS + 1,
                                   "friendlyPlayers": [1]}],
                          players=[{"id": 1, "name": "Mage"}])]

    monkeypatch.setattr(service, "fetch_report_list", fake_list)
    monkeypatch.setattr(service, "fetch_reports", fake_reports)
    asyncio.run(service.sync_logs())
    assert 0 not in service._cache   # cache dropped after new data synced
