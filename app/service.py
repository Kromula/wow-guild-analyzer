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


def _timeframe(days: int) -> Timeframe:
    """A rolling window, or all-time when days is 0."""
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


def _chunked(items: list, size: int):
    """Yield successive `size`-length slices of `items` (size>=1)."""
    step = max(1, size)
    for i in range(0, len(items), step):
        yield items[i:i + step]


async def _store_batch(raws: list, now: float) -> None:
    """Persist one batch: aggregate frames first, then best-effort boss frames."""
    for raw in raws:
        store.store_report(normalize_report(raw), fetched_at=now)
    # Per-encounter (boss-panel) frames — heavier, best-effort: the aggregate is
    # already stored, so a rate-limit here just defers boss caching to the next
    # sync rather than losing this batch's work.
    if raws and settings.cache_boss_panels:
        try:
            enc_map = await fetch_encounter_frames(raws)
        except WCLError:
            enc_map = {}
        for code, encounters in enc_map.items():
            store.attach_encounters(code, encounters)


async def sync_logs(*, force: bool = False) -> dict:
    """Pull current-tier logs into the local store, fetching only new/grown reports.

    Cheap list step first, then heavy detail/table fetches for just the reports
    that are missing or have grown. Serialized by a lock so two clicks don't
    double-fetch.

    The heavy fetch runs in batches (`sync_batch_size`), each persisted before the
    next, so a full-season backfill is resumable: if WCL rate-limits mid-run, every
    batch already stored is kept and the next "Update Logs" picks up where this one
    stopped (stored reports are skipped by `_needs_fetch`)."""
    async with _sync_lock:
        metas = await fetch_report_list(Timeframe.all_time())
        to_fetch = [m for m in metas if _needs_fetch(m, force=force)]
        now = time.time()
        fetched_total = 0
        stopped_early = False
        for batch in _chunked(to_fetch, settings.sync_batch_size):
            try:
                raws = await fetch_reports(batch)
            except WCLError:
                # Rate-limited (or transient WCL failure) after retries. Stop
                # gracefully — what's stored stays stored; the next sync resumes.
                stopped_early = True
                break
            await _store_batch(raws, now)
            fetched_total += len(raws)
        if fetched_total:
            _invalidate_caches()  # new data — drop cached datasets/boss panels
        return {
            "fetched": fetched_total,
            "skipped": len(metas) - len(to_fetch),
            "remaining": len(to_fetch) - fetched_total,
            "stopped_early": stopped_early,
            "current_tier_reports": len(metas),
            "stored_total": len(store.stored_codes()),
            "last_synced": last_synced(),
        }


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
