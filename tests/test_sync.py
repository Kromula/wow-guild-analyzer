"""Tests for incremental log sync (issue #13 slice 4).

sync_logs lists current-tier reports, fetches only the missing/grown ones, and
persists them to the store. The WCL fetch functions are mocked so the test runs
offline.
"""
from __future__ import annotations

import asyncio

import pytest

from app import service, store
from app.config import settings
from app.ingest.fetcher import RawReport

START = 1_700_000_000_000


def _meta(code, end=START + 3_600_000):
    return {"code": code, "title": code, "startTime": START, "endTime": end,
            "zone": {"id": 46, "name": "VS / DR / MQD"}}


def _raw_for(meta):
    return RawReport(code=meta["code"], title=meta["title"],
                     start_time=meta["startTime"], end_time=meta["endTime"],
                     zone=meta["zone"]["name"],
                     fights=[{"id": 1, "name": "Voidspire", "encounterID": 9999, "difficulty": 5,
                              "kill": False, "startTime": START, "endTime": START + 60_000,
                              "friendlyPlayers": [1]}],
                     players=[{"id": 1, "name": "Mage"}])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield


def _wire(monkeypatch, metas):
    """Point sync at canned data and record which codes get fetched."""
    fetched: list[list[str]] = []

    async def fake_list(tf):
        return list(metas)

    async def fake_reports(to_fetch):
        fetched.append([m["code"] for m in to_fetch])
        return [_raw_for(m) for m in to_fetch]

    monkeypatch.setattr(service, "fetch_report_list", fake_list)
    monkeypatch.setattr(service, "fetch_reports", fake_reports)
    return fetched


def test_first_sync_fetches_everything(monkeypatch):
    _wire(monkeypatch, [_meta("a"), _meta("b")])
    summary = asyncio.run(service.sync_logs())
    assert summary["fetched"] == 2
    assert summary["skipped"] == 0
    assert store.stored_codes() == {"a", "b"}


def test_second_sync_is_noop(monkeypatch):
    metas = [_meta("a"), _meta("b")]
    fetched = _wire(monkeypatch, metas)
    asyncio.run(service.sync_logs())          # populate
    summary = asyncio.run(service.sync_logs())  # nothing changed
    assert summary["fetched"] == 0
    assert summary["skipped"] == 2
    assert fetched[-1] == []                  # second run fetched no codes


def test_grown_report_is_refetched(monkeypatch):
    fetched = _wire(monkeypatch, [_meta("a", end=START + 3_600_000)])
    asyncio.run(service.sync_logs())
    # Same report, but the night ran longer -> endTime grew -> must refetch.
    _wire(monkeypatch, [_meta("a", end=START + 9_999_999)])
    summary = asyncio.run(service.sync_logs())
    assert summary["fetched"] == 1


def test_new_report_added_incrementally(monkeypatch):
    _wire(monkeypatch, [_meta("a")])
    asyncio.run(service.sync_logs())
    fetched = _wire(monkeypatch, [_meta("a"), _meta("b")])  # b is new
    summary = asyncio.run(service.sync_logs())
    assert summary["fetched"] == 1
    assert fetched[-1] == ["b"]               # only the new report
    assert store.stored_codes() == {"a", "b"}


def test_force_refetches_all(monkeypatch):
    _wire(monkeypatch, [_meta("a"), _meta("b")])
    asyncio.run(service.sync_logs())
    summary = asyncio.run(service.sync_logs(force=True))
    assert summary["fetched"] == 2


def test_last_synced_reflects_store(monkeypatch):
    assert service.last_synced() is None
    _wire(monkeypatch, [_meta("a")])
    asyncio.run(service.sync_logs())
    assert service.last_synced() is not None
