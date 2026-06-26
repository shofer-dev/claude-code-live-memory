"""OAuth credential: valid-token reuse, refresh-on-expiry, HTTP refresh parsing
+ persistence — all with mocked credentials path and mocked httpx (no network)."""
from __future__ import annotations

import json
import time

import pytest

from live_memory import oauth
from live_memory.oauth import OAuthCredential


def _write_creds(path, access, refresh, expires_at):
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": refresh, "expiresAt": expires_at}}))


def test_subscription_present(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    monkeypatch.setattr(oauth, "_credentials_path", lambda: creds)
    assert oauth.subscription_present() is False
    _write_creds(creds, "AT", "RT", 0)
    assert oauth.subscription_present() is True


@pytest.mark.asyncio
async def test_valid_token_reused_without_refresh(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    _write_creds(creds, "AT-valid", "RT", int(time.time() * 1000) + 3_600_000)
    monkeypatch.setattr(oauth, "_credentials_path", lambda: creds)
    cred = OAuthCredential(tmp_path / "state.json")
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1

    monkeypatch.setattr(cred, "_refresh", boom)
    assert await cred.token() == "AT-valid"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_refreshes_when_expired(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    _write_creds(creds, "AT-old", "RT", int(time.time() * 1000) - 1000)
    monkeypatch.setattr(oauth, "_credentials_path", lambda: creds)
    cred = OAuthCredential(tmp_path / "state.json")

    async def fake_refresh():
        cred.access_token = "AT-new"
        cred.expires_at = int(time.time() * 1000) + 3_600_000

    monkeypatch.setattr(cred, "_refresh", fake_refresh)
    assert await cred.token() == "AT-new"


@pytest.mark.asyncio
async def test_http_refresh_parses_and_persists(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    _write_creds(creds, "AT-old", "RT-old", int(time.time() * 1000) - 1000)
    monkeypatch.setattr(oauth, "_credentials_path", lambda: creds)
    state = tmp_path / "state.json"
    cred = OAuthCredential(state)

    class FakeResp:
        def raise_for_status(self): ...
        def json(self):
            return {"access_token": "AT-fresh", "refresh_token": "RT-fresh", "expires_in": 3600}

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json): return FakeResp()

    monkeypatch.setattr(oauth.httpx, "AsyncClient", FakeClient)
    assert await cred.token() == "AT-fresh"
    saved = json.loads(state.read_text())  # persisted to OUR file, not Claude Code's
    assert saved["accessToken"] == "AT-fresh" and saved["refreshToken"] == "RT-fresh"
