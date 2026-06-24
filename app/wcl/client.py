"""Async WarcraftLogs v2 GraphQL client with OAuth2 client-credentials auth.

Handles token acquisition/refresh, retries on transient errors, and respects the
API's point-based rate limiting by backing off on 429s.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings


class WCLError(RuntimeError):
    """Raised when the WarcraftLogs API returns an error or is misconfigured."""


class WCLClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _ensure_token(self) -> str:
        # Refresh slightly before expiry to avoid edge-of-window failures.
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        if not (settings.wcl_client_id and settings.wcl_client_secret):
            raise WCLError(
                "WCL credentials missing. Set WCL_CLIENT_ID and WCL_CLIENT_SECRET in your .env file. "
                "See README.md for how to create an API client."
            )

        resp = await self._client.post(
            settings.wcl_token_url,
            data={"grant_type": "client_credentials"},
            auth=(settings.wcl_client_id, settings.wcl_client_secret),
        )
        if resp.status_code != 200:
            raise WCLError(f"OAuth token request failed ({resp.status_code}): {resp.text}")

        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + payload.get("expires_in", 3600)
        return self._token

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, WCLError)),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        token = await self._ensure_token()
        resp = await self._client.post(
            settings.wcl_api_url,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {token}"},
        )

        if resp.status_code == 401:
            # Token went stale — force a refresh and let tenacity retry.
            self._token = None
            raise WCLError("Unauthorized; refreshing token.")
        if resp.status_code == 429:
            raise WCLError("Rate limited by WarcraftLogs (429); backing off.")
        if resp.status_code != 200:
            raise WCLError(f"GraphQL request failed ({resp.status_code}): {resp.text}")

        body = resp.json()
        if "errors" in body and body["errors"]:
            raise WCLError(f"GraphQL errors: {body['errors']}")
        return body["data"]
