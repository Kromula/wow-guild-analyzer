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
    # These tests target the incremental-diff logic; the per-encounter boss-frame
    # fetch is covered separately (test_sync_caches_boss_frames).
    monkeypatch.setattr(settings, "cache_boss_panels", False)
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
    calls_before = len(fetched)
    summary = asyncio.run(service.sync_logs())  # nothing changed
    assert summary["fetched"] == 0
    assert summary["skipped"] == 2
    assert len(fetched) == calls_before       # second run made no fetch call


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


def _meta_at(code, start, hours=3):
    return {"code": code, "title": code, "startTime": start,
            "endTime": start + hours * 3_600_000, "zone": {"id": 46, "name": "VS / DR / MQD"}}


def test_sync_latest_only_fetches_the_newest_night(monkeypatch):
    DAY = 86_400_000
    metas = [_meta_at("old", START), _meta_at("midweek", START + 3 * DAY),
             _meta_at("tonight", START + 7 * DAY)]
    fetched = _wire(monkeypatch, metas)
    summary = asyncio.run(service.sync_latest())
    assert summary["scope"] == "latest"
    assert summary["fetched"] == 1
    assert fetched[-1] == ["tonight"]              # older nights untouched
    assert store.stored_codes() == {"tonight"}


def test_sync_latest_includes_same_night_co_logger(monkeypatch):
    DAY = 86_400_000
    # main + co-logger overlap tonight; a previous night must be excluded.
    metas = [_meta_at("prev", START),
             _meta_at("main", START + 7 * DAY),
             _meta_at("colog", START + 7 * DAY + 3_600_000)]  # starts 1h into main's window
    fetched = _wire(monkeypatch, metas)
    summary = asyncio.run(service.sync_latest())
    assert summary["fetched"] == 2
    assert set(fetched[-1]) == {"main", "colog"}
    assert "prev" not in store.stored_codes()


def test_backfill_runs_in_batches(monkeypatch):
    """A large to_fetch list is pulled in bounded batches, all persisted."""
    monkeypatch.setattr(settings, "sync_batch_size", 2)
    fetched = _wire(monkeypatch, [_meta(c) for c in "abcde"])
    summary = asyncio.run(service.sync_logs())
    assert summary["fetched"] == 5
    assert [len(call) for call in fetched] == [2, 2, 1]   # batched 2/2/1
    assert store.stored_codes() == set("abcde")


def test_rate_limit_mid_backfill_keeps_progress(monkeypatch):
    """If WCL rate-limits on a later batch, earlier batches stay stored and the
    run reports it stopped early with work remaining."""
    from app.wcl import WCLError
    monkeypatch.setattr(settings, "sync_batch_size", 2)

    async def fake_list(tf):
        return [_meta(c) for c in "abcde"]

    calls: list[list[str]] = []

    async def flaky_reports(to_fetch):
        calls.append([m["code"] for m in to_fetch])
        if len(calls) == 2:           # second batch trips the limit
            raise WCLError("Rate limited by WarcraftLogs (429); backing off.")
        return [_raw_for(m) for m in to_fetch]

    monkeypatch.setattr(service, "fetch_report_list", fake_list)
    monkeypatch.setattr(service, "fetch_reports", flaky_reports)

    summary = asyncio.run(service.sync_logs())
    assert summary["fetched"] == 2          # only the first batch landed
    assert summary["stopped_early"] is True
    assert summary["remaining"] == 3
    assert store.stored_codes() == {"a", "b"}

    # Resuming skips what's stored and fetches the rest.
    _wire(monkeypatch, [_meta(c) for c in "abcde"])
    resume = asyncio.run(service.sync_logs())
    assert resume["fetched"] == 3
    assert resume["stopped_early"] is False
    assert store.stored_codes() == set("abcde")


def test_sync_caches_boss_frames(monkeypatch):
    """With boss caching on, sync fetches per-encounter frames and attaches them."""
    from app import store
    from app.ingest.normalize import normalize_report
    monkeypatch.setattr(settings, "cache_boss_panels", True)
    _wire(monkeypatch, [_meta("a")])

    async def fake_enc(raws):
        # One encounter (9999) cached for report "a".
        return {r.code: {9999: normalize_report(_raw_for(_meta(r.code)))} for r in raws}

    monkeypatch.setattr(service, "fetch_encounter_frames", fake_enc)
    asyncio.run(service.sync_logs())
    assert store.encounter_is_cached(9999) is True
    assert store.load_encounter("a", 9999) is not None
