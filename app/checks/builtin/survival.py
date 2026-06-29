"""Survival-resource checks: are players using personal defensives and consumables?

Both are derived from the Casts table — a defensive cast (Shield Wall, Barkskin…)
or a consumable use (healthstone, healing potion) shows up as a cast attributed to
the player. Ranked ascending so the people *not* pressing these buttons surface at
the top, with the whole core roster included (zeros are the point).
"""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.builtin._util import classify_ability
from app.checks.registry import register
from app.ingest.normalize import AnalysisDataset


def _usage_by_player(ds: AnalysisDataset, kind: str) -> pl.DataFrame:
    """Per-player count of casts classified as `kind`, including every core
    raider (filled with 0 if they never used one), sorted fewest-first."""
    roster = (ds.players.select("player", "player_class")
              if not ds.players.is_empty()
              else pl.DataFrame(schema={"player": pl.Utf8, "player_class": pl.Utf8}))
    if ds.casts.is_empty():
        counts = pl.DataFrame(schema={"player": pl.Utf8, "uses": pl.Float64})
    else:
        counts = (
            ds.casts
            .with_columns(pl.col("ability_name")
                          .map_elements(classify_ability, return_dtype=pl.Utf8).alias("kind"))
            .filter(pl.col("kind") == kind)
            .group_by("player").agg(pl.col("hits").sum().alias("uses"))
        )
    return (roster.join(counts, on="player", how="left")
            .with_columns(pl.col("uses").fill_null(0.0))
            .sort(["uses", "player"]))


@register
class Defensives(Check):
    id = "defensives"
    name = "Defensives Used"
    description = ("How often each raider pressed a personal defensive (Shield Wall, "
                  "Barkskin, etc.). Lowest first — zeros usually mean someone is sitting "
                  "on their cooldowns. Edit survival_abilities.json to tune the list.")
    category = Category.SURVIVAL
    order = 30

    def run(self, ds: AnalysisDataset) -> CheckResult:
        df = _usage_by_player(ds, "defensive")
        rows = [
            CheckRow(player=r["player"], player_class=r["player_class"], value=r["uses"],
                     display=f"{int(r['uses'])}",
                     detail="⚠ none" if r["uses"] == 0 else "")
            for r in df.head(14).to_dicts()
        ]
        zeros = int(df.filter(pl.col("uses") == 0).height) if not df.is_empty() else 0
        return self.result(
            severity=Severity.WARN if zeros else Severity.GOOD,
            headline=(f"{zeros} raider(s) used no personal defensives." if zeros
                      else "Everyone popped at least one defensive."),
            columns=["Player", "Defensives", "Detail"],
            rows=rows,
        )


@register
class Consumables(Check):
    id = "consumables"
    name = "Consumables Used"
    description = ("Healthstones and healing potions used (counted from casts, since "
                  "they're instant heals that often leave no buff). Lowest first — a 0 "
                  "means no emergency button pressed all timeframe.")
    category = Category.SURVIVAL
    order = 31

    def run(self, ds: AnalysisDataset) -> CheckResult:
        df = _usage_by_player(ds, "consumable")
        rows = [
            CheckRow(player=r["player"], player_class=r["player_class"], value=r["uses"],
                     display=f"{int(r['uses'])}",
                     detail="⚠ none used" if r["uses"] == 0 else "")
            for r in df.head(14).to_dicts()
        ]
        zeros = int(df.filter(pl.col("uses") == 0).height) if not df.is_empty() else 0
        # A review panel (who's using the fewest), not pass/fail — always WARN so it
        # reads as "worth a glance" (amber) and sorts next to its sibling Defensives
        # Used rather than sinking to the bottom as GOOD/green.
        return self.result(
            severity=Severity.WARN,
            headline=(f"{zeros} raider(s) used no healthstone or potion." if zeros
                      else "Lowest healthstone/potion users — worth a glance."),
            columns=["Player", "Pots/Stones", "Detail"],
            rows=rows,
        )
