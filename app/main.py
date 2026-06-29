"""FastAPI entrypoint. Serves the JSON API and the dashboard.

Run with:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import router as api_router
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_WEB = Path(__file__).parent / "web"

app = FastAPI(title="WoW Guild Analyzer", version="0.1.0")


@app.middleware("http")
async def _no_browser_cache(request: Request, call_next):
    """Tell the browser never to cache responses. This is a local single-user dev
    tool, so the freshness win (a normal refresh always shows the latest UI after a
    code change — no hard-refresh needed) is worth more than caching."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


app.include_router(api_router)
app.mount("/static", StaticFiles(directory=_WEB / "static"), name="static")
templates = Jinja2Templates(directory=str(_WEB / "templates"))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "guild": settings.guild_name or "Your Guild",
         "default_days": settings.default_timeframe_days},
    )
