"""Tests for the Dies First check.

Dies First counts how often a player is the *first* to die in a pull (death
order #1), ranked most-first — not the average death order (which buries the
signal, since most deaths are the wipe cascade and every average lands mid-pack).
"""
from __future__ import annotations

import polars as pl
import pytest

from app.checks.base import Severity
from app.checks.builtin.deaths import DiesFirst
from app.config import settings
from app.ingest.fetcher import Timeframe
from app.ingest.normalize import AnalysisDataset

TF = Timeframe(days=7, start_ms=0, end_ms=1)
_EMPTY = pl.DataFrame()


def _ds(death_rows: list[dict]) -> AnalysisDataset:
    deaths = pl.DataFrame(death_rows, schema={
        "report_code": pl.Utf8, "fight_id": pl.Int64, "player": pl.Utf8,
        "death_time_s": pl.Float64, "death_order": pl.Int64, "ability": pl.Utf8})
    return AnalysisDataset(timeframe=TF, reports=[], players=_EMPTY, fights=_EMPTY,
                           damage=_EMPTY, healing=_EMPTY, casts=_EMPTY, deaths=deaths,
                           damage_taken=_EMPTY)


def _death(fight, player, order, t=10.0, ability="Boss Hit"):
    return {"report_code": "r", "fight_id": fight, "player": player,
            "death_time_s": t, "death_order": order, "ability": ability}


@pytest.fixture(autouse=True)
def _restore():
    saved = settings.non_culpable_death_abilities
    settings.non_culpable_death_abilities = ()  # don't filter in these tests
    yield
    settings.non_culpable_death_abilities = saved


def test_ranks_by_first_death_count_descending():
    # Alice is first to die in 3 pulls; Bob in 1. Bob dies a LOT but always late.
    rows = []
    for f in range(1, 4):
        rows.append(_death(f, "Alice", 1))
        rows.append(_death(f, "Bob", 5))
    rows.append(_death(4, "Bob", 1))      # Bob first once
    rows.append(_death(4, "Alice", 9))    # Alice late once
    res = DiesFirst().run(_ds(rows)).to_dict()

    ranked = [(r["player"], r["value"]) for r in res["rows"]]
    assert ranked[0] == ("Alice", 3.0)    # most first-deaths on top
    assert ("Bob", 1.0) in ranked
    assert res["columns"] == ["Player", "First deaths", "Detail"]
    assert "Alice" in res["headline"] and "3" in res["headline"]


def test_only_counts_order_one():
    # Nobody is ever first (orders all >= 2) -> nothing to rank.
    rows = [_death(1, "Cara", 2), _death(1, "Dee", 3), _death(2, "Cara", 4)]
    res = DiesFirst().run(_ds(rows)).to_dict()
    assert res["rows"] == []
    assert res["severity"] == Severity.INFO.value


def test_no_deaths_is_info():
    res = DiesFirst().run(_ds([])).to_dict()
    assert res["severity"] == Severity.INFO.value
    assert res["rows"] == []
