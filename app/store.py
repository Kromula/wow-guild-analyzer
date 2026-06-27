"""On-disk cache of normalized per-report data (issue #13).

A WCL report is immutable once the raid night ends, so its parsed `ReportFrames`
can be cached and reused instead of re-fetched. Storage is one directory per
report `code`: a `meta.json` (scalars + attendance bits) plus one Parquet file
per frame. Reads reconstruct the `ReportFrames`; the cross-report `assemble()`
step then runs over the loaded set with zero API calls.

Writes are atomic: a report is staged in a sibling `.tmp` directory and renamed
into place, so a crash mid-write can't leave a half-written report that later
reads as valid.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import polars as pl

from app.config import settings
from app.ingest.normalize import ReportFrames

# Bump to invalidate every stored report when the normalization output changes
# (new columns, different parsing). Mismatched reports read back as "not stored"
# and get re-fetched on the next sync.
SCHEMA_VERSION = 2  # v2: fights frame gained encounter_id

# ReportFrames attributes persisted as Parquet, one file each.
_FRAME_FILES = ("players", "role_rows", "fights", "damage", "healing",
                "casts", "deaths", "damage_taken")


def _root() -> Path:
    return Path(settings.data_dir) / "reports"


def _dir(code: str) -> Path:
    return _root() / code


def store_report(rf: ReportFrames, *, fetched_at: float) -> None:
    """Persist one report's frames + metadata, atomically."""
    d = _dir(rf.code)
    tmp = d.with_name(d.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for attr in _FRAME_FILES:
            getattr(rf, attr).write_parquet(tmp / f"{attr}.parquet")
        meta = {
            "code": rf.code, "title": rf.title, "zone": rf.zone,
            "start_time": rf.start_time, "end_time": rf.end_time,
            "is_raid_night": rf.is_raid_night, "present": rf.present,
            "fetched_at": fetched_at, "schema_version": SCHEMA_VERSION,
        }
        (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if d.exists():
            shutil.rmtree(d)
        tmp.rename(d)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def report_meta(code: str) -> dict | None:
    """Stored metadata for a report, or None if absent / unreadable / stale schema."""
    p = _dir(code) / "meta.json"
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if meta.get("schema_version") != SCHEMA_VERSION:
        return None
    return meta


def stored_codes() -> set[str]:
    """Report codes with valid, current-schema stored data.

    A report whose schema_version is stale is treated as absent so sync
    re-fetches it."""
    root = _root()
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir()
            if p.is_dir() and not p.name.endswith(".tmp") and report_meta(p.name) is not None}


def all_meta() -> list[dict]:
    """Metadata for every stored report (for sync diffing / read-path scoping)."""
    return [m for c in stored_codes() if (m := report_meta(c)) is not None]


def load_report(code: str) -> ReportFrames | None:
    """Reconstruct a stored report's ReportFrames, or None if not fully present."""
    meta = report_meta(code)
    if meta is None:
        return None
    d = _dir(code)
    frames = {}
    for attr in _FRAME_FILES:
        fp = d / f"{attr}.parquet"
        if not fp.exists():
            return None
        frames[attr] = pl.read_parquet(fp)
    return ReportFrames(
        code=meta["code"], title=meta["title"], zone=meta["zone"],
        start_time=meta["start_time"], end_time=meta["end_time"],
        is_raid_night=meta["is_raid_night"], present=list(meta["present"]),
        **frames,
    )


def load_reports(codes) -> list[ReportFrames]:
    """Load several reports by code, skipping any that aren't fully stored."""
    out = []
    for c in codes:
        rf = load_report(c)
        if rf is not None:
            out.append(rf)
    return out
