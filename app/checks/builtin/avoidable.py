"""Boss-specific avoidable-damage checks.

Currently: Glaive ("Heaven's Glaives") hits on Midnight Falls — an avoidable
mechanic. This check is scoped to that encounter by *data presence*: the Glaive
damage-taken events are only fetched on the Midnight Falls boss panel, so on every
other view `ds.damage_taken` is empty and the check returns None (no card).

Only the "live" portion of each pull counts. Once the pull is effectively lost
the rest of the raid soaks the mechanic too, which isn't representative — so hits
after the `early_death_cutoff`-th death of a pull are dropped, the same wipe-cutoff
convention the death checks use.
"""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.builtin._util import fmt_num, tank_names
from app.checks.registry import register
from app.config import settings
from app.ingest.normalize import AnalysisDataset


def _live_hits(ds: AnalysisDataset) -> pl.DataFrame:
    """Damage-taken rows trimmed to before each pull's `early_death_cutoff`-th
    death. Pulls with fewer deaths than the cutoff are kept in full."""
    cutoff = settings.early_death_cutoff
    nth_death = (
        ds.deaths.filter(pl.col("death_order") == cutoff)
        .select("report_code", "fight_id", pl.col("death_time_s").alias("cutoff_s"))
        if not ds.deaths.is_empty()
        else pl.DataFrame(schema={"report_code": pl.Utf8, "fight_id": pl.Int64, "cutoff_s": pl.Float64})
    )
    return (
        ds.damage_taken.join(nth_death, on=["report_code", "fight_id"], how="left")
        # No Nth death => pull never reached the cutoff => count the whole pull.
        .with_columns(pl.col("cutoff_s").fill_null(float("inf")))
        .filter(pl.col("time_s") <= pl.col("cutoff_s"))
    )


@register
class GlaiveDamage(Check):
    id = "glaive-damage"
    name = "Glaives Taken (Midnight Falls)"
    description = ("Avoidable Glaive damage taken on Midnight Falls, counting only the live "
                  "portion of each pull (hits after the early-death cutoff are the wipe "
                  "cascade, not avoidable play). Tanks are excluded — they soak Glaives by "
                  "design. Ranked by damage taken; hits shown too.")
    category = Category.SURVIVAL
    order = 40
    boss_only = True

    def run(self, ds: AnalysisDataset) -> CheckResult | None:
        if ds.damage_taken.is_empty():
            return None  # not the Midnight Falls panel — no Glaive data fetched

        live = _live_hits(ds)
        tanks = tank_names(ds)  # tanks soak Glaives intentionally — drop them
        if tanks:
            live = live.filter(~pl.col("player").is_in(list(tanks)))
        agg = (
            live
            .group_by("player")
            .agg(pl.col("amount").sum().alias("dmg"), pl.len().alias("hits"))
            .sort(["dmg", "hits"], descending=True)
        )
        rows = [
            CheckRow(
                player=r["player"],
                value=float(r["dmg"]),
                display=f"{fmt_num(r['dmg'])} taken",
                detail=f"{int(r['hits'])} hit(s)",
            )
            for r in agg.head(14).to_dicts()
        ]
        worst = rows[0] if rows else None
        return self.result(
            severity=Severity.WARN if worst else Severity.GOOD,
            headline=(f"{worst.player} ate the most Glaive damage ({worst.display})."
                      if worst else "No avoidable Glaive damage before the wipe — clean."),
            columns=["Player", "Glaive Damage", "Hits"],
            rows=rows,
        )
