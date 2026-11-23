"""Twitch OAuth Device Code Grant flow (public client: no client secret).

Docs: dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow
  * access tokens: ~4 h
  * refresh tokens: one-time use, expire after 30 days of inactivity
  * apps keeping a session must call /validate (hourly)
Tokens are stored in Windows Credential Manager via `keyring`, never in plain files.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass

import httpx

ID = "https://id.twitch.tv/oauth2"
KEYRING_SERVICE = "TwitchClipper"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

SCOPES_CHAT = ["user:read:chat"]
SCOPES_CLIPS = ["clips:edit"]
SCOPES_EDITOR = ["editor:manage:clips"]
SCOPES_BROADCASTER = ["channel:manage:clips"]


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: list[str]

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - 60


class AuthError(Exception):
    pass


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceAuth:
    def __init__(self, client_id: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = client_id
        self.http = httpx.AsyncClient(timeout=20.0, transport=transport)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def start(self, scopes: list[str]) -> DeviceCode:
        r = await self.http.post(f"{ID}/device", data={"client_id": self.client_id, "scopes": " ".join(scopes)})
        if r.status_code != 200:
            raise AuthError(f"device code request failed: {r.status_code} {r.text}")
        d = r.json()
        return DeviceCode(d["device_code"], d["user_code"], d["verification_uri"], int(d["expires_in"]), int(d.get("interval", 5)))

    async def poll(self, code: DeviceCode, scopes: list[str], sleep=asyncio.sleep) -> Token:
        deadline = time.monotonic() + code.expires_in
        interval = code.interval
        while time.monotonic() < deadline:
            r = await self.http.post(f"{ID}/token", data={
                "client_id": self.client_id, "scopes": " ".join(scopes),
                "device_code": code.device_code, "grant_type": DEVICE_GRANT,
            })
            if r.status_code == 200:
                return self._token(r.json())
            msg = (r.json().get("message") or "").lower() if r.content else ""
            if "authorization_pending" in msg:
                await sleep(interval)
                continue
            if "slow_down" in msg:
                interval += 5
                await sleep(interval)
                continue
            raise AuthError(f"device authorization failed: {r.status_code} {r.text}")
        raise AuthError("device code expired before the user approved it")

    async def refresh(self, token: Token) -> Token:
        r = await self.http.post(f"{ID}/token", data={
            "client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": token.refresh_token,
        })
        if r.status_code != 200:
            raise AuthError(f"refresh failed ({r.status_code}): sign in again. {r.text}")
        return self._token(r.json())

    async def validate(self, token: Token) -> dict | None:
        r = await self.http.get(f"{ID}/validate", headers={"Authorization": f"OAuth {token.access_token}"})
        return r.json() if r.status_code == 200 else None

    @staticmethod
    def _token(d: dict) -> Token:
        scopes = d.get("scope") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        return Token(d["access_token"], d.get("refresh_token", ""), time.time() + float(d.get("expires_in", 0)), scopes)


def save_token(client_id: str, token: Token) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, client_id, json.dumps(asdict(token)))


def load_token(client_id: str) -> Token | None:
    """The stored token, or None if there is none or the stored entry is unreadable
    (treated as signed out, so the user can simply sign in again)."""
    import keyring
    raw = keyring.get_password(KEYRING_SERVICE, client_id)
    if not raw:
        return None
    try:
        return Token(**json.loads(raw))
    except (ValueError, TypeError):
        return None


def delete_token(client_id: str) -> None:
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, client_id)
    except keyring.errors.PasswordDeleteError:
        pass


async def ensure_token(client_id: str, scopes: list[str], printer=print) -> Token:
    """Load, refresh, or interactively obtain a token with at least `scopes`."""
    auth = DeviceAuth(client_id)
    try:
        tok = load_token(client_id)
        if tok and set(scopes) <= set(tok.scopes):
            try:
                if tok.expired:
                    tok = await auth.refresh(tok)
                    save_token(client_id, tok)
                if await auth.validate(tok):
                    return tok
            except AuthError:
                # Refresh tokens are one-time use and expire after 30 days unused: sign in again.
                delete_token(client_id)
            printer("Your Twitch sign-in has expired. Please sign in again.")
        code = await auth.start(scopes)
        printer(f"\nOpen {code.verification_uri} and enter code: {code.user_code}\n"
                f"(waiting up to {code.expires_in // 60} minutes...)")
        tok = await auth.poll(code, scopes)
        save_token(client_id, tok)
        return tok
    finally:
        await auth.aclose()
