"""Regression tests for death_order normalization (issue #4).

The Deaths table is parsed without the roster filter that Damage/Healing/Casts
apply, so non-player deaths (pets, guardians, totems — e.g. "Akaari's Soul")
could occupy early `death_order` slots and distort the early-death counts. These
tests pin the fix: non-player deaths must be dropped *before* order is assigned.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.ingest.fetcher import RawReport, Timeframe
from app.ingest.normalize import build_dataset

START = 1_000_000  # fight start (ms)
ROSTER = [
    {"id": 1, "name": "Tankadin"},
    {"id": 2, "name": "Healer"},
    {"id": 3, "name": "Mage"},
    {"id": 4, "name": "Rogue"},
    {"id": 5, "name": "Warrior"},
]


def _at(sec: float) -> int:
    return START + int(sec * 1000)


def _build(entries, *, attendance: float):
    """Build a one-fight dataset from raw death entries at the given attendance threshold."""
    settings.min_attendance_pct = attendance
    fight = {
        "id": 10, "name": "Midnight Falls", "encounterID": 9999, "difficulty": 5,
        "kill": False, "startTime": START, "endTime": START + 120_000,
        "friendlyPlayers": [p["id"] for p in ROSTER],
    }
    raw = RawReport(
        code="ABC", title="t", start_time=START, end_time=START + 120_000,
        zone="z", fights=[fight], players=ROSTER,
    )
    raw.deaths_by_fight = {10: {"data": {"entries": entries}}}
    tf = Timeframe(days=7, start_ms=START, end_ms=START + 120_000)
    return build_dataset([raw], tf)


# A guardian dies first, ahead of three real raiders.
DEATHS = [
    {"name": "Akaari's Soul", "deathTime": _at(5),  "ability": {"name": "Splat"}},
    {"name": "Mage",          "deathTime": _at(8),  "ability": {"name": "Fireball"}},
    {"name": "Rogue",         "deathTime": _at(12), "ability": {"name": "Cleave"}},
    {"name": "Warrior",       "deathTime": _at(40), "ability": {"name": "Cleave"}},
    {"name": "Tankadin",      "deathTime": _at(60), "ability": {"name": "Cleave"}},
]


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = settings.min_attendance_pct
    yield
    settings.min_attendance_pct = saved


def test_non_player_deaths_excluded():
    """A guardian death must never appear in the deaths frame, regardless of attendance filtering."""
    ds = _build(DEATHS, attendance=0.0)  # attendance filter off — only the roster filter can drop it
    assert "Akaari's Soul" not in ds.deaths["player"].to_list()


def test_death_order_assigned_over_real_players_only():
    """With the guardian removed before ordering, real players keep contiguous 1..N order."""
    ds = _build(DEATHS, attendance=0.0)
    by_player = {r["player"]: r["death_order"] for r in ds.deaths.to_dicts()}
    assert by_player == {"Mage": 1, "Rogue": 2, "Warrior": 3, "Tankadin": 4}


def test_real_third_death_flagged_early():
    """The real 3rd-to-die raider must fall within the early-death cutoff.

    Before the fix the guardian stole order 1, pushing Warrior (real #3) to
    order 4 and silently dropping them from the early-death set.
    """
    ds = _build(DEATHS, attendance=0.5)
    early = ds.deaths.filter(ds.deaths["death_order"] <= settings.early_death_cutoff)
    assert set(early["player"].to_list()) == {"Mage", "Rogue", "Warrior"}
