"""Claude Code subscription OAuth credential, with auto-refresh.

Lets the plugin run with **no API key**: if the user is logged into a Claude
subscription, we reuse that credential to talk to the Anthropic API (Bearer
token + the oauth beta header). The short-lived access token is refreshed via the
refresh token when it nears expiry.

CAVEATS (documented, opt-out-able):
  - This reuses a *subscription* credential for a separate service — a ToS gray
    area; it draws on the subscription's rate-limit budget (NOT $-metered).
  - The token endpoint + client_id below are Claude Code's public OAuth values;
    if Anthropic changes them, refresh fails and we fall back to the on-disk
    token (and finally a clear "re-login with `claude`" error).
  - We persist refreshed tokens to the plugin's OWN data dir, never writing back
    to Claude Code's `~/.claude/.credentials.json` (avoids corrupting its auth).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
from .constants import OAUTH_REFRESH_MARGIN_MS as REFRESH_MARGIN_MS


def _credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def subscription_present() -> bool:
    try:
        d = json.loads(_credentials_path().read_text())
        return bool(d.get("claudeAiOauth", {}).get("accessToken"))
    except Exception:  # noqa: BLE001
        return False


class OAuthCredential:
    """Holds + refreshes the subscription OAuth token. `await token()` always
    returns a currently-valid access token."""

    def __init__(self, state_path: Path):
        self._state_path = state_path  # plugin data dir, NOT Claude Code's file
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: int = 0
        self._load()

    def _load(self) -> None:
        # Prefer our own persisted (possibly refreshed) state, else Claude Code's.
        for p in (self._state_path, _credentials_path()):
            try:
                raw = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            o = raw.get("claudeAiOauth", raw)  # our file stores the inner object
            at = o.get("accessToken")
            if at:
                # take the one with the later expiry
                exp = int(o.get("expiresAt", 0))
                if at and exp >= self.expires_at:
                    self.access_token = at
                    self.refresh_token = o.get("refreshToken")
                    self.expires_at = exp

    def _persist(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at,
            }))
            tmp.replace(self._state_path)
        except OSError:
            pass

    async def _refresh(self) -> None:
        if not self.refresh_token:
            raise RuntimeError("Live Memory: no refresh token; log in with `claude` or set an API key.")
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(TOKEN_ENDPOINT, json={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": CLAUDE_CODE_CLIENT_ID,
            })
            r.raise_for_status()
            d = r.json()
        self.access_token = d["access_token"]
        self.refresh_token = d.get("refresh_token", self.refresh_token)
        self.expires_at = int(time.time() * 1000) + int(d.get("expires_in", 3600)) * 1000
        self._persist()

    async def token(self) -> str:
        now_ms = int(time.time() * 1000)
        if not self.access_token or now_ms >= self.expires_at - REFRESH_MARGIN_MS:
            # reload first (Claude Code may have refreshed it for us), then refresh
            self._load()
            if not self.access_token or now_ms >= self.expires_at - REFRESH_MARGIN_MS:
                await self._refresh()
        assert self.access_token
        return self.access_token
