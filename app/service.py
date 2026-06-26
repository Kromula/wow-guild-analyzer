"""Orchestration: fetch -> normalize -> run checks, with a small TTL cache.

The cache is keyed by timeframe so flipping between 7/14/30 days is instant after
the first pull, while a manual refresh (or TTL expiry) re-queries WarcraftLogs.
"""
from __future__ import annotations

import asyncio
import time

from app import store
from app.checks import list_checks, run_all
from app.config import settings
from app.ingest import (Timeframe, build_dataset, fetch_dataset, fetch_report_list,
                        fetch_reports, normalize_report)
from app.ingest.boss import analyze_boss, discover_bosses

_DIFFICULTY_NAMES = {0: "All", 1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}

_CACHE_TTL_S = 600  # 10 minutes
_cache: dict[int, tuple[float, object]] = {}
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(days: int) -> asyncio.Lock:
    return _locks.setdefault(days, asyncio.Lock())


def _timeframe(days: int) -> Timeframe:
    """A rolling window, or all-time when days is 0."""
    return Timeframe.all_time() if days <= 0 else Timeframe.last_n_days(days)


async def get_dataset(days: int, *, force: bool = False):
    now = time.time()
    cached = _cache.get(days)
    if cached and not force and now - cached[0] < _CACHE_TTL_S:
        return cached[1]

    async with _lock_for(days):
        cached = _cache.get(days)  # re-check after acquiring lock
        if cached and not force and time.time() - cached[0] < _CACHE_TTL_S:
            return cached[1]
        tf = _timeframe(days)
        raws = await fetch_dataset(tf)
        ds = build_dataset(raws, tf)
        _cache[days] = (time.time(), ds)
        return ds


async def analyze(days: int, *, only: list[str] | None = None, force: bool = False) -> dict:
    ds = await get_dataset(days, force=force)
    results = run_all(ds, only=only)
    return {
        "timeframe_days": days,
        "reports": ds.reports,
        "report_count": len(ds.reports),
        "fight_count": ds.fights.height,
        "player_count": ds.players.height,
        "filters": {
            "difficulty": _DIFFICULTY_NAMES.get(settings.raid_difficulty, str(settings.raid_difficulty)),
            "min_attendance_pct": settings.min_attendance_pct,
            "mythic_plus_excluded": settings.exclude_mythic_plus,
        },
        "checks": [r.to_dict() for r in results],
    }


def available_checks() -> list[dict]:
    return list_checks()


# ── log sync (manual "Update Logs") ───────────────────────────────────────────
_sync_lock = asyncio.Lock()


def _needs_fetch(live_meta: dict, *, force: bool) -> bool:
    """True if a current-tier report isn't cached, or grew since we cached it.

    A report logged mid-raid keeps accumulating pulls; its `endTime` grows on
    later listings, so endTime > stored end_time means there's new data to pull."""
    if force:
        return True
    stored = store.report_meta(live_meta["code"])
    if stored is None:
        return True
    live_end, stored_end = live_meta.get("endTime"), stored.get("end_time")
    return bool(live_end and stored_end and live_end > stored_end)


async def sync_logs(*, force: bool = False) -> dict:
    """Pull current-tier logs into the local store, fetching only new/grown reports.

    Cheap list step first, then heavy detail/table fetches for just the reports
    that are missing or have grown. Serialized by a lock so two clicks don't
    double-fetch."""
    async with _sync_lock:
        metas = await fetch_report_list(Timeframe.all_time())
        to_fetch = [m for m in metas if _needs_fetch(m, force=force)]
        raws = await fetch_reports(to_fetch)
        now = time.time()
        for raw in raws:
            store.store_report(normalize_report(raw), fetched_at=now)
        return {
            "fetched": len(raws),
            "skipped": len(metas) - len(to_fetch),
            "current_tier_reports": len(metas),
            "stored_total": len(store.stored_codes()),
            "last_synced": last_synced(),
        }


def last_synced() -> float | None:
    """Most recent fetch time across stored reports (persists across restarts)."""
    times = [m.get("fetched_at") for m in store.all_meta() if m.get("fetched_at")]
    return max(times) if times else None


# ── boss drill-down (separate, on-demand fetches) ─────────────────────────────
_boss_list_cache: dict[int, tuple[float, list]] = {}
_boss_cache: dict[tuple[int, int], tuple[float, dict]] = {}


async def list_bosses(days: int, *, force: bool = False) -> dict:
    cached = _boss_list_cache.get(days)
    if cached and not force and time.time() - cached[0] < _CACHE_TTL_S:
        raids = cached[1]
    else:
        raids = await discover_bosses(_timeframe(days))
        _boss_list_cache[days] = (time.time(), raids)
    return {"timeframe_days": days, "raids": raids}


async def boss_panel(days: int, encounter_id: int, *, force: bool = False) -> dict:
    key = (days, encounter_id)
    cached = _boss_cache.get(key)
    if cached and not force and time.time() - cached[0] < _CACHE_TTL_S:
        return cached[1]
    panel = await analyze_boss(_timeframe(days), encounter_id)
    panel["timeframe_days"] = days
    _boss_cache[key] = (time.time(), panel)
    return panel
