"""JSON API consumed by the dashboard."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app import service
from app.config import settings
from app.wcl import WCLError, WCLRateLimited

router = APIRouter(prefix="/api")


@router.get("/status")
async def status() -> dict:
    return {
        "configured": settings.configured,
        "guild": {
            "name": settings.guild_name,
            "server": settings.guild_server_slug,
            "region": settings.guild_region,
        },
        "default_timeframe_days": settings.default_timeframe_days,
        "sync": service.sync_status(),
    }


@router.post("/update-logs")
async def update_logs(force: bool = Query(default=False)) -> dict:
    """Manual "Update Logs": pull new/grown current-tier reports into the store."""
    if not settings.configured:
        raise HTTPException(status_code=409, detail="App is not configured. See README / .env.example.")
    try:
        return await service.sync_logs(force=force)
    except WCLRateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail="WarcraftLogs rate limit reached. Wait a minute and try Update Logs again.",
        ) from exc
    except WCLError as exc:
        raise HTTPException(status_code=502, detail=f"WarcraftLogs error: {exc}") from exc


@router.get("/checks")
async def checks() -> dict:
    return {"checks": service.available_checks()}


@router.get("/bosses")
async def bosses(days: int = Query(default=14, ge=0, le=180), force: bool = Query(default=False)) -> dict:
    if not settings.configured:
        raise HTTPException(status_code=409, detail="App is not configured. See README / .env.example.")
    try:
        return await service.list_bosses(days, force=force)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Boss discovery failed: {exc}") from exc


@router.get("/boss")
async def boss(
    encounter_id: int = Query(..., ge=1),
    days: int = Query(default=14, ge=0, le=180),  # 0 = all time
    force: bool = Query(default=False),
) -> dict:
    if not settings.configured:
        raise HTTPException(status_code=409, detail="App is not configured. See README / .env.example.")
    try:
        return await service.boss_panel(days, encounter_id, force=force)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Boss analysis failed: {exc}") from exc


@router.get("/analyze")
async def analyze(
    days: int = Query(default=14, ge=0, le=180),  # 0 = all time
    only: str | None = Query(default=None, description="comma-separated check ids"),
    force: bool = Query(default=False),
) -> dict:
    if not settings.configured:
        raise HTTPException(status_code=409, detail="App is not configured. See README / .env.example.")
    only_ids = [s.strip() for s in only.split(",")] if only else None
    try:
        return await service.analyze(days, only=only_ids, force=force)
    except Exception as exc:  # surface a clean error to the dashboard
        raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}") from exc
