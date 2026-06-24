"""Survival checks: who dies the most, and who tends to die first."""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.registry import register
from app.ingest.normalize import AnalysisDataset


@register
class FrequentDeaths(Check):
    id = "frequent-deaths"
    name = "Most Deaths"
    description = "Players who died the most across all pulls in the timeframe."
    category = Category.SURVIVAL
    order = 20

    def run(self, ds: AnalysisDataset) -> CheckResult:
        if ds.deaths.is_empty():
            return self.result(severity=Severity.INFO, headline="No deaths recorded — clean runs!",
                               columns=["Player", "Deaths", "Detail"], rows=[])
        agg = (
            ds.deaths.group_by("player")
            .agg(pl.len().alias("deaths"), pl.col("ability").mode().first().alias("top_killer"))
            .sort("deaths", descending=True)
        )
        rows = [
            CheckRow(player=r["player"], value=float(r["deaths"]), display=f"{r['deaths']} deaths",
                     detail=f"most often to: {r['top_killer']}")
            for r in agg.head(12).to_dicts()
        ]
        worst = rows[0] if rows else None
        return self.result(
            severity=Severity.CRITICAL if worst and worst.value >= 5 else Severity.WARN,
            headline=f"{worst.player} died {int(worst.value)} times." if worst else "No deaths.",
            columns=["Player", "Deaths", "Detail"],
            rows=rows,
        )


@register
class DiesFirst(Check):
    id = "dies-first"
    name = "Dies First"
    description = ("Players who tend to die earliest in a pull (low average death order and "
                  "early death timing). Often a sign of overpulling threat or missing mechanics.")
    category = Category.SURVIVAL
    order = 21

    def run(self, ds: AnalysisDataset) -> CheckResult:
        if ds.deaths.is_empty():
            return self.result(severity=Severity.INFO, headline="No deaths recorded — clean runs!",
                               columns=["Player", "Avg death order", "Detail"], rows=[])
        agg = (
            ds.deaths.group_by("player")
            .agg(
                pl.col("death_order").mean().alias("avg_order"),
                pl.col("death_time_s").mean().alias("avg_time"),
                pl.len().alias("deaths"),
            )
            # Only meaningful for players who die with some regularity.
            .filter(pl.col("deaths") >= 2)
            .sort(["avg_order", "avg_time"])
        )
        rows = [
            CheckRow(player=r["player"], value=r["avg_order"],
                     display=f"#{r['avg_order']:.1f} avg",
                     detail=f"avg {r['avg_time']:.0f}s into pull · {r['deaths']} deaths")
            for r in agg.head(10).to_dicts()
        ]
        first = rows[0].player if rows else "nobody"
        return self.result(
            severity=Severity.WARN,
            headline=f"{first} consistently dies earliest." if rows else "Not enough repeated deaths to rank.",
            columns=["Player", "Avg death order", "Detail"],
            rows=rows,
        )
