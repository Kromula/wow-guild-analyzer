"""Boss drill-down: discover raids/bosses in a window, and build a focused
per-boss analysis scoped to a single encounter across the timeframe.

The per-boss view runs the *same* check framework as the overall page, just over
an AnalysisDataset scoped to one encounter — so the cards (and their metrics) are
identical, only the data is narrower. On-demand fetching (only when the user
drills in) is what keeps per-boss data affordable.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

from app.checks import run_all
from app.config import settings
from app.ingest.fetcher import (RawReport, Timeframe, _assign_deaths, _bucket_events, _fetch_events,
                                _fetch_player_details, _fetch_table, _list_reports, _load_report_detail,
                                _to_float)
from app.ingest.normalize import build_dataset
from app.wcl import WCLClient

_CONCURRENCY = 5


async def _load_raid_reports(client: WCLClient, tf: Timeframe) -> list[RawReport]:
    """Reports (raid-only, difficulty-filtered) with fights + roster, no tables."""
    metas = await _list_reports(client, tf)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(m):
        async with sem:
            return await _load_report_detail(client, m)

    raws = await asyncio.gather(*(one(m) for m in metas))
    return [r for r in raws if r.fights]  # only reports with target-difficulty fights


async def discover_bosses(tf: Timeframe) -> list[dict]:
    """Raids (zones) and their bosses seen in the window, for the UI dropdowns."""
    client = WCLClient()
    try:
        raws = await _load_raid_reports(client, tf)
    finally:
        await client.aclose()

    # A boss is uniquely identified by encounterID. Report `zone.name` is
    # unreliable for grouping (the same boss shows up under different report-level
    # zone labels), so dedupe by encounterID and assign each boss to the zone it
    # most often appears under.
    bosses: dict[int, dict] = {}
    zone_votes: dict[int, Counter] = defaultdict(Counter)
    for raw in raws:
        for f in raw.fights:
            enc = f.get("encounterID")
            if not enc:
                continue
            slot = bosses.setdefault(enc, {"encounter_id": enc, "name": f.get("name", "?"), "pulls": 0})
            slot["pulls"] += 1
            zone_votes[enc][raw.zone] += 1

    zones: dict[str, list] = defaultdict(list)
    for enc, boss in bosses.items():
        zone = zone_votes[enc].most_common(1)[0][0]
        zones[zone].append(boss)

    out = [{"zone": zone, "bosses": sorted(bs, key=lambda b: -b["pulls"])}
           for zone, bs in zones.items()]
    return sorted(out, key=lambda z: z["zone"])


async def _populate_boss_tables(guarded, client: WCLClient, raw: RawReport, encounter_id: int) -> None:
    """Fetch this encounter's Damage/Healing/Casts (aggregated over its pulls) and
    per-fight Deaths into `raw`, so build_dataset can normalize them as usual."""
    bf = [f for f in raw.fights if f.get("encounterID") == encounter_id]
    ids = [f["id"] for f in bf]
    span_s = float(min(f["startTime"] for f in bf))
    span_e = float(max(f["endTime"] for f in bf))

    dmg, heal, casts, pdetails = await asyncio.gather(
        guarded(_fetch_table(client, raw.code, "DamageDone", span_s, span_e, ids)),
        guarded(_fetch_table(client, raw.code, "Healing", span_s, span_e, ids)),
        guarded(_fetch_table(client, raw.code, "Casts", span_s, span_e, ids)),
        guarded(_fetch_player_details(client, raw.code, span_s, span_e, ids)),
    )
    raw.tables["DamageDone"], raw.tables["Healing"], raw.tables["Casts"] = dmg, heal, casts
    raw.player_details = pdetails

    bounds = {f["id"]: (float(f["startTime"]), float(f["endTime"])) for f in bf}
    await _assign_deaths(guarded, client, raw, bounds)

    # Boss-specific avoidable-damage events: Glaive hits on Midnight Falls. One
    # fetch for all this boss's pulls, split per fight by timestamp so each hit can
    # be trimmed to its pull's live portion downstream. Only this encounter, and
    # only if configured.
    if encounter_id == settings.midnight_falls_encounter_id and settings.glaive_ability_id:
        evs = await guarded(_fetch_events(client, raw.code, "DamageTaken", span_s, span_e, ids,
                                          ability_id=settings.glaive_ability_id))
        raw.damage_taken_by_fight = _bucket_events(evs, bounds, lambda e: _to_float(e.get("timestamp")))


def _boss_summary(relevant: list[RawReport], encounter_id: int) -> dict:
    fights = [f for r in relevant for f in r.fights if f.get("encounterID") == encounter_id]
    name = next((f.get("name", "?") for f in fights), "?")
    kills = sum(1 for f in fights if f.get("kill"))
    kill_durs = [(f["endTime"] - f["startTime"]) / 1000.0 for f in fights if f.get("kill")]
    wipe_pcts = [f["fightPercentage"] for f in fights
                 if not f.get("kill") and f.get("fightPercentage") is not None]
    return {
        "name": name, "zone": relevant[0].zone, "encounter_id": encounter_id,
        "pulls": len(fights), "kills": kills, "wipes": len(fights) - kills,
        "best_kill_s": min(kill_durs) if kill_durs else None,
        "best_wipe_pct": min(wipe_pcts) if wipe_pcts else None,
    }


async def analyze_boss(tf: Timeframe, encounter_id: int) -> dict:
    client = WCLClient()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def guarded(coro):
        async with sem:
            return await coro

    try:
        raws = await _load_raid_reports(client, tf)
        relevant = [r for r in raws if any(f.get("encounterID") == encounter_id for f in r.fights)]
        if not relevant:
            return {"error": "No pulls of that boss in the selected window."}

        summary = _boss_summary(relevant, encounter_id)
        await asyncio.gather(*(_populate_boss_tables(guarded, client, r, encounter_id) for r in relevant))

        # Scope each report to this encounter's fights so build_dataset's fights
        # frame and (per-night) core-raider filter both narrow to this boss.
        for r in relevant:
            r.fights = [f for f in r.fights if f.get("encounterID") == encounter_id]

        ds = build_dataset(relevant, tf)
        results = run_all(ds)
        return {"boss": summary, "checks": [r.to_dict() for r in results]}
    finally:
        await client.aclose()
