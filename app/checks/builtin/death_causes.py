"""What's killing the raid early — the abilities behind the first few deaths.

Complements the death checks (which rank *who* dies early): this ranks *what*
ability landed the killing blow on the first `early_death_cutoff` deaths of each
pull, so a boss view shows the mechanics actually opening pulls badly. Unlike the
who-dies checks, Terminate and other one-shots are kept here on purpose — they're
real, actionable causes (e.g. a missed interrupt), even if the victim isn't at
fault. Boss-panel only: blending every boss's abilities on the overall page would
be meaningless.
"""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.registry import register
from app.config import settings
from app.ingest.normalize import AnalysisDataset


@register
class FirstDeathCauses(Check):
    id = "first-death-causes"
    name = "Top Death Causes"
    description = (
        f"Boss abilities landing the killing blow on the first {settings.early_death_cutoff} "
        "deaths of each pull — what's opening pulls badly. Ranked by early deaths caused, "
        "with pulls affected. Boss view only."
    )
    category = Category.SURVIVAL
    order = 23  # after Glaives Taken (22), which slots in just below Dies First (21)
    boss_only = True

    def run(self, ds: AnalysisDataset) -> CheckResult | None:
        cutoff = settings.early_death_cutoff
        columns = ["Ability", "Early deaths", "Pulls"]
        if ds.deaths.is_empty():
            return self.result(severity=Severity.INFO, headline="No deaths recorded — clean pulls!",
                               columns=columns, rows=[])
        early = ds.deaths.filter(pl.col("death_order") <= cutoff)
        if early.is_empty():
            return self.result(severity=Severity.INFO,
                               headline="No early deaths to attribute.", columns=columns, rows=[])

        agg = (
            early.group_by("ability")
            .agg(
                pl.len().alias("deaths"),
                # distinct pulls this ability opened, so one ability wiping the raid
                # in a single pull doesn't outrank one that recurs across many.
                (pl.col("report_code") + "_" + pl.col("fight_id").cast(pl.Utf8))
                .n_unique().alias("pulls"),
            )
            .sort(["deaths", "pulls"], descending=True)
        )
        dicts = agg.head(8).to_dicts()
        rows = [
            CheckRow(player=r["ability"], value=float(r["deaths"]),
                     display=f"{r['deaths']}", detail=f"{r['pulls']} pull(s)")
            for r in dicts
        ]
        top = dicts[0]
        return self.result(
            severity=Severity.WARN,
            headline=(f"{top['ability']} caused the most early deaths "
                      f"({top['deaths']} across {top['pulls']} pull(s))."),
            columns=columns,
            rows=rows,
        )
