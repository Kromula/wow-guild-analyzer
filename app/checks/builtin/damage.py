"""Damage performance checks: top performers and underperformers."""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.builtin._util import fmt_rate
from app.checks.registry import register
from app.ingest.normalize import AnalysisDataset


def _healer_names(ds: AnalysisDataset) -> set[str]:
    """Players whose healing output exceeds their damage output — i.e. real
    healers. Data-driven so it works regardless of spec naming, which the WCL
    damage table does not expose (it gives class, not spec)."""
    if ds.healing.is_empty():
        return set()
    heal = ds.healing.group_by("player").agg(pl.col("total").sum().alias("heal"))
    dmg = (ds.damage.group_by("player").agg(pl.col("total").sum().alias("dmg"))
           if not ds.damage.is_empty() else pl.DataFrame({"player": [], "dmg": []}))
    merged = heal.join(dmg, on="player", how="left").with_columns(pl.col("dmg").fill_null(0.0))
    return set(merged.filter(pl.col("heal") > pl.col("dmg")).get_column("player").to_list())


def _player_dps(ds: AnalysisDataset) -> pl.DataFrame:
    """Time-weighted average DPS per player across the timeframe."""
    if ds.damage.is_empty():
        return ds.damage
    return (
        ds.damage.group_by("player")
        .agg(
            pl.col("player_class").first(),
            pl.col("total").sum().alias("total_damage"),
            pl.col("active_time_s").sum().alias("active_s"),
        )
        .with_columns((pl.col("total_damage") / pl.col("active_s").clip(lower_bound=1)).alias("dps"))
        .sort("dps", descending=True)
    )


@register
class HighDamage(Check):
    id = "high-damage"
    name = "Top Damage Dealers"
    description = "Players with the highest time-weighted DPS across the selected timeframe."
    category = Category.PERFORMANCE
    order = 10

    def run(self, ds: AnalysisDataset) -> CheckResult:
        df = _player_dps(ds)
        rows = [
            CheckRow(
                player=r["player"],
                player_class=r["player_class"],
                value=r["dps"],
                display=fmt_rate(r["dps"], "DPS"),
                detail=f"{fmt_rate(r['total_damage'], 'total')} over {r['active_s']:.0f}s",
            )
            for r in df.head(10).to_dicts()
        ]
        top = rows[0].player if rows else "nobody"
        return self.result(
            severity=Severity.GOOD,
            headline=f"{top} leads the damage meters." if rows else "No damage data in range.",
            columns=["Player", "DPS", "Detail"],
            rows=rows,
        )


@register
class LowDamage(Check):
    id = "low-damage"
    name = "Underperforming Damage"
    description = ("DPS-role players in the bottom of the meters. Players who heal more "
                  "than they damage (healers) are excluded so the comparison is fair.")
    category = Category.PERFORMANCE
    order = 11

    def run(self, ds: AnalysisDataset) -> CheckResult:
        df = _player_dps(ds)
        healers = _healer_names(ds)
        if not df.is_empty() and healers:
            df = df.filter(~pl.col("player").is_in(list(healers)))
        rows = [
            CheckRow(
                player=r["player"],
                player_class=r["player_class"],
                value=r["dps"],
                display=fmt_rate(r["dps"], "DPS"),
                detail=f"{fmt_rate(r['total_damage'], 'total')} over {r['active_s']:.0f}s",
            )
            for r in df.sort("dps").head(10).to_dicts()
        ]
        worst = rows[0].player if rows else "nobody"
        return self.result(
            severity=Severity.WARN,
            headline=f"{worst} is lowest among damage roles." if rows else "No damage data in range.",
            columns=["Player", "DPS", "Detail"],
            rows=rows,
        )
