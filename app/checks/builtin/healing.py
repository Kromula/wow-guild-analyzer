"""Healing performance check: top healers by throughput."""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.builtin._util import fmt_rate
from app.checks.registry import register
from app.ingest.normalize import AnalysisDataset


@register
class TopHealing(Check):
    id = "high-healing"
    name = "Top Healers"
    description = "Players with the highest healing throughput (HPS) across the selected timeframe."
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
