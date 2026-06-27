"""API tests for the manual log-sync endpoint and status (issue #13 slice 6)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import service
from app.config import settings
from app.main import app
from app.wcl import WCLRateLimited

client = TestClient(app)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    # The sync endpoint requires configured credentials.
    for field in ("wcl_client_id", "wcl_client_secret", "guild_name", "guild_server_slug"):
        monkeypatch.setattr(settings, field, "x")
    yield


def test_status_includes_sync(monkeypatch):
    monkeypatch.setattr(service, "sync_status", lambda: {"last_synced": 123.0, "stored_reports": 4})
    body = client.get("/api/status").json()
    assert body["sync"] == {"last_synced": 123.0, "stored_reports": 4}


def test_update_logs_success(monkeypatch):
    async def fake_sync(*, force=False):
        return {"fetched": 2, "skipped": 50, "stored_total": 52, "last_synced": 1.0}
    monkeypatch.setattr(service, "sync_logs", fake_sync)
    res = client.post("/api/update-logs")
    assert res.status_code == 200
    assert res.json()["fetched"] == 2


def test_update_logs_rate_limited_returns_429(monkeypatch):
    async def boom(*, force=False):
        raise WCLRateLimited("rate limited", retry_after=30)
    monkeypatch.setattr(service, "sync_logs", boom)
    res = client.post("/api/update-logs")
    assert res.status_code == 429
    assert "rate limit" in res.json()["detail"].lower()


def test_update_logs_requires_configuration(monkeypatch):
    for field in ("wcl_client_id", "wcl_client_secret", "guild_name", "guild_server_slug"):
        monkeypatch.setattr(settings, field, "")
    res = client.post("/api/update-logs")
    assert res.status_code == 409
