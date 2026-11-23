"""Official Twitch API (Helix), using an app access token from the user's own Twitch application.

Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (see docs/credentials.md). The token is cached in the OS keychain
when one is available and is never printed.
"""
import json, re, time
from datetime import datetime, timedelta, timezone

import requests

import settings

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
TOKEN_KEY = "twitch-app-token"
TIMEOUT = 20
_token = None


class TwitchError(SystemExit):
    """A Twitch problem, shown as a plain message that never contains credentials."""


def _credentials():
    cid, secret = settings.secret("TWITCH_CLIENT_ID"), settings.secret("TWITCH_CLIENT_SECRET")
    if not cid or not secret:
        raise TwitchError("Twitch isn't configured: set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (see docs/credentials.md).")
    return cid, secret


def _new_token(cid, secret):
    global _token
    try:
        r = requests.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
                          timeout=TIMEOUT)
    except requests.RequestException as e:
        raise TwitchError(f"Couldn't reach Twitch to sign in ({type(e).__name__}).") from None
    if r.status_code != 200:
        raise TwitchError(f"Twitch rejected your app credentials (HTTP {r.status_code}). Check TWITCH_CLIENT_ID and "
                          "TWITCH_CLIENT_SECRET.")
    data = r.json()
    _token = {"client_id": cid, "access_token": data["access_token"],
              "expires_at": time.time() + float(data.get("expires_in", 3600)) - 300}
    settings.keychain_set(TOKEN_KEY, json.dumps(_token))
    return _token["access_token"]


def _access_token(cid, secret):
    global _token
    if _token is None:
        try:
            _token = json.loads(settings.keychain_get(TOKEN_KEY) or "null")
        except ValueError:
            _token = None
    if _token and _token.get("client_id") == cid and _token.get("expires_at", 0) > time.time():
        return _token["access_token"]
    return _new_token(cid, secret)


def forget_token():
    global _token
    _token = None
    settings.keychain_delete(TOKEN_KEY)


def get(path, params):
    """GET a Helix endpoint; refreshes an expired token once and waits out rate limits and brief outages."""
    cid, secret = _credentials()
    refreshed = False
    for attempt in range(4):
        headers = {"Client-Id": cid, "Authorization": f"Bearer {_access_token(cid, secret)}"}
        try:
            r = requests.get(f"{HELIX}/{path}", params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt == 3:
                raise TwitchError(f"Couldn't reach the Twitch API ({type(e).__name__}).") from None
            time.sleep(1 + attempt)
            continue
        if r.status_code == 401 and not refreshed:
            refreshed = True
            forget_token()
            continue
        if r.status_code == 429 and attempt < 3:
            reset = float(r.headers.get("Ratelimit-Reset") or 0)
            time.sleep(min(60.0, max(1.0, reset - time.time())))
            continue
        if r.status_code >= 500 and attempt < 3:
            time.sleep(1 + attempt)
            continue
        if r.status_code != 200:
            raise TwitchError(f"Twitch API error on {path}: HTTP {r.status_code}.")
        return r.json()
    raise TwitchError(f"Twitch API error on {path}: too many retries.")


def _paged(path, params, limit):
    out, cursor = [], None
    while len(out) < limit:
        query = {**params, "first": min(100, limit - len(out))}
        if cursor:
            query["after"] = cursor
        page = get(path, query)
        out += page.get("data", [])
        cursor = (page.get("pagination") or {}).get("cursor")
        if not cursor or not page.get("data"):
            break
    return out[:limit]


def _chunks(values, size=100):
    values = list(values)
    return [values[i:i + size] for i in range(0, len(values), size)]


def users(logins):
    return [u for chunk in _chunks(logins) for u in get("users", {"login": chunk}).get("data", [])]


def user_id(login):
    found = users([login])
    if not found:
        raise TwitchError(f"No Twitch channel named '{login}'.")
    return found[0]["id"]


def streams(logins):
    return [s for chunk in _chunks(logins) for s in get("streams", {"user_login": chunk, "first": 100}).get("data", [])]


def games(ids):
    ids = sorted({i for i in ids if i})
    return {g["id"]: g["name"] for chunk in _chunks(ids) for g in get("games", {"id": chunk}).get("data", [])}


def clips(login, days=7, limit=25):
    """A channel's most-viewed clips from the last `days` days (None = all time)."""
    params = {"broadcaster_id": user_id(login)}
    if days is not None:
        now = datetime.now(timezone.utc)
        params["started_at"] = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["ended_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    found = _paged("clips", params, limit)
    return sorted(found, key=lambda c: c["view_count"], reverse=True)


def parse_duration(text) -> int:
    """Twitch video durations look like 1h0m5s or 3m21s."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text or "")
    if not m or not text:
        raise TwitchError(f"Unexpected video duration from Twitch: {text!r}")
    h, mins, s = (int(v or 0) for v in m.groups())
    return h * 3600 + mins * 60 + s


def videos(login, limit=20):
    """A channel's past broadcasts, newest first. Helix doesn't report a video's game, so `game` is None."""
    found = _paged("videos", {"user_id": user_id(login), "type": "archive", "sort": "time"}, limit)
    return [{"id": v["id"], "title": v["title"], "createdAt": v["created_at"], "lengthSeconds": parse_duration(v["duration"]),
             "game": None} for v in found]
