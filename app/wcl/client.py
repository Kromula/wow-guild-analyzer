"""Async WarcraftLogs v2 GraphQL client with OAuth2 client-credentials auth.

Handles token acquisition/refresh, retries on transient errors, and respects the
API's point-based rate limiting by backing off on 429s.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from app.config import settings


class WCLError(RuntimeError):
    """Raised when the WarcraftLogs API returns an error or is misconfigured."""


class WCLRateLimited(WCLError):
    """429 from WarcraftLogs. Carries the server's Retry-After (seconds) when given
    so the backoff can wait exactly that long instead of guessing."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _wcl_wait(retry_state) -> float:
    """Backoff that honors a 429's Retry-After when present, otherwise climbs
    exponentially (2, 4, 8, … capped at 30s)."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, WCLRateLimited) and exc.retry_after:
        return min(exc.retry_after, 60.0)
    return min(2.0 * (2 ** max(0, retry_state.attempt_number - 1)), 30.0)


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
        wait=_wcl_wait,
        stop=stop_after_attempt(6),
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
            # Point budget exhausted. Honor Retry-After if the server sent one so
            # the backoff waits exactly long enough rather than guessing.
            ra = resp.headers.get("Retry-After")
            raise WCLRateLimited("Rate limited by WarcraftLogs (429); backing off.",
                                 retry_after=float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        if resp.status_code != 200:
            raise WCLError(f"GraphQL request failed ({resp.status_code}): {resp.text}")

        body = resp.json()
        if "errors" in body and body["errors"]:
            raise WCLError(f"GraphQL errors: {body['errors']}")
        return body["data"]
