"""Fetch and assemble the raw data needed for analysis from WarcraftLogs.

Strategy (to keep API usage reasonable):
  * Damage / Healing / Casts  -> aggregated per report across all encounter fights.
  * Deaths                    -> per fight, so we can compute death ORDER within a fight.

Table fetches run with bounded concurrency to stay polite to the API.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.wcl import WCLClient
from app.wcl.queries import (GUILD_REPORTS, REPORT_EVENTS, REPORT_FIGHTS,
                             REPORT_PLAYER_DETAILS, REPORT_TABLE)

_CONCURRENCY = 5


@dataclass
class Timeframe:
    days: int          # rolling window length; 0 means "all time" (no lower bound)
    start_ms: int
    end_ms: int

    @classmethod
    def last_n_days(cls, days: int) -> "Timeframe":
        now_ms = int(time.time() * 1000)
        return cls(days=days, start_ms=now_ms - days * 86_400_000, end_ms=now_ms)

    @classmethod
    def all_time(cls) -> "Timeframe":
        """Every log the guild has, regardless of date — bounded only by WCL
        pagination, not a rolling window or max_reports."""
        return cls(days=0, start_ms=0, end_ms=int(time.time() * 1000))

    @property
    def is_all_time(self) -> bool:
        return self.days <= 0


@dataclass
class RawReport:
    code: str
    title: str
    start_time: int
    end_time: int
    zone: str
    fights: list[dict[str, Any]] = field(default_factory=list)
    players: list[dict[str, Any]] = field(default_factory=list)
    # dataType -> table JSON (report-level aggregate)
    tables: dict[str, Any] = field(default_factory=dict)
    # fight_id -> deaths table JSON
    deaths_by_fight: dict[int, Any] = field(default_factory=dict)
    # playerDetails JSON (role/spec buckets) for this report's encounter window
    player_details: Any = field(default_factory=dict)
    # fight_id -> list of damage-taken events (boss-specific, e.g. Glaive hits)
    damage_taken_by_fight: dict[int, Any] = field(default_factory=dict)


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _death_ts(entry: dict) -> float:
    """Absolute timestamp (ms) of a death-table entry, tolerating field naming."""
    return _to_float(entry.get("deathTime", entry.get("timestamp", entry.get("startTime"))))


def _fight_id_for_ts(ts: float, bounds: dict[int, tuple[float, float]]) -> int | None:
    """Which fight's [start, end] window contains this absolute timestamp."""
    for fid, (start, end) in bounds.items():
        if start <= ts <= end:
            return fid
    return None


def _bucket_events(items: list[dict], bounds: dict[int, tuple[float, float]], ts_of) -> dict[int, list]:
    """Split a flat per-report event/death list into per-fight buckets by
    timestamp. Lets us fetch once per report and group client-side, instead of
    one API query per fight. Every fight id gets an entry (possibly empty)."""
    buckets: dict[int, list] = {fid: [] for fid in bounds}
    for it in items:
        fid = _fight_id_for_ts(ts_of(it), bounds)
        if fid is not None:
            buckets[fid].append(it)
    return buckets


def _is_mythic_plus(report_meta: dict[str, Any]) -> bool:
    zone_name = ((report_meta.get("zone") or {}).get("name") or "").lower()
    return any(p in zone_name for p in settings.mythic_plus_zone_patterns)


def _zone_id(meta: dict[str, Any]) -> Any:
    return (meta.get("zone") or {}).get("id")


async def _list_reports(client: WCLClient, tf: Timeframe) -> list[dict[str, Any]]:
    # "All time" uses a higher cap (newest-first, so it covers the current tier);
    # a bounded window uses the normal cap. Both stay capped so a wide window
    # doesn't crawl the guild's entire history and exhaust WCL rate limits.
    cap = settings.max_reports_all_time if tf.is_all_time else settings.max_reports
    # Current-tier scope: lock onto a zone id (explicit, or auto-detected from the
    # newest qualifying report) and stop once we page past it. Reports are
    # newest-first, so the current tier is the leading contiguous block — once the
    # zone changes going back in time we're into an older tier and can stop,
    # avoiding the expensive per-report detail fetches for off-tier reports.
    target_zone = settings.current_tier_zone_id or None
    reports: list[dict[str, Any]] = []
    page = 1
    done = False
    while len(reports) < cap and not done:
        data = await client.query(
            GUILD_REPORTS,
            {
                "guildName": settings.guild_name,
                "serverSlug": settings.guild_server_slug,
                "serverRegion": settings.guild_region,
                "startTime": float(tf.start_ms),
                "endTime": float(tf.end_ms),
                "page": page,
                "limit": 25,
            },
        )
        block = data["reportData"]["reports"]
        for meta in block["data"]:
            if settings.exclude_mythic_plus and _is_mythic_plus(meta):
                continue  # raid-only: skip Mythic+ reports entirely (also saves API calls)
            if settings.current_tier_only:
                zid = _zone_id(meta)
                if target_zone is None:
                    target_zone = zid  # newest qualifying report defines the current tier
                if zid != target_zone:
                    done = True  # newest-first, so a zone change means we've left the current tier
                    break
            reports.append(meta)
            if len(reports) >= cap:
                break
        if not block["has_more_pages"]:
            break
        page += 1
    return reports[:cap]


async def _load_report_detail(client: WCLClient, report_meta: dict[str, Any]) -> RawReport:
    code = report_meta["code"]
    data = await client.query(REPORT_FIGHTS, {"code": code})
    report = data["reportData"]["report"]
    fights = report.get("fights") or []
    # Keep only encounter fights at the configured difficulty (e.g. Mythic=5).
    # Filtering here means the downstream damage/healing/casts aggregates and the
    # attendance calc all see exactly the same set of pulls.
    fights = [
        f for f in fights
        if f.get("encounterID") and (not settings.raid_difficulty
                                     or f.get("difficulty") == settings.raid_difficulty)
    ]
    players = (report.get("masterData") or {}).get("actors") or []

    raw = RawReport(
        code=code,
        title=report.get("title") or report_meta.get("title") or code,
        start_time=report["startTime"],
        end_time=report["endTime"],
        zone=(report_meta.get("zone") or {}).get("name", "Unknown"),
        fights=fights,
        players=players,
    )
    return raw


async def _fetch_table(client: WCLClient, code: str, data_type: str,
                       start: float, end: float, fight_ids: list[int] | None) -> Any:
    data = await client.query(
        REPORT_TABLE,
        {"code": code, "dataType": data_type, "startTime": start, "endTime": end, "fightIDs": fight_ids},
    )
    return data["reportData"]["report"]["table"]


async def _fetch_player_details(client: WCLClient, code: str,
                                start: float, end: float, fight_ids: list[int] | None) -> Any:
    data = await client.query(
        REPORT_PLAYER_DETAILS,
        {"code": code, "startTime": start, "endTime": end, "fightIDs": fight_ids},
    )
    return data["reportData"]["report"]["playerDetails"]


async def _fetch_events(client: WCLClient, code: str, data_type: str, start: float, end: float,
                        fight_ids: list[int] | None, ability_id: int | None = None,
                        hostility: str = "Friendlies") -> list[dict]:
    """All events of a type for the window, following `nextPageTimestamp` to the
    end. Optionally filtered to a single ability (e.g. the Glaive). Returns the
    flat event list."""
    out: list[dict] = []
    page_start = start
    while True:
        data = await client.query(REPORT_EVENTS, {
            "code": code, "startTime": page_start, "endTime": end, "fightIDs": fight_ids,
            "dataType": data_type,
            "abilityID": float(ability_id) if ability_id else None,
            "hostility": hostility,
        })
        block = data["reportData"]["report"]["events"] or {}
        out.extend(block.get("data") or [])
        nxt = block.get("nextPageTimestamp")
        if not nxt or float(nxt) <= page_start:
            break
        page_start = float(nxt)
    return out


async def fetch_dataset(tf: Timeframe) -> list[RawReport]:
    """Return fully-populated RawReports for the timeframe."""
    if not settings.configured:
        raise RuntimeError("App not configured. Fill in .env (see .env.example / README.md).")

    client = WCLClient()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def guarded(coro):
        async with sem:
            return await coro

    try:
        report_metas = await _list_reports(client, tf)
        raws = await asyncio.gather(*(guarded(_load_report_detail(client, m)) for m in report_metas))

        table_jobs = []
        for raw in raws:
            encounter_fights = [f for f in raw.fights if f.get("encounterID")]
            if not encounter_fights:
                continue
            fight_ids = [f["id"] for f in encounter_fights]
            span_start = float(min(f["startTime"] for f in encounter_fights))
            span_end = float(max(f["endTime"] for f in encounter_fights))

            for data_type in ("DamageDone", "Healing", "Casts"):
                table_jobs.append(_assign_table(guarded, client, raw, data_type, span_start, span_end, fight_ids))

            table_jobs.append(_assign_player_details(guarded, client, raw, span_start, span_end, fight_ids))

            fight_bounds = {f["id"]: (float(f["startTime"]), float(f["endTime"])) for f in encounter_fights}
            table_jobs.append(_assign_deaths(guarded, client, raw, fight_bounds))

        await asyncio.gather(*table_jobs)
        return raws
    finally:
        await client.aclose()


async def _assign_table(guarded, client, raw: RawReport, data_type: str,
                        start: float, end: float, fight_ids: list[int]) -> None:
    raw.tables[data_type] = await guarded(_fetch_table(client, raw.code, data_type, start, end, fight_ids))


async def _assign_player_details(guarded, client, raw: RawReport,
                                 start: float, end: float, fight_ids: list[int]) -> None:
    raw.player_details = await guarded(_fetch_player_details(client, raw.code, start, end, fight_ids))


async def _assign_deaths(guarded, client, raw: RawReport,
                         fight_bounds: dict[int, tuple[float, float]]) -> None:
    """One Deaths fetch for the whole report, split into per-fight buckets by
    death timestamp — replaces the previous one-query-per-fight fan-out. Each
    bucket is wrapped to match the table shape the normalizer already parses."""
    if not fight_bounds:
        return
    from app.ingest.normalize import _entries  # local import avoids an import cycle
    ids = list(fight_bounds)
    span_s = min(s for s, _ in fight_bounds.values())
    span_e = max(e for _, e in fight_bounds.values())
    payload = await guarded(_fetch_table(client, raw.code, "Deaths", span_s, span_e, ids))
    buckets = _bucket_events(_entries(payload), fight_bounds, _death_ts)
    for fid, entries in buckets.items():
        raw.deaths_by_fight[fid] = {"data": {"entries": entries}}
