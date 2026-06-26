"""Turn raw WarcraftLogs table JSON into tidy Polars frames.

The WCL `table` payload is provider-shaped JSON. Field names vary slightly by
dataType and game version, so parsing here is deliberately defensive: we read
through several likely keys and skip entries we can't interpret. If the live
schema differs, this module is the single place to adjust.

The resulting AnalysisDataset is the contract every check depends on.
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


def core_raiders(raws: list[RawReport]) -> set[str]:
    """Players present for >= min_attendance_pct of the raid nights in the window.

    Presence is determined per report from the (difficulty-filtered) fights'
    `friendlyPlayers`, mapped through that report's actor ids. Excludes one-off
    pugs and socials. Returns an empty set (meaning "no filter") if attendance
    can't be computed or the threshold is disabled.
    """
    if settings.min_attendance_pct <= 0:
        return set()

    attended: Counter[str] = Counter()
    nights = 0
    for raw in raws:
        if not raw.fights:  # report had no fights at the target difficulty
            continue
        nights += 1
        id2name = {p["id"]: p["name"] for p in raw.players}
        present = {
            id2name[pid]
            for f in raw.fights
            for pid in (f.get("friendlyPlayers") or [])
            if pid in id2name
        }
        attended.update(present)

    if nights == 0:
        return set()
    threshold = settings.min_attendance_pct * nights
    return {name for name, count in attended.items() if count >= threshold}


def _filter_players(df: pl.DataFrame, keep: set[str]) -> pl.DataFrame:
    if not keep or df.is_empty() or "player" not in df.columns:
        return df
    return df.filter(pl.col("player").is_in(list(keep)))


def build_dataset(raws: list[RawReport], tf: Timeframe) -> AnalysisDataset:
    player_rows: dict[str, str] = {}
    fight_rows: list[dict] = []
    dmg_rows: list[dict] = []
    heal_rows: list[dict] = []
    cast_rows: list[dict] = []
    death_rows: list[dict] = []
    dmg_taken_rows: list[dict] = []
    report_rows: list[dict] = []
    # player -> {role: total fight count}, accumulated across reports so a primary
    # role can be picked per player for the whole window.
    role_counts: dict[str, Counter] = defaultdict(Counter)

    for raw in raws:
        report_rows.append({"code": raw.code, "title": raw.title, "zone": raw.zone})

        for p in raw.players:
            player_rows.setdefault(p["name"], p.get("subType") or "Unknown")

        # Roster of real players for this report — used to exclude pets, guardians,
        # and totems (e.g. "Akaari's Soul") that otherwise pollute the meters.
        roster = {p["name"] for p in raw.players}
        # Damage-taken events identify the victim by actor id, not name.
        id2name = {p["id"]: p["name"] for p in raw.players}

        for name, role, count in _role_rows(raw.player_details):
            if not roster or name in roster:
                role_counts[name][role] += count

        # fight_id -> startTime (ms), so death timestamps can be made relative to the pull.
        fight_start = {f["id"]: f["startTime"] for f in raw.fights}

        for f in raw.fights:
            if not f.get("encounterID"):
                continue
            fight_rows.append({
                "report_code": raw.code,
                "fight_id": f["id"],
                "name": f.get("name", "?"),
                "difficulty": f.get("difficulty"),
                "kill": bool(f.get("kill")),
                "duration_s": max(0.0, (f["endTime"] - f["startTime"]) / 1000.0),
            })

        # Damage
        for e in _entries(raw.tables.get("DamageDone")):
            name = e.get("name")
            if not name or (roster and name not in roster):
                continue
            active_s = _num(e.get("activeTime")) / 1000.0
            total = _num(e.get("total"))
            dmg_rows.append({
                "report_code": raw.code,
                "player": name,
                "player_class": e.get("type") or player_rows.get(name, "Unknown"),
                "total": total,
                "active_time_s": active_s,
                "dps": total / active_s if active_s > 0 else 0.0,
            })

        # Healing
        for e in _entries(raw.tables.get("Healing")):
            name = e.get("name")
            if not name or (roster and name not in roster):
                continue
            active_s = _num(e.get("activeTime")) / 1000.0
            total = _num(e.get("total"))
            heal_rows.append({
                "report_code": raw.code,
                "player": name,
                "player_class": e.get("type") or player_rows.get(name, "Unknown"),
                "total": total,
                "hps": total / active_s if active_s > 0 else 0.0,
            })

        # Casts — entries may nest per-ability under "abilities", else are flat.
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
        for fight_id, table in raw.deaths_by_fight.items():
            entries = _entries(table)
            start_ms = fight_start.get(fight_id, 0)
            parsed = []
            for e in entries:
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

    keep = core_raiders(raws)

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
        fights=pl.DataFrame(fight_rows) if fight_rows else _empty_fights(),
        damage=_filter_players(pl.DataFrame(dmg_rows) if dmg_rows else _empty_damage(), keep),
        healing=_filter_players(pl.DataFrame(heal_rows) if heal_rows else _empty_healing(), keep),
        casts=_filter_players(pl.DataFrame(cast_rows) if cast_rows else _empty_casts(), keep),
        deaths=_filter_players(pl.DataFrame(death_rows) if death_rows else _empty_deaths(), keep),
        damage_taken=_filter_players(
            pl.DataFrame(dmg_taken_rows) if dmg_taken_rows else _empty_damage_taken(), keep),
    )


def _empty_fights() -> pl.DataFrame:
    return pl.DataFrame(schema={"report_code": pl.Utf8, "fight_id": pl.Int64, "name": pl.Utf8,
                                "difficulty": pl.Int64, "kill": pl.Boolean, "duration_s": pl.Float64})


def _empty_damage() -> pl.DataFrame:
    return pl.DataFrame(schema={"report_code": pl.Utf8, "player": pl.Utf8, "player_class": pl.Utf8,
                                "total": pl.Float64, "active_time_s": pl.Float64, "dps": pl.Float64})


def _empty_healing() -> pl.DataFrame:
    return pl.DataFrame(schema={"report_code": pl.Utf8, "player": pl.Utf8, "player_class": pl.Utf8,
                                "total": pl.Float64, "hps": pl.Float64})


def _empty_casts() -> pl.DataFrame:
    return pl.DataFrame(schema={"report_code": pl.Utf8, "player": pl.Utf8, "ability_id": pl.Int64,
                                "ability_name": pl.Utf8, "hits": pl.Float64})


def _empty_deaths() -> pl.DataFrame:
    return pl.DataFrame(schema={"report_code": pl.Utf8, "fight_id": pl.Int64, "player": pl.Utf8,
                                "death_time_s": pl.Float64, "death_order": pl.Int64, "ability": pl.Utf8})


def _empty_damage_taken() -> pl.DataFrame:
    return pl.DataFrame(schema={"report_code": pl.Utf8, "fight_id": pl.Int64, "player": pl.Utf8,
                                "time_s": pl.Float64, "amount": pl.Float64})
