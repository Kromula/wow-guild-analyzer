"""Turn raw WarcraftLogs table JSON into tidy Polars frames.

The WCL `table` payload is provider-shaped JSON. Field names vary slightly by
dataType and game version, so parsing here is deliberately defensive: we read
through several likely keys and skip entries we can't interpret. If the live
schema differs, this module is the single place to adjust.

Normalization is split into two stages so the per-report result can be cached
on disk (see the local-store work):

  * `normalize_report(raw)` -> `ReportFrames` — a pure function of ONE report.
    This is the storable unit; a finished raid log never changes.
  * `assemble(frames, tf)` -> `AnalysisDataset` — the cross-report step
    (attendance / core-raider filter, primary-role pick, concatenation).

`build_dataset(raws, tf)` chains the two and is the contract every check depends
on.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

import polars as pl

from app.config import settings
from app.ingest.fetcher import RawReport, Timeframe


@dataclass
class AnalysisDataset:
    timeframe: Timeframe
    reports: list[dict]        # [{code, title, zone}]
    players: pl.DataFrame      # player, player_class, role (tank|healer|dps|None)
    fights: pl.DataFrame       # report_code, fight_id, name, difficulty, kill, duration_s
    damage: pl.DataFrame       # report_code, player, player_class, total, active_time_s, dps
    healing: pl.DataFrame      # report_code, player, player_class, total, hps
    casts: pl.DataFrame        # report_code, player, ability_id, ability_name, hits
    deaths: pl.DataFrame       # report_code, fight_id, player, death_time_s, death_order, ability
    damage_taken: pl.DataFrame # report_code, fight_id, player, time_s, amount (boss-specific, e.g. Glaives)


# Per-frame schemas, declared once so per-report frames built independently still
# concatenate cleanly in `assemble` (no dtype-inference drift between reports).
_FIGHTS_SCHEMA = {"report_code": pl.Utf8, "fight_id": pl.Int64, "name": pl.Utf8,
                  "difficulty": pl.Int64, "kill": pl.Boolean, "duration_s": pl.Float64}
_DAMAGE_SCHEMA = {"report_code": pl.Utf8, "player": pl.Utf8, "player_class": pl.Utf8,
                  "total": pl.Float64, "active_time_s": pl.Float64, "dps": pl.Float64}
_HEALING_SCHEMA = {"report_code": pl.Utf8, "player": pl.Utf8, "player_class": pl.Utf8,
                   "total": pl.Float64, "hps": pl.Float64}
_CASTS_SCHEMA = {"report_code": pl.Utf8, "player": pl.Utf8, "ability_id": pl.Int64,
                 "ability_name": pl.Utf8, "hits": pl.Float64}
_DEATHS_SCHEMA = {"report_code": pl.Utf8, "fight_id": pl.Int64, "player": pl.Utf8,
                  "death_time_s": pl.Float64, "death_order": pl.Int64, "ability": pl.Utf8}
_DAMAGE_TAKEN_SCHEMA = {"report_code": pl.Utf8, "fight_id": pl.Int64, "player": pl.Utf8,
                        "time_s": pl.Float64, "amount": pl.Float64}
_PLAYERS_SCHEMA = {"player": pl.Utf8, "player_class": pl.Utf8}
_ROLE_ROWS_SCHEMA = {"player": pl.Utf8, "role": pl.Utf8, "count": pl.Float64}


def _df(rows: list[dict], schema: dict) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def _entries(table: object) -> list[dict]:
    """Extract the entries list from a WCL table payload, tolerating shape variation."""
    if not isinstance(table, dict):
        return []
    data = table.get("data", table)
    if isinstance(data, dict):
        entries = data.get("entries", data.get("deaths", []))
        return entries if isinstance(entries, list) else []
    if isinstance(data, list):
        return data
    return []


def _num(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


_ROLE_BUCKETS = {"tanks": "tank", "healers": "healer", "dps": "dps"}


def _role_rows(blob: object) -> list[tuple[str, str, float]]:
    """(player, role, fight_count) triples from a WCL playerDetails payload.

    `playerDetails` buckets actors into tanks/healers/dps, each entry carrying a
    `specs` list of {spec, count} where count is fights played in that spec. We
    weight by that count so a one-off emergency tank doesn't outvote someone's
    real role across the window. Shape-tolerant like `_entries`."""
    if not isinstance(blob, dict):
        return []
    data = blob.get("data", blob)
    details = data.get("playerDetails", data) if isinstance(data, dict) else {}
    if not isinstance(details, dict):
        return []
    out: list[tuple[str, str, float]] = []
    for bucket, role in _ROLE_BUCKETS.items():
        for p in details.get(bucket) or []:
            name = p.get("name") if isinstance(p, dict) else None
            if not name:
                continue
            specs = p.get("specs") or []
            count = sum(_num(s.get("count")) for s in specs) if specs else 1.0
            out.append((name, role, count))
    return out


@dataclass
class ReportFrames:
    """One report's normalized contribution — the unit the local store persists.

    A finished raid log is immutable, so this can be computed once and cached.
    Holds per-report frames plus the small bits `assemble` needs to do the
    cross-report work: `present`/`is_raid_night` feed the attendance filter, and
    `role_rows` feed the primary-role pick."""
    code: str
    title: str
    zone: str
    start_time: int              # absolute report start (ms) — for dedup / completeness
    end_time: int                # absolute report end (ms)
    is_raid_night: bool          # had target-difficulty fights (counts as an attendance night)
    present: list[str]           # players present this night (for attendance)
    players: pl.DataFrame        # player, player_class
    role_rows: pl.DataFrame      # player, role, count (roster-filtered)
    fights: pl.DataFrame
    damage: pl.DataFrame
    healing: pl.DataFrame
    casts: pl.DataFrame
    deaths: pl.DataFrame
    damage_taken: pl.DataFrame


def normalize_report(raw: RawReport) -> ReportFrames:
    """Parse a single report's tables into tidy per-report frames.

    Pure function of `raw` — no cross-report state — so the result is cacheable.
    """
    # Roster of real players for this report — used to exclude pets, guardians,
    # and totems (e.g. "Akaari's Soul") that otherwise pollute the meters.
    roster = {p["name"] for p in raw.players}
    # Damage-taken events identify the victim by actor id, not name.
    id2name = {p["id"]: p["name"] for p in raw.players}
    # First-seen class per player in this report; used as a fallback below.
    player_map: dict[str, str] = {}
    for p in raw.players:
        player_map.setdefault(p["name"], p.get("subType") or "Unknown")

    role_rows = [{"player": n, "role": r, "count": c}
                 for n, r, c in _role_rows(raw.player_details)
                 if not roster or n in roster]

    # Players present this night (for the cross-report attendance filter).
    present = sorted({
        id2name[pid]
        for f in raw.fights
        for pid in (f.get("friendlyPlayers") or [])
        if pid in id2name
    })

    # fight_id -> startTime (ms), so death timestamps can be made relative to the pull.
    fight_start = {f["id"]: f["startTime"] for f in raw.fights}

    fight_rows = [
        {
            "report_code": raw.code,
            "fight_id": f["id"],
            "name": f.get("name", "?"),
            "difficulty": f.get("difficulty"),
            "kill": bool(f.get("kill")),
            "duration_s": max(0.0, (f["endTime"] - f["startTime"]) / 1000.0),
        }
        for f in raw.fights if f.get("encounterID")
    ]

    # Damage
    dmg_rows = []
    for e in _entries(raw.tables.get("DamageDone")):
        name = e.get("name")
        if not name or (roster and name not in roster):
            continue
        active_s = _num(e.get("activeTime")) / 1000.0
        total = _num(e.get("total"))
        dmg_rows.append({
            "report_code": raw.code,
            "player": name,
            "player_class": e.get("type") or player_map.get(name, "Unknown"),
            "total": total,
            "active_time_s": active_s,
            "dps": total / active_s if active_s > 0 else 0.0,
        })

    # Healing
    heal_rows = []
    for e in _entries(raw.tables.get("Healing")):
        name = e.get("name")
        if not name or (roster and name not in roster):
            continue
        active_s = _num(e.get("activeTime")) / 1000.0
        total = _num(e.get("total"))
        heal_rows.append({
            "report_code": raw.code,
            "player": name,
            "player_class": e.get("type") or player_map.get(name, "Unknown"),
            "total": total,
            "hps": total / active_s if active_s > 0 else 0.0,
        })

    # Casts — entries may nest per-ability under "abilities", else are flat.
    cast_rows = []
    for e in _entries(raw.tables.get("Casts")):
        name = e.get("name")
        if not name or (roster and name not in roster):
            continue
        abilities = e.get("abilities")
        if isinstance(abilities, list) and abilities:
            for ab in abilities:
                cast_rows.append({
                    "report_code": raw.code,
                    "player": name,
                    "ability_id": ab.get("guid") or ab.get("id"),
                    "ability_name": ab.get("name", "?"),
                    "hits": _num(ab.get("total")),
                })
        else:
            cast_rows.append({
                "report_code": raw.code,
                "player": name,
                "ability_id": e.get("guid") or e.get("id"),
                "ability_name": e.get("abilityName") or e.get("name", "?"),
                "hits": _num(e.get("total")),
            })

    # Deaths — per fight, ordered by time of death.
    # WCL death `timestamp` is absolute report time; subtract the fight start
    # so death_time_s is "seconds into the pull".
    death_rows = []
    for fight_id, table in raw.deaths_by_fight.items():
        start_ms = fight_start.get(fight_id, 0)
        parsed = []
        for e in _entries(table):
            name = e.get("name")
            if not name or (roster and name not in roster):
                continue
            t = e.get("deathTime", e.get("timestamp", e.get("startTime")))
            ability = e.get("ability") or e.get("killingBlow") or {}
            parsed.append({
                "player": name,
                "death_time_s": max(0.0, (_num(t) - start_ms) / 1000.0),
                "ability": ability.get("name", "Unknown") if isinstance(ability, dict) else "Unknown",
            })
        parsed.sort(key=lambda d: d["death_time_s"])
        for order, d in enumerate(parsed, start=1):
            death_rows.append({
                "report_code": raw.code,
                "fight_id": fight_id,
                "player": d["player"],
                "death_time_s": d["death_time_s"],
                "death_order": order,
                "ability": d["ability"],
            })

    # Damage-taken events (boss-specific, e.g. Glaive hits). Each event is a
    # raw hit with an absolute `timestamp`; rebase to seconds-into-pull like
    # deaths so checks can trim to the live portion of a wipe.
    dmg_taken_rows = []
    for fight_id, events in (raw.damage_taken_by_fight or {}).items():
        start_ms = fight_start.get(fight_id, 0)
        for e in events or []:
            name = id2name.get(e.get("targetID"))
            if not name or (roster and name not in roster):
                continue
            dmg_taken_rows.append({
                "report_code": raw.code,
                "fight_id": fight_id,
                "player": name,
                "time_s": max(0.0, (_num(e.get("timestamp")) - start_ms) / 1000.0),
                "amount": _num(e.get("amount")),
            })

    return ReportFrames(
        code=raw.code,
        title=raw.title,
        zone=raw.zone,
        start_time=raw.start_time,
        end_time=raw.end_time,
        is_raid_night=bool(raw.fights),
        present=present,
        players=_df([{"player": k, "player_class": v} for k, v in player_map.items()], _PLAYERS_SCHEMA),
        role_rows=_df(role_rows, _ROLE_ROWS_SCHEMA),
        fights=_df(fight_rows, _FIGHTS_SCHEMA),
        damage=_df(dmg_rows, _DAMAGE_SCHEMA),
        healing=_df(heal_rows, _HEALING_SCHEMA),
        casts=_df(cast_rows, _CASTS_SCHEMA),
        deaths=_df(death_rows, _DEATHS_SCHEMA),
        damage_taken=_df(dmg_taken_rows, _DAMAGE_TAKEN_SCHEMA),
    )


def canonical_report_codes(reports) -> set[str]:
    """Codes to KEEP when the same night is logged more than once.

    `reports` is an iterable of `(code, zone, start_ms, end_ms, n_fights)`. When
    several raiders upload the same night, those reports overlap in time and would
    double-count everything. We cluster reports by zone (so two *different* raids
    on the same evening aren't merged) and then by overlapping time window, and
    keep one canonical report per cluster: the one with the most fights, breaking
    ties by longest coverage then lexically-smallest code (for determinism).
    """
    by_zone: dict[object, list] = defaultdict(list)
    for code, zone, start, end, n in reports:
        by_zone[zone].append((start, end, n, code))

    keep: set[str] = set()
    for items in by_zone.values():
        items.sort()  # by start, then end
        cluster: list[tuple] = []
        cluster_end = None
        for start, end, n, code in items:
            if cluster and cluster_end is not None and start <= cluster_end:
                cluster.append((n, end - start, code))
                cluster_end = max(cluster_end, end)
            else:
                if cluster:
                    keep.add(_canonical(cluster))
                cluster = [(n, end - start, code)]
                cluster_end = end
        if cluster:
            keep.add(_canonical(cluster))
    return keep


def _canonical(cluster: list[tuple]) -> str:
    """Pick the canonical code from a cluster of (n_fights, span, code) tuples."""
    return sorted(cluster, key=lambda c: (-c[0], -c[1], c[2]))[0][2]


def _dedupe(frames: list[ReportFrames]) -> list[ReportFrames]:
    if not settings.dedupe_overlapping_logs or len(frames) < 2:
        return frames
    keep = canonical_report_codes(
        (f.code, f.zone, f.start_time, f.end_time, f.fights.height) for f in frames)
    return [f for f in frames if f.code in keep]


def _core_raiders(frames: list[ReportFrames]) -> set[str]:
    """Players present for >= min_attendance_pct of the raid nights in the window.

    Presence comes from each report's `present` list (the difficulty-filtered
    fights' `friendlyPlayers`). Excludes one-off pugs and socials. Returns an
    empty set (meaning "no filter") if attendance can't be computed or the
    threshold is disabled.
    """
    if settings.min_attendance_pct <= 0:
        return set()
    attended: Counter[str] = Counter()
    nights = 0
    for fr in frames:
        if not fr.is_raid_night:
            continue
        nights += 1
        attended.update(fr.present)
    if nights == 0:
        return set()
    threshold = settings.min_attendance_pct * nights
    return {name for name, count in attended.items() if count >= threshold}


def _filter_players(df: pl.DataFrame, keep: set[str]) -> pl.DataFrame:
    if not keep or df.is_empty() or "player" not in df.columns:
        return df
    return df.filter(pl.col("player").is_in(list(keep)))


def _concat(frames: list[ReportFrames], attr: str, empty) -> pl.DataFrame:
    parts = [getattr(f, attr) for f in frames]
    return pl.concat(parts) if parts else empty()


def assemble(frames: list[ReportFrames], tf: Timeframe) -> AnalysisDataset:
    """Combine per-report frames into the cross-report AnalysisDataset.

    This is where window-wide decisions live: duplicate-night de-duplication,
    the core-raider attendance filter, the per-player primary-role pick, and
    concatenation."""
    frames = _dedupe(frames)
    report_rows = [{"code": f.code, "title": f.title, "zone": f.zone} for f in frames]

    # First-seen class per player, and role fight-counts, accumulated across reports.
    player_rows: dict[str, str] = {}
    role_counts: dict[str, Counter] = defaultdict(Counter)
    for fr in frames:
        for row in fr.players.iter_rows(named=True):
            player_rows.setdefault(row["player"], row["player_class"])
        for row in fr.role_rows.iter_rows(named=True):
            role_counts[row["player"]][row["role"]] += row["count"]

    keep = _core_raiders(frames)

    # Primary role = the role a player spent the most fights in across the window.
    player_role = {name: counts.most_common(1)[0][0] for name, counts in role_counts.items() if counts}

    players = (
        pl.DataFrame([{"player": k, "player_class": v, "role": player_role.get(k)}
                      for k, v in player_rows.items()])
        if player_rows else pl.DataFrame(schema={"player": pl.Utf8, "player_class": pl.Utf8, "role": pl.Utf8})
    )
    if "role" in players.columns:
        players = players.with_columns(pl.col("role").cast(pl.Utf8))  # all-None -> Utf8, not Null

    return AnalysisDataset(
        timeframe=tf,
        reports=report_rows,
        players=_filter_players(players, keep),
        fights=_concat(frames, "fights", _empty_fights),
        damage=_filter_players(_concat(frames, "damage", _empty_damage), keep),
        healing=_filter_players(_concat(frames, "healing", _empty_healing), keep),
        casts=_filter_players(_concat(frames, "casts", _empty_casts), keep),
        deaths=_filter_players(_concat(frames, "deaths", _empty_deaths), keep),
        damage_taken=_filter_players(_concat(frames, "damage_taken", _empty_damage_taken), keep),
    )


def build_dataset(raws: list[RawReport], tf: Timeframe) -> AnalysisDataset:
    """Normalize each report and assemble them into the analysis dataset."""
    return assemble([normalize_report(raw) for raw in raws], tf)


def _empty_fights() -> pl.DataFrame:
    return pl.DataFrame(schema=_FIGHTS_SCHEMA)


def _empty_damage() -> pl.DataFrame:
    return pl.DataFrame(schema=_DAMAGE_SCHEMA)


def _empty_healing() -> pl.DataFrame:
    return pl.DataFrame(schema=_HEALING_SCHEMA)


def _empty_casts() -> pl.DataFrame:
    return pl.DataFrame(schema=_CASTS_SCHEMA)


def _empty_deaths() -> pl.DataFrame:
    return pl.DataFrame(schema=_DEATHS_SCHEMA)


def _empty_damage_taken() -> pl.DataFrame:
    return pl.DataFrame(schema=_DAMAGE_TAKEN_SCHEMA)
