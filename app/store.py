"""On-disk cache of normalized per-report data (issue #13).

A WCL report is immutable once the raid night ends, so its parsed `ReportFrames`
can be cached and reused instead of re-fetched. Storage is one directory per
report `code`: a `meta.json` (scalars + attendance bits) plus one Parquet file
per frame. Reads reconstruct the `ReportFrames`; the cross-report `assemble()`
step then runs over the loaded set with zero API calls.

Per-encounter frames (for the boss drill-down) live under `enc/<encounter_id>/`
inside the report dir — the same frame layout, scoped to one boss. They're an
additive cache: `report_meta`'s `encounters` list is the source of truth for
which ones are present.

Aggregate writes are atomic (stage in a sibling `.tmp`, then rename). Encounter
frames are attached incrementally; the parent's `encounters` list is updated
last, so an interrupted attach just leaves unlisted (ignored) dirs.
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
SCHEMA_VERSION = 3  # v3: fights frame gained fight_percentage

# ReportFrames attributes persisted as Parquet, one file each.
_FRAME_FILES = ("players", "role_rows", "fights", "damage", "healing",
                "casts", "deaths", "damage_taken")


def _root() -> Path:
    return Path(settings.data_dir) / "reports"


def _dir(code: str) -> Path:
    return _root() / code


def _write_frames(d: Path, rf: ReportFrames) -> None:
    for attr in _FRAME_FILES:
        getattr(rf, attr).write_parquet(d / f"{attr}.parquet")


def _read_frames(d: Path) -> dict | None:
    frames = {}
    for attr in _FRAME_FILES:
        fp = d / f"{attr}.parquet"
        if not fp.exists():
            return None
        frames[attr] = pl.read_parquet(fp)
    return frames


def _write_encounter(ed: Path, erf: ReportFrames) -> None:
    if ed.exists():
        shutil.rmtree(ed)
    ed.mkdir(parents=True, exist_ok=True)
    _write_frames(ed, erf)
    (ed / "meta.json").write_text(
        json.dumps({"present": erf.present, "is_raid_night": erf.is_raid_night}), encoding="utf-8")


def store_report(rf: ReportFrames, *, fetched_at: float,
                 encounters: dict[int, ReportFrames] | None = None) -> None:
    """Persist one report's aggregate frames + metadata (and any per-encounter
    frames), atomically."""
    encounters = encounters or {}
    d = _dir(rf.code)
    tmp = d.with_name(d.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        _write_frames(tmp, rf)
        for enc, erf in encounters.items():
            _write_encounter(tmp / "enc" / str(enc), erf)
        meta = {
            "code": rf.code, "title": rf.title, "zone": rf.zone,
            "start_time": rf.start_time, "end_time": rf.end_time,
            "is_raid_night": rf.is_raid_night, "present": rf.present,
            "fetched_at": fetched_at, "schema_version": SCHEMA_VERSION,
            "encounters": sorted(encounters),
        }
        (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if d.exists():
            shutil.rmtree(d)
        tmp.rename(d)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def attach_encounters(code: str, encounters: dict[int, ReportFrames]) -> None:
    """Add per-encounter frames to an already-stored report (additive cache).

    The parent's `encounters` list is updated last, so an interrupted call just
    leaves unlisted dirs that reads ignore."""
    meta = report_meta(code)
    if meta is None or not encounters:
        return
    d = _dir(code)
    for enc, erf in encounters.items():
        _write_encounter(d / "enc" / str(enc), erf)
    meta["encounters"] = sorted(set(meta.get("encounters", [])) | set(encounters))
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


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


def _frames_obj(scalars: dict, frames: dict) -> ReportFrames:
    return ReportFrames(
        code=scalars["code"], title=scalars["title"], zone=scalars["zone"],
        start_time=scalars["start_time"], end_time=scalars["end_time"],
        is_raid_night=scalars["is_raid_night"], present=list(scalars["present"]),
        **frames,
    )


def load_report(code: str) -> ReportFrames | None:
    """Reconstruct a stored report's aggregate ReportFrames, or None if incomplete."""
    meta = report_meta(code)
    if meta is None:
        return None
    frames = _read_frames(_dir(code))
    return _frames_obj(meta, frames) if frames is not None else None


def load_reports(codes) -> list[ReportFrames]:
    """Load several reports by code, skipping any that aren't fully stored."""
    return [rf for c in codes if (rf := load_report(c)) is not None]


def load_encounter(code: str, encounter_id: int) -> ReportFrames | None:
    """Reconstruct one report's per-encounter ReportFrames, or None if not cached."""
    meta = report_meta(code)
    if meta is None or encounter_id not in meta.get("encounters", []):
        return None
    ed = _dir(code) / "enc" / str(encounter_id)
    frames = _read_frames(ed)
    if frames is None:
        return None
    try:
        enc_scalars = json.loads((ed / "meta.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # Per-encounter present/is_raid_night override the report-level scalars.
    return _frames_obj({**meta, **enc_scalars}, frames)


def load_encounter_frames(encounter_id: int) -> list[ReportFrames]:
    """Per-encounter frames for an encounter across every stored report."""
    return [rf for c in stored_codes()
            if (rf := load_encounter(c, encounter_id)) is not None]


def encounter_is_cached(encounter_id: int) -> bool:
    """Whether any stored report has per-encounter frames for this encounter."""
    return any(encounter_id in (report_meta(c) or {}).get("encounters", [])
               for c in stored_codes())
