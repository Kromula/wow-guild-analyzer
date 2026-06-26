"""Regression tests for the current-tier scope filter (issue #12, unblocks #11).

`_list_reports` should restrict the guild's reports to the current raid tier so
the "All" timeframe stops fanning out into older tiers (which is what stalls the
WCL fetch). Reports come back newest-first; the current tier is the leading
contiguous block of one zone.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.ingest.fetcher import Timeframe, _list_reports


def _report(code: str, zone_id: int, zone_name: str = "Raid") -> dict:
    return {"code": code, "title": code, "startTime": 0, "endTime": 1,
            "zone": {"id": zone_id, "name": zone_name}}


class FakeClient:
    """Serves canned GUILD_REPORTS pages, paginated by the `page` variable."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.calls = 0

    async def query(self, query: str, variables: dict) -> dict:
        self.calls += 1
        page = variables["page"]
        idx = page - 1
        data = self.pages[idx] if idx < len(self.pages) else []
        return {"reportData": {"reports": {
            "total": sum(len(p) for p in self.pages),
            "current_page": page,
            "has_more_pages": idx < len(self.pages) - 1,
            "data": data,
        }}}


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = (settings.current_tier_only, settings.current_tier_zone_id,
             settings.exclude_mythic_plus, settings.max_reports, settings.max_reports_all_time)
    yield
    (settings.current_tier_only, settings.current_tier_zone_id,
     settings.exclude_mythic_plus, settings.max_reports, settings.max_reports_all_time) = saved


def _run(client) -> list[dict]:
    tf = Timeframe(days=7, start_ms=0, end_ms=1)
    return asyncio.run(_list_reports(client, tf))


def test_autodetect_current_tier_excludes_older_tiers():
    settings.current_tier_only = True
    settings.current_tier_zone_id = 0  # auto-detect from newest report
    settings.exclude_mythic_plus = False
    # Newest-first: two current-tier (zone 100), then an older tier (zone 99).
    client = FakeClient([[_report("a", 100), _report("b", 100), _report("c", 99), _report("d", 99)]])
    out = _run(client)
    assert [r["code"] for r in out] == ["a", "b"]


def test_stops_paging_once_past_the_tier():
    settings.current_tier_only = True
    settings.current_tier_zone_id = 0
    settings.exclude_mythic_plus = False
    # Page 1 is all current tier; page 2 starts the old tier. We should stop after
    # seeing the first off-tier report and never request page 3.
    client = FakeClient([
        [_report("a", 100), _report("b", 100)],
        [_report("c", 99), _report("d", 99)],
        [_report("e", 99)],
    ])
    out = _run(client)
    assert [r["code"] for r in out] == ["a", "b"]
    assert client.calls == 2  # page 3 never fetched


def test_explicit_zone_id_pins_tier():
    settings.current_tier_only = True
    settings.current_tier_zone_id = 99  # pin the older tier explicitly
    settings.exclude_mythic_plus = False
    client = FakeClient([[_report("a", 100), _report("b", 99), _report("c", 100), _report("d", 99)]])
    out = _run(client)
    # Pinned to zone 99: "a" (zone 100) is a mismatch and, being newest-first, ends the scan.
    assert [r["code"] for r in out] == []


def test_disabled_keeps_all_tiers():
    settings.current_tier_only = False
    settings.exclude_mythic_plus = False
    client = FakeClient([[_report("a", 100), _report("b", 100), _report("c", 99)]])
    out = _run(client)
    assert [r["code"] for r in out] == ["a", "b", "c"]
