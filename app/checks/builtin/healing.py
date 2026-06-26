"""Healing performance check: top healers by throughput."""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.builtin._util import fmt_rate, healer_names
from app.checks.registry import register
from app.ingest.normalize import AnalysisDataset


@register
class TopHealing(Check):
    id = "high-healing"
    name = "Top Healers"
    description = ("Healers ranked by healing throughput (HPS). Only healer-spec players are "
                  "shown — DPS/tank off-healing and hybrid output are filtered out using the "
                  "role data from the logs.")
    category = Category.PERFORMANCE
    order = 12

    def run(self, ds: AnalysisDataset) -> CheckResult:
        if ds.healing.is_empty():
            return self.result(severity=Severity.INFO, headline="No healing data in range.",
                               columns=["Player", "HPS", "Detail"], rows=[])
        agg = (
            ds.healing.group_by("player")
            .agg(
                pl.col("player_class").first(),
                pl.col("total").sum().alias("total_healing"),
                pl.col("hps").mean().alias("hps"),
            )
            .sort("total_healing", descending=True)
        )
        # Keep only healer-spec players. Fall back to the unfiltered list if role
        # data is unavailable or matches nobody, so the grid never goes blank.
        healers = healer_names(ds)
        if healers:
            only_healers = agg.filter(pl.col("player").is_in(list(healers)))
            if not only_healers.is_empty():
                agg = only_healers
        rows = [
            CheckRow(player=r["player"], player_class=r["player_class"], value=r["hps"],
                     display=fmt_rate(r["hps"], "HPS"),
                     detail=f"{fmt_rate(r['total_healing'], 'total')}")
            for r in agg.head(10).to_dicts()
        ]
        top = rows[0].player if rows else "nobody"
        return self.result(
            severity=Severity.GOOD,
            headline=f"{top} leads the healing meters." if rows else "No healing data in range.",
            columns=["Player", "HPS", "Detail"],
            rows=rows,
        )
