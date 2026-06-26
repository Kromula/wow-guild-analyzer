"""Tests for duplicate same-night log de-duplication (issue #14).

When several raiders log the same night, the reports overlap in time and would
double-count pulls/deaths/attendance. We keep one canonical log per cluster of
overlapping same-zone reports.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.ingest.fetcher import RawReport, Timeframe
from app.ingest.normalize import build_dataset, canonical_report_codes

DAY = 86_400_000
T0 = 1_700_000_000_000
TF = Timeframe(days=0, start_ms=0, end_ms=T0 + DAY)


# ── pure-function tests ───────────────────────────────────────────────────────

def _r(code, zone, start_day, dur_h, n_fights):
    start = T0 + int(start_day * DAY)
    return (code, zone, start, start + int(dur_h * 3_600_000), n_fights)


def test_overlapping_same_zone_keeps_one():
    keep = canonical_report_codes([
        _r("a", "VS", 0, 3, 32),
        _r("b", "VS", 0.05, 3, 30),   # same night, overlaps a, fewer fights
    ])
    assert keep == {"a"}  # more fights wins


def test_different_zones_same_night_both_kept():
    """Two different raids the same evening overlap in time but must NOT be merged."""
    keep = canonical_report_codes([
        _r("vs", "VS / DR / MQD", 0, 1, 24),
        _r("spore", "Sporefall", 0.02, 3, 27),   # overlaps in time, different zone
    ])
    assert keep == {"vs", "spore"}


def test_separate_nights_both_kept():
    keep = canonical_report_codes([
        _r("mon", "VS", 0, 3, 30),
        _r("wed", "VS", 2, 3, 30),    # 2 days later, no overlap
    ])
    assert keep == {"mon", "wed"}


def test_canonical_tiebreak_by_coverage_then_code():
    # equal fights -> longer coverage wins
    keep = canonical_report_codes([
        _r("short", "VS", 0, 2, 32),
        _r("long", "VS", 0.01, 4, 32),
    ])
    assert keep == {"long"}
    # equal fights and coverage -> lexically smallest code (deterministic)
    keep2 = canonical_report_codes([
        _r("zzz", "VS", 0, 3, 32),
        _r("aaa", "VS", 0.01, 3, 32),
    ])
    assert keep2 == {"aaa"}


def test_transitive_overlap_clusters_together():
    """a-b overlap and b-c overlap -> all one cluster, single canonical kept."""
    keep = canonical_report_codes([
        _r("a", "VS", 0, 2, 10),
        _r("b", "VS", 0.05, 2, 40),   # overlaps a, most fights
        _r("c", "VS", 0.10, 2, 20),   # overlaps b
    ])
    assert keep == {"b"}


# ── integration through build_dataset ─────────────────────────────────────────

START = 1_000_000


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = (settings.min_attendance_pct, settings.dedupe_overlapping_logs)
    settings.min_attendance_pct = 0.0
    settings.dedupe_overlapping_logs = True
    yield
    settings.min_attendance_pct, settings.dedupe_overlapping_logs = saved


def _raw(code, start_ms, end_ms, fight_ids, zone="VS / DR / MQD"):
    fights = [{"id": fid, "name": "Voidspire", "encounterID": 9999, "difficulty": 5,
               "kill": False, "startTime": START, "endTime": START + 60_000,
               "friendlyPlayers": [1]} for fid in fight_ids]
    r = RawReport(code=code, title=code, start_time=start_ms, end_time=end_ms,
                  zone=zone, fights=fights, players=[{"id": 1, "name": "Mage"}])
    return r


def test_build_dataset_dedupes_overlapping_logs():
    # Same night logged twice; report A has more fights so it's canonical.
    a = _raw("A", T0, T0 + 3 * 3_600_000, [1, 2, 3])
    b = _raw("B", T0 + 60_000, T0 + 3 * 3_600_000, [10, 11])  # overlaps A
    ds = build_dataset([a, b], TF)
    assert ds.fights.height == 3                       # A's fights only, not 5
    assert [r["code"] for r in ds.reports] == ["A"]


def test_toggle_off_keeps_duplicates():
    settings.dedupe_overlapping_logs = False
    a = _raw("A", T0, T0 + 3 * 3_600_000, [1, 2, 3])
    b = _raw("B", T0 + 60_000, T0 + 3 * 3_600_000, [10, 11])
    ds = build_dataset([a, b], TF)
    assert ds.fights.height == 5                       # both counted
    assert {r["code"] for r in ds.reports} == {"A", "B"}
