"""Offline smoke test: builds a synthetic AnalysisDataset and runs every
registered check. No API credentials required. Verifies the framework wiring,
auto-discovery, and that each check produces a valid result.

Run:  ./.venv/Scripts/python.exe scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Check details can contain non-ASCII (e.g. "⚠"); force UTF-8 so the Windows
# console (cp1252 by default) doesn't choke when printing results.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

from app.checks import list_checks, run_all
from app.ingest.fetcher import Timeframe
from app.ingest.normalize import AnalysisDataset


def synthetic() -> AnalysisDataset:
    players = pl.DataFrame({
        "player": ["Tankzilla", "Healbot", "Pewpew", "Slacker", "Stabby"],
        "player_class": ["Warrior", "Priest", "Mage", "Hunter", "Rogue"],
        "role": ["tank", "healer", "dps", "dps", "dps"],
    })
    damage = pl.DataFrame({
        "report_code": ["AAAA"] * 5,
        "player": ["Pewpew", "Stabby", "Slacker", "Tankzilla", "Healbot"],
        "player_class": ["Mage", "Rogue", "Hunter", "Warrior", "Priest"],
        "total": [9.0e8, 7.5e8, 2.1e8, 3.0e8, 5.0e7],
        "active_time_s": [300.0, 300.0, 300.0, 300.0, 300.0],
        "dps": [3.0e6, 2.5e6, 7.0e5, 1.0e6, 1.6e5],
    })
    healing = pl.DataFrame({
        "report_code": ["AAAA"], "player": ["Healbot"], "player_class": ["Priest"],
        "total": [8.0e8], "hps": [2.6e6],
    })
    casts = pl.DataFrame({
        "report_code": ["AAAA"] * 4,
        "player": ["Tankzilla", "Pewpew", "Healbot", "Tankzilla"],
        "ability_id": [871, 86949, 47788, 190456],
        "ability_name": ["Shield Wall", "Healing Potion", "Guardian Spirit", "Ardent Defender"],
        "hits": [4.0, 2.0, 3.0, 1.0],
    })
    # Slacker's fight-2 order-1 death is a Terminate — an unavoidable one-shot that
    # the death checks must NOT count against him, so his early-death tally should
    # be 2 (both from fight 1), not 3.
    deaths = pl.DataFrame({
        "report_code": ["AAAA"] * 6,
        "fight_id": [1, 1, 1, 2, 2, 2],
        "player": ["Slacker", "Stabby", "Slacker", "Slacker", "Pewpew", "Stabby"],
        "death_time_s": [12.0, 95.0, 30.0, 8.0, 150.0, 60.0],
        "death_order": [1, 3, 2, 1, 3, 2],
        "ability": ["Fireball", "Cleave", "Fireball", "Terminate", "Enrage", "Cleave"],
    })
    fights = pl.DataFrame({
        "report_code": ["AAAA", "AAAA"], "fight_id": [1, 2],
        "name": ["Boss A", "Boss B"], "difficulty": [5, 5],
        "kill": [True, False], "duration_s": [300.0, 280.0],
    })
    # Avoidable-damage events. Fight 1's 3rd death is at t=95s, fight 2's at t=150s
    # (see `deaths`), so the t=120 and t=200 hits fall in the wipe cascade and must
    # be trimmed. Tankzilla's big t=40 hit is in the live window but must be dropped
    # too — tanks soak Glaives. So only Pewpew@50 (fight 1) and Slacker@100 (fight 2)
    # should remain, with Pewpew on top.
    damage_taken = pl.DataFrame({
        "report_code": ["AAAA"] * 5,
        "fight_id": [1, 1, 1, 2, 2],
        "player": ["Tankzilla", "Pewpew", "Stabby", "Slacker", "Pewpew"],
        "time_s": [40.0, 50.0, 120.0, 100.0, 200.0],
        "amount": [9.9e6, 5.0e5, 9.9e5, 3.0e5, 9.9e5],
    })
    return AnalysisDataset(
        timeframe=Timeframe(days=14, start_ms=0, end_ms=1),
        reports=[{"code": "AAAA", "title": "Test Raid", "zone": "Test Zone"}],
        players=players, fights=fights, damage=damage, healing=healing, casts=casts,
        deaths=deaths, damage_taken=damage_taken,
    )


def main() -> int:
    checks = list_checks()
    print(f"Discovered {len(checks)} checks:")
    for c in checks:
        print(f"  · {c['id']:<18} [{c['category']}]  {c['name']}")

    ds = synthetic()
    print("\nRunning checks on synthetic data:\n")
    # boss_view=True so boss-only checks (Glaives, Top Death Causes) run too.
    results = run_all(ds, boss_view=True)
    assert results, "no results produced"
    for r in results:
        d = r.to_dict()
        assert d["id"] and d["name"] and "rows" in d, f"malformed result: {d}"
        print(f"[{d['severity'].upper():<8}] {d['name']}: {d['headline']}")
        for i, row in enumerate(d["rows"][:3], 1):
            print(f"      {i}. {row['player']:<10} {row['display']:<14} {row['detail']}")
        print()

    print(f"OK — {len(results)} checks ran cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
