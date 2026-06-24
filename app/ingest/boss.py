"""Boss drill-down: discover raids/bosses in a window, and build a focused
per-boss stat panel scoped to a single encounter across the timeframe.

This is intentionally separate from the overall pipeline: it fetches per-boss
data on demand (only when the user drills in), which is what makes the accurate
per-player defensive/consumable tracking affordable.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path

from app.config import settings
from app.ingest.fetcher import (RawReport, Timeframe, _fetch_table, _list_reports,
                                _load_report_detail)
from app.wcl import WCLClient
from app.wcl.queries import REPORT_BUFF_EVENTS, REPORT_BUFFS_TABLE

_CONCURRENCY = 5

_ABIL = json.loads((Path(__file__).resolve().parents[1]
                    / "checks/builtin/survival_abilities.json").read_text(encoding="utf-8"))
_CONSUMABLES = [s.lower() for s in _ABIL.get("consumables", [])]
_DEFENSIVES = [s.lower() for s in _ABIL.get("personal_defensives", [])]


def _classify(name: str) -> str | None:
    low = (name or "").lower()
    if any(p in low for p in _CONSUMABLES):
        return "consumable"
    if any(p in low for p in _DEFENSIVES):
        return "defensive"
    return None


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


def _core_for_boss(raws: list[RawReport], encounter_id: int) -> tuple[set[str], int]:
    """Players present for >= min_attendance_pct of this boss's pulls."""
    attended: Counter[str] = Counter()
    total_pulls = 0
    for raw in raws:
        id2name = {p["id"]: p["name"] for p in raw.players}
        for f in raw.fights:
            if f.get("encounterID") != encounter_id:
                continue
            total_pulls += 1
            for pid in (f.get("friendlyPlayers") or []):
                if pid in id2name:
                    attended[id2name[pid]] += 1
    if settings.min_attendance_pct <= 0 or total_pulls == 0:
        return set(attended), total_pulls
    threshold = settings.min_attendance_pct * total_pulls
    return {n for n, c in attended.items() if c >= threshold}, total_pulls


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
            return {"error": "No Mythic pulls of that boss in the selected window."}

        core, total_pulls = _core_for_boss(raws, encounter_id)
        boss_name = next((f["name"] for r in relevant for f in r.fights
                          if f.get("encounterID") == encounter_id), "?")
        zone = relevant[0].zone

        # Per-report fetch jobs.
        dmg: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "active_s": 0.0, "player_class": "Unknown"})
        heal: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "active_s": 0.0})
        deaths: dict[str, dict] = defaultdict(lambda: {"deaths": 0, "time_sum": 0.0, "first": 0, "killers": Counter()})
        survival: dict[str, dict] = defaultdict(lambda: {"consumable": 0, "defensive": 0})
        kills = wipes = 0
        best_kill_s: float | None = None
        best_wipe_pct: float | None = None

        async def process(raw: RawReport):
            nonlocal kills, wipes, best_kill_s, best_wipe_pct
            bf = [f for f in raw.fights if f.get("encounterID") == encounter_id]
            if not bf:
                return
            ids = [f["id"] for f in bf]
            span_s = float(min(f["startTime"] for f in bf))
            span_e = float(max(f["endTime"] for f in bf))
            id2name = {p["id"]: p["name"] for p in raw.players}

            for f in bf:
                if f.get("kill"):
                    kills += 1
                    dur = (f["endTime"] - f["startTime"]) / 1000.0
                    best_kill_s = dur if best_kill_s is None else min(best_kill_s, dur)
                else:
                    wipes += 1
                    pct = f.get("fightPercentage")
                    if pct is not None:
                        best_wipe_pct = pct if best_wipe_pct is None else min(best_wipe_pct, pct)

            dmg_tbl, heal_tbl, buffs_tbl = await asyncio.gather(
                guarded(_fetch_table(client, raw.code, "DamageDone", span_s, span_e, ids)),
                guarded(_fetch_table(client, raw.code, "Healing", span_s, span_e, ids)),
                guarded(client.query(REPORT_BUFFS_TABLE,
                        {"code": raw.code, "startTime": span_s, "endTime": span_e, "fightIDs": ids})),
            )
            _accumulate_table(dmg_tbl, dmg, core, with_class=True)
            _accumulate_table(heal_tbl, heal, core, with_class=False)

            # Deaths per fight (relative timing + first-death).
            for f in bf:
                d_tbl = await guarded(_fetch_table(client, raw.code, "Deaths",
                                                   float(f["startTime"]), float(f["endTime"]), [f["id"]]))
                _accumulate_deaths(d_tbl, deaths, core, f["startTime"])

            # Defensive/consumable applications, attributed per player.
            # Discover the *actual* survival buff names present in this report from
            # the aggregate Buffs auras (flexible substring match), then fetch only
            # those exact names server-side. This avoids hard-coding volatile potion
            # names while keeping the event payload tiny.
            auras = (((buffs_tbl["reportData"]["report"]["table"] or {}).get("data") or {}).get("auras") or [])
            guid2name = {a.get("guid"): a.get("name") for a in auras}
            survival_names = sorted({a.get("name") for a in auras if a.get("name") and _classify(a["name"])})
            if not survival_names:
                return
            filt = 'type="applybuff" and ability.name in ({})'.format(
                ",".join(f'"{n}"' for n in survival_names))
            ev = await guarded(client.query(REPORT_BUFF_EVENTS, {
                "code": raw.code, "startTime": span_s, "endTime": span_e,
                "fightIDs": ids, "filter": filt}))
            for e in ev["reportData"]["report"]["events"]["data"]:
                if e.get("type") != "applybuff":
                    continue
                name = e.get("name") or guid2name.get(e.get("abilityGameID"), "")
                player = id2name.get(e.get("targetID"))
                kind = _classify(name)
                if player and kind and (not core or player in core):
                    survival[player][kind] += 1

        await asyncio.gather(*(process(r) for r in relevant))

        return {
            "boss": {
                "name": boss_name, "zone": zone, "encounter_id": encounter_id,
                "pulls": total_pulls, "kills": kills, "wipes": wipes,
                "best_kill_s": best_kill_s, "best_wipe_pct": best_wipe_pct,
            },
            "damage": _rank_dps(dmg),
            "healing": _rank_hps(heal),
            "deaths": _rank_deaths(deaths),
            "survival": _rank_survival(survival, core),
        }
    finally:
        await client.aclose()


# ── accumulation helpers ──────────────────────────────────────────────────────
def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _entries(table):
    if not isinstance(table, dict):
        return []
    data = table.get("data", table)
    if isinstance(data, dict):
        e = data.get("entries", [])
        return e if isinstance(e, list) else []
    return data if isinstance(data, list) else []


def _accumulate_table(table, acc: dict, core: set[str], *, with_class: bool) -> None:
    for e in _entries(table):
        name = e.get("name")
        if not name or (core and name not in core):
            continue
        acc[name]["total"] += _num(e.get("total"))
        acc[name]["active_s"] += _num(e.get("activeTime")) / 1000.0
        if with_class:
            acc[name]["player_class"] = e.get("type") or acc[name]["player_class"]


def _accumulate_deaths(table, acc: dict, core: set[str], fight_start: int) -> None:
    parsed = []
    for e in _entries(table):
        name = e.get("name")
        if not name or (core and name not in core):
            continue
        t = e.get("deathTime", e.get("timestamp", e.get("startTime")))
        ability = e.get("ability") or e.get("killingBlow") or {}
        parsed.append((name, max(0.0, (_num(t) - fight_start) / 1000.0),
                       ability.get("name", "Unknown") if isinstance(ability, dict) else "Unknown"))
    parsed.sort(key=lambda x: x[1])
    for order, (name, rel_s, killer) in enumerate(parsed, start=1):
        acc[name]["deaths"] += 1
        acc[name]["time_sum"] += rel_s
        acc[name]["killers"][killer] += 1
        if order == 1:
            acc[name]["first"] += 1


# ── ranking / shaping helpers ─────────────────────────────────────────────────
def _rank_dps(dmg: dict) -> list[dict]:
    rows = []
    for name, d in dmg.items():
        active = max(d["active_s"], 1.0)
        rows.append({"player": name, "player_class": d["player_class"],
                     "dps": d["total"] / active, "total": d["total"], "active_s": d["active_s"]})
    return sorted(rows, key=lambda r: r["dps"], reverse=True)


def _rank_hps(heal: dict) -> list[dict]:
    rows = []
    for name, d in heal.items():
        active = max(d["active_s"], 1.0)
        rows.append({"player": name, "hps": d["total"] / active, "total": d["total"]})
    return sorted(rows, key=lambda r: r["hps"], reverse=True)


def _rank_deaths(deaths: dict) -> list[dict]:
    rows = []
    for name, d in deaths.items():
        n = d["deaths"]
        top = d["killers"].most_common(1)[0][0] if d["killers"] else "—"
        rows.append({"player": name, "deaths": n, "first_deaths": d["first"],
                     "avg_time_s": (d["time_sum"] / n) if n else 0.0, "top_killer": top})
    return sorted(rows, key=lambda r: r["deaths"], reverse=True)


def _rank_survival(survival: dict, core: set[str]) -> list[dict]:
    # Include core raiders with zero usage so gaps are visible.
    names = set(survival) | (core or set())
    rows = []
    for name in names:
        s = survival.get(name, {"consumable": 0, "defensive": 0})
        rows.append({"player": name, "consumables": s["consumable"], "defensives": s["defensive"]})
    return sorted(rows, key=lambda r: (r["defensives"], r["consumables"]))
