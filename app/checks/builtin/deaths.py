"""Survival checks: who dies the most *early*, and who tends to die first.

A raid wipe means everyone dies, so raw total deaths is mostly a measure of how
many wipes a player was present for — roughly equal across the roster and
especially noisy for tanks, who die in every cascade. The meaningful, often
avoidable deaths are the *first few* of a pull: they're the cause or trigger of
the wipe rather than the inevitable fallout. So we rank by early deaths only.
"""
from __future__ import annotations

import polars as pl

from app.checks.base import Category, Check, CheckResult, CheckRow, Severity
from app.checks.registry import register
from app.config import settings
from app.ingest.normalize import AnalysisDataset


def _culpable_deaths(ds: AnalysisDataset) -> pl.DataFrame:
    """Deaths with unavoidable one-shots removed, so they don't blame the victim.

    Drops any death whose killing-blow ability is in `non_culpable_death_abilities`
    (e.g. Terminate — a missed-interrupt wipe, not the dead player's fault). Death
    order is left as-is: other deaths keep their original position, so this only
    *removes* rows, it doesn't re-rank survivors. Shared by both death checks so
    they stay consistent."""
    d = ds.deaths
    skip = [s.lower() for s in settings.non_culpable_death_abilities]
    if d.is_empty() or not skip:
        return d
    return d.filter(~pl.col("ability").str.to_lowercase().is_in(skip))


@register
class FrequentDeaths(Check):
    id = "frequent-deaths"
    name = "Most Early Deaths"
    description = (
        "Players who are most often among the first "
        f"{settings.early_death_cutoff} to die in a pull. Early deaths tend to be "
        "the avoidable, wipe-causing ones; later deaths are usually just the rest "
        "of the raid going down with the pull, so they're excluded here."
    )
    category = Category.SURVIVAL
    order = 20

    def run(self, ds: AnalysisDataset) -> CheckResult:
        cutoff = settings.early_death_cutoff
        deaths = _culpable_deaths(ds)
        if deaths.is_empty():
            return self.result(severity=Severity.INFO, headline="No deaths recorded — clean runs!",
                               columns=["Player", "Early deaths"], rows=[])
        early = deaths.filter(pl.col("death_order") <= cutoff)
        if early.is_empty():
            return self.result(severity=Severity.INFO,
                               headline="No early deaths — wipes weren't anyone dying first.",
                               columns=["Player", "Early deaths"], rows=[])
        agg = (
            early.group_by("player")
            .agg(pl.len().alias("deaths"))
            .sort("deaths", descending=True)
        )
        rows = [
            CheckRow(player=r["player"], value=float(r["deaths"]),
                     display=f"{r['deaths']} early deaths")
            for r in agg.head(12).to_dicts()
        ]
        worst = rows[0] if rows else None
        return self.result(
            severity=Severity.CRITICAL,  # always red — these are the wipe-causing deaths
            headline=(f"{worst.player} was among the first {cutoff} to die "
                      f"{int(worst.value)} times.") if worst else "No early deaths.",
            columns=["Player", "Early deaths"],
            rows=rows,
        )


@register
class DiesFirst(Check):
    id = "dies-first"
    name = "Dies First"
    description = ("How often each player is the very first to die in a pull (death order #1), "
                  "ranked most-first. Repeatedly dying first usually means overpulling threat, "
                  "eating an early mechanic, or not using a defensive on pull. (An *average* death "
                  "order would hide this — most deaths are the wipe cascade, so everyone's average "
                  "sits mid-pack; counting only the first death surfaces the real culprits.)")
    category = Category.SURVIVAL
    order = 21

    def run(self, ds: AnalysisDataset) -> CheckResult:
        cols = ["Player", "First deaths"]
        deaths = _culpable_deaths(ds)
        if deaths.is_empty():
            return self.result(severity=Severity.INFO, headline="No deaths recorded — clean runs!",
                               columns=cols, rows=[])
        firsts = deaths.filter(pl.col("death_order") == 1)
        if firsts.is_empty():
            return self.result(severity=Severity.INFO,
                               headline="No one died first in any pull.", columns=cols, rows=[])
        agg = (
            firsts.group_by("player")
            .agg(pl.len().alias("firsts"))
            .sort(["firsts", "player"], descending=[True, False])
        )
        rows = [
            CheckRow(player=r["player"], value=float(r["firsts"]),
                     display=f"{int(r['firsts'])}")
            for r in agg.head(10).to_dicts()
        ]
        worst = rows[0]
        return self.result(
            severity=Severity.CRITICAL,  # always red — first deaths are the wipe triggers
            headline=f"{worst.player} died first {int(worst.value)} times.",
            columns=cols,
            rows=rows,
        )
