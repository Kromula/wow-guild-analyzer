"""Orchestration: read the local store -> assemble -> run checks, with a small
TTL cache.

The overall analysis is served from the on-disk store (populated by `sync_logs`,
the manual "Update Logs" action), so normal page loads make zero WarcraftLogs
calls. Before the first sync the store is empty, so we fall back to a live fetch
once. The cache is keyed by timeframe so flipping between 7/14/30 days is instant.
"""
from __future__ import annotations

import asyncio
import time

from app import store
from app.checks import list_checks, run_all
from app.config import settings
from app.ingest import (Timeframe, assemble, build_dataset, dedupe_frames, fetch_dataset,
                        fetch_report_list, fetch_reports, normalize_report)
from app.ingest.boss import (analyze_boss, boss_summary_from_frames, bosses_from_frames,
                             discover_bosses, fetch_encounter_frames)
from app.wcl import WCLError

_DIFFICULTY_NAMES = {0: "All", 1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}

_CACHE_TTL_S = 600  # 10 minutes
_cache: dict[int, tuple[float, object]] = {}
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(days: int) -> asyncio.Lock:
    return _locks.setdefault(days, asyncio.Lock())


# Sentinel `days` value for the "Last" filter — the most recent raid night,
# rather than a rolling day count. Bounded window, resolved from the store.
LAST_RAID = -1


def _latest_raid_window() -> tuple[int, int] | None:
    """[start_ms, end_ms] spanning the most recent raid night, or None if the store
    is empty. The night is the newest raid-night report plus every other report
    overlapping its time window (co-loggers of the same evening), so multi-logger
    nights aren't truncated — dedupe in `assemble` collapses the duplicates."""
    frames = store.load_reports(store.stored_codes())
    raids = [f for f in frames if f.is_raid_night] or frames
    if not raids:
        return None
    newest = max(raids, key=lambda f: f.start_time)
    night = [f for f in frames
             if f.start_time <= newest.end_time and f.end_time >= newest.start_time]
    return min(f.start_time for f in night), max(f.end_time for f in night)


def _timeframe(days: int) -> Timeframe:
    """A rolling window, all-time (days <= 0), or the latest raid night (LAST_RAID)."""
    if days == LAST_RAID:
        win = _latest_raid_window()
        if win:
            return Timeframe(days=LAST_RAID, start_ms=win[0], end_ms=win[1])
        return Timeframe.all_time()  # empty store: fall back so a live fetch still works
    return Timeframe.all_time() if days <= 0 else Timeframe.last_n_days(days)


def _frames_in_window(tf: Timeframe):
    """Stored per-report frames whose report falls in the timeframe window."""
    frames = store.load_reports(store.stored_codes())
    if tf.is_all_time:
        return frames
    return [f for f in frames if tf.start_ms <= f.start_time <= tf.end_ms]


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
        if store.stored_codes():
            # Served entirely from the local store — no WarcraftLogs calls.
            ds = assemble(_frames_in_window(tf), tf)
        else:
            # Store empty (no sync yet) — fall back to a one-off live fetch so the
            # app still works before the first "Update Logs".
            ds = build_dataset(await fetch_dataset(tf), tf)
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
    """True if a current-tier report isn't cached, has grown, or predates the
    current data revision.

    A report logged mid-raid keeps accumulating pulls; its `endTime` grows on
    later listings, so endTime > stored end_time means there's new data to pull.
    A stale `data_revision` (e.g. stored before boss-panel consumables existed)
    triggers a re-fetch too — but the report stays readable in the meantime, so
    the app keeps serving it rather than going dark like a schema bump would."""
    if force:
        return True
    stored = store.report_meta(live_meta["code"])
    if stored is None:
        return True
    if stored.get("data_revision", 0) < store.DATA_REVISION:
        return True
    live_end, stored_end = live_meta.get("endTime"), stored.get("end_time")
    return bool(live_end and stored_end and live_end > stored_end)


def _chunked(items: list, size: int):
    """Yield successive `size`-length slices of `items` (size>=1)."""
    step = max(1, size)
    for i in range(0, len(items), step):
        yield items[i:i + step]


async def _store_batch(raws: list, now: float) -> None:
    """Persist one batch, each report atomic: fetch its per-encounter (boss-panel)
    frames first, then store the aggregate + encounters together. So a stored
    report ALWAYS has its boss frames — a rate-limit raises here and leaves the
    batch unstored for the next sync to retry, rather than persisting aggregate-only
    data that `_needs_fetch` would treat as complete and never backfill the panels
    for. The caller turns that raise into a graceful `stopped_early`."""
    enc_map = await fetch_encounter_frames(raws) if (raws and settings.cache_boss_panels) else {}
    for raw in raws:
        store.store_report(normalize_report(raw), fetched_at=now,
                           encounters=enc_map.get(raw.code, {}))


async def _run_sync(to_fetch: list[dict], listed: int, scope: str) -> dict:
    """Fetch+store `to_fetch` in resumable batches. Each batch is persisted before
    the next, so a rate-limit mid-run keeps everything already fetched and the next
    run picks up where this stopped (stored reports are skipped by `_needs_fetch`).
    Caller holds `_sync_lock`."""
    now = time.time()
    fetched_total = 0
    stopped_early = False
    for batch in _chunked(to_fetch, settings.sync_batch_size):
        try:
            raws = await fetch_reports(batch)
            await _store_batch(raws, now)
        except WCLError:
            # Rate-limited (or transient WCL failure) after retries. Stop
            # gracefully — earlier batches stay stored; the next run resumes.
            # This batch is left unstored (atomic), so it's retried in full.
            stopped_early = True
            break
        fetched_total += len(raws)
    if fetched_total:
        _invalidate_caches()  # new data — drop cached datasets/boss panels
    return {
        "fetched": fetched_total,
        "skipped": listed - len(to_fetch),
        "remaining": len(to_fetch) - fetched_total,
        "stopped_early": stopped_early,
        "scope": scope,
        "current_tier_reports": listed,
        "stored_total": len(store.stored_codes()),
        "last_synced": last_synced(),
    }


def _latest_night_metas(metas: list[dict]) -> list[dict]:
    """From a current-tier report list, just the most recent raid night: the newest
    report plus any others overlapping its window (co-loggers of the same evening)."""
    if not metas:
        return []
    newest = max(metas, key=lambda m: m.get("startTime") or 0)
    ns, ne = newest.get("startTime") or 0, newest.get("endTime") or 0
    return [m for m in metas
            if (m.get("startTime") or 0) <= ne and (m.get("endTime") or 0) >= ns]


async def sync_logs(*, force: bool = False) -> dict:
    """Pull current-tier logs into the local store, fetching only new/grown (or
    revision-stale) reports. Cheap list step first, then heavy detail/table fetches
    for just the reports that need it. Serialized by a lock so two clicks don't
    double-fetch. Batched + resumable (see `_run_sync`)."""
    async with _sync_lock:
        metas = await fetch_report_list(Timeframe.all_time())
        to_fetch = [m for m in metas if _needs_fetch(m, force=force)]
        return await _run_sync(to_fetch, len(metas), scope="all")


async def sync_latest(*, force: bool = False) -> dict:
    """Import just the most recent raid night's reports (new/grown) — a cheap,
    rate-limit-friendly 'grab tonight's logs' for the Last view that skips the
    full-tier backfill of older reports."""
    async with _sync_lock:
        metas = await fetch_report_list(Timeframe.all_time())
        night = _latest_night_metas(metas)
        to_fetch = [m for m in night if _needs_fetch(m, force=force)]
        return await _run_sync(to_fetch, len(night), scope="latest")


def _invalidate_caches() -> None:
    _cache.clear()
    _boss_list_cache.clear()
    _boss_cache.clear()


def last_synced() -> float | None:
    """Most recent fetch time across stored reports (persists across restarts)."""
    times = [m.get("fetched_at") for m in store.all_meta() if m.get("fetched_at")]
    return max(times) if times else None


def sync_status() -> dict:
    """Store summary for the UI (last sync time + how many reports are cached)."""
    return {"last_synced": last_synced(), "stored_reports": len(store.stored_codes())}


# ── boss drill-down (separate, on-demand fetches) ─────────────────────────────
_boss_list_cache: dict[int, tuple[float, list]] = {}
_boss_cache: dict[tuple[int, int], tuple[float, dict]] = {}


async def list_bosses(days: int, *, force: bool = False) -> dict:
    cached = _boss_list_cache.get(days)
    if cached and not force and time.time() - cached[0] < _CACHE_TTL_S:
        raids = cached[1]
    elif store.stored_codes():
        # Built from the store (no WCL) — the boss dropdown loads instantly.
        raids = bosses_from_frames(_frames_in_window(_timeframe(days)))
        _boss_list_cache[days] = (time.time(), raids)
    else:
        raids = await discover_bosses(_timeframe(days))  # empty store: one-off live fallback
        _boss_list_cache[days] = (time.time(), raids)
    return {"timeframe_days": days, "raids": raids}


async def boss_panel(days: int, encounter_id: int, *, force: bool = False) -> dict:
    key = (days, encounter_id)
    cached = _boss_cache.get(key)
    if cached and not force and time.time() - cached[0] < _CACHE_TTL_S:
        return cached[1]
    tf = _timeframe(days)
    if store.encounter_is_cached(encounter_id):
        # Served from stored per-encounter frames — no WCL calls.
        frames = dedupe_frames([f for f in store.load_encounter_frames(encounter_id)
                                if tf.is_all_time or tf.start_ms <= f.start_time <= tf.end_ms])
        if frames:
            ds = assemble(frames, tf)
            panel = {"boss": boss_summary_from_frames(frames, encounter_id),
                     "checks": [r.to_dict() for r in run_all(ds, boss_view=True)]}
        else:
            panel = {"error": "No pulls of that boss in the selected window."}
    else:
        # Not cached yet (e.g. boss-panel caching off, or pre-cache sync) — live.
        panel = await analyze_boss(tf, encounter_id)
    panel["timeframe_days"] = days
    _boss_cache[key] = (time.time(), panel)
    return panel
