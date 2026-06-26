"""Regression tests for the current-tier scope filter (issue #12, unblocks #11).

`_list_reports` restricts the guild's reports to the current raid tier so the
"All" timeframe stops fanning out into older tiers (which is what stalls the WCL
fetch). A tier can span MULTIPLE raids (e.g. a main raid plus a later mini-raid),
so the scope is a set of zones: every raid zone logged within the recent active
window, plus older reports of those same zones. Reports come back newest-first.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.ingest.fetcher import Timeframe, _list_reports

DAY = 86_400_000
T0 = 1_700_000_000_000  # newest report time (ms)


def _report(code: str, zone_id: int, age_days: float = 0, zone_name: str = "Raid") -> dict:
    return {"code": code, "title": code,
            "startTime": T0 - int(age_days * DAY), "endTime": T0 - int(age_days * DAY) + 1,
            "zone": {"id": zone_id, "name": zone_name}}


class FakeClient:
    """Serves canned GUILD_REPORTS pages, paginated by the `page` variable."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.calls = 0

    async def query(self, query: str, variables: dict) -> dict:
        self.calls += 1
        idx = variables["page"] - 1
        data = self.pages[idx] if idx < len(self.pages) else []
        return {"reportData": {"reports": {
            "total": sum(len(p) for p in self.pages),
            "current_page": variables["page"],
            "has_more_pages": idx < len(self.pages) - 1,
            "data": data,
        }}}


@pytest.fixture(autouse=True)
def _restore_settings():
    keys = ("current_tier_only", "current_tier_zone_ids", "current_tier_active_days",
            "exclude_mythic_plus", "max_reports", "max_reports_all_time")
    saved = {k: getattr(settings, k) for k in keys}
    # Sensible defaults for these tests; individual tests override as needed.
    settings.current_tier_only = True
    settings.current_tier_zone_ids = ()
    settings.current_tier_active_days = 30
    settings.exclude_mythic_plus = False
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


def _run(client) -> list[str]:
    tf = Timeframe(days=0, start_ms=0, end_ms=T0)  # all-time
    return [r["code"] for r in asyncio.run(_list_reports(client, tf))]


def test_multiple_raids_in_tier_all_included():
    """Two raids raided in the same recent window both count as the current tier."""
    client = FakeClient([[
        _report("spore1", 100, age_days=0),    # Sporefall (newest)
        _report("void1", 101, age_days=3),     # Voidspire
        _report("spore2", 100, age_days=7),
        _report("void2", 101, age_days=10),
        _report("oldtier", 99, age_days=200),  # previous tier — excluded
    ]])
    assert _run(client) == ["spore1", "void1", "spore2", "void2"]


def test_early_tier_reports_beyond_active_window_kept():
    """A current-tier zone's older reports (before the active window) are still kept."""
    client = FakeClient([[
        _report("recent", 100, age_days=2),    # seeds zone 100 as current tier
        _report("early", 100, age_days=90),    # same zone, older than the 30d window
        _report("oldtier", 99, age_days=300),  # different zone, older — excluded
    ]])
    assert _run(client) == ["recent", "early"]


def test_stops_paging_once_past_the_tier():
    """After collecting current-tier reports, a full off-tier page ends the scan."""
    client = FakeClient([
        [_report("a", 100, age_days=0), _report("b", 100, age_days=5)],
        [_report("old1", 99, age_days=120), _report("old2", 99, age_days=125)],
        [_report("old3", 99, age_days=130)],
    ])
    tf = Timeframe(days=0, start_ms=0, end_ms=T0)
    out = asyncio.run(_list_reports(client, tf))
    assert [r["code"] for r in out] == ["a", "b"]
    assert client.calls == 2  # page 3 never fetched


def test_explicit_zone_ids_pin_the_tier():
    """An explicit allowlist includes exactly those zones, ignoring the active window."""
    settings.current_tier_zone_ids = (100, 101)
    client = FakeClient([[
        _report("a", 100, age_days=0),
        _report("b", 101, age_days=5),
        _report("c", 99, age_days=10),    # not in allowlist — skipped
        _report("d", 100, age_days=12),
    ]])
    assert _run(client) == ["a", "b", "d"]


def test_disabled_keeps_all_tiers():
    settings.current_tier_only = False
    client = FakeClient([[
        _report("a", 100, age_days=0),
        _report("b", 100, age_days=5),
        _report("c", 99, age_days=400),
    ]])
    assert _run(client) == ["a", "b", "c"]
