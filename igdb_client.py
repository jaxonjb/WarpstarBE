"""
igdb_client.py
Async IGDB API client — Twitch OAuth token management and Apicalypse queries.
Uses httpx (already in requirements) to stay fully async.
"""

import os
import time
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_BASE_URL    = "https://api.igdb.com/v4"

_token: str | None   = None
_token_expiry: float = 0.0


async def _get_access_token() -> str:
    """Fetch (or return cached in-memory) Twitch OAuth token for IGDB."""
    global _token, _token_expiry

    if _token and time.time() < _token_expiry - 60:
        return _token

    client_id     = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(TWITCH_TOKEN_URL, params={
            "client_id":     client_id,
            "client_secret": client_secret,
            "grant_type":    "client_credentials",
        })
    resp.raise_for_status()
    data = resp.json()

    _token        = data["access_token"]
    _token_expiry = time.time() + data.get("expires_in", 3600)
    logger.info("Fetched new IGDB access token (expires in %ds)", data.get("expires_in"))
    return _token


async def query(endpoint: str, body: str) -> list[dict]:
    """POST an Apicalypse query to an IGDB endpoint, return JSON list."""
    client_id = os.getenv("TWITCH_CLIENT_ID")
    token     = await _get_access_token()
    headers   = {
        "Client-ID":     client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type":  "text/plain",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{IGDB_BASE_URL}/{endpoint}",
            headers=headers,
            content=body.encode(),
        )
    resp.raise_for_status()
    return resp.json()
