"""Minimal client for documented Twitch Helix endpoints used in Phase 0."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

API = "https://api.twitch.tv/helix"


class HelixError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after


class VideoStatus(str, Enum):
    AVAILABLE = "available"
    # Not returned to this account. Could be deleted, expired, made private,
    # subscriber-only, or otherwise restricted - Twitch does not say which.
    UNAVAILABLE_OR_INACCESSIBLE = "unavailable_or_inaccessible"
    FORBIDDEN = "inaccessible_forbidden"
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    CHECK_FAILED = "check_failed"


STATUS_MESSAGES = {
    VideoStatus.AVAILABLE: "Available.",
    VideoStatus.UNAVAILABLE_OR_INACCESSIBLE: (
        "Unavailable or inaccessible. Twitch did not return this video to your account. "
        "It may have expired or been deleted, or it may be private or restricted."
    ),
    VideoStatus.FORBIDDEN: "Inaccessible: Twitch refused access to this video for your account.",
    VideoStatus.AUTH_REQUIRED: "Your Twitch login has expired or lacks permission. Sign in again.",
    VideoStatus.RATE_LIMITED: "Twitch rate limit reached. The check will be retried automatically.",
    VideoStatus.CHECK_FAILED: "Couldn't check availability (network or Twitch error). Try again later.",
}


@dataclass
class VideoCheck:
    status: VideoStatus
    video: dict | None = None
    http_status: int | None = None
    detail: str = ""

    @property
    def message(self) -> str:
        return STATUS_MESSAGES[self.status]


class Helix:
    def __init__(self, client_id: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(
            base_url=API,
            headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
            timeout=20.0,
            transport=transport,
        )
        self.ratelimit_remaining: int | None = None
        self.ratelimit_reset: float | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, *, params: Any = None, json: Any = None, retries: int = 3) -> dict:
        for attempt in range(retries + 1):
            try:
                r = await self.client.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise HelixError(0, f"network error: {exc!r}") from exc
                await asyncio.sleep(2 ** attempt)
                continue
            rem = r.headers.get("Ratelimit-Remaining")
            reset = r.headers.get("Ratelimit-Reset")
            self.ratelimit_remaining = int(rem) if rem and rem.isdigit() else self.ratelimit_remaining
            self.ratelimit_reset = float(reset) if reset else self.ratelimit_reset
            if r.status_code == 429:
                wait = max(1.0, (self.ratelimit_reset or time.time() + 5) - time.time())
                if attempt == retries:
                    raise HelixError(429, "rate limited", wait)
                await asyncio.sleep(min(wait, 60))
                continue
            if r.status_code >= 500 and attempt < retries:
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("message", r.text)
                except ValueError:
                    msg = r.text
                raise HelixError(r.status_code, msg)
            return r.json() if r.content else {}
        raise HelixError(0, "unreachable")

    # --- users / streams ---------------------------------------------------
    async def get_users(self, logins: list[str] | None = None, ids: list[str] | None = None) -> list[dict]:
        params = [("login", l) for l in (logins or [])] + [("id", i) for i in (ids or [])]
        return (await self.request("GET", "/users", params=params or None))["data"]

    async def get_streams(self, logins: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(logins), 100):
            params = [("user_login", l) for l in logins[i : i + 100]] + [("first", "100")]
            out += (await self.request("GET", "/streams", params=params))["data"]
        return out

    # --- videos ------------------------------------------------------------
    async def check_video(self, video_id: str) -> VideoCheck:
        """Never concludes 'deleted': Twitch only tells us whether it returned the video."""
        try:
            data = (await self.request("GET", "/videos", params={"id": video_id}, retries=1))["data"]
        except HelixError as e:
            if e.status == 404:
                return VideoCheck(VideoStatus.UNAVAILABLE_OR_INACCESSIBLE, http_status=404, detail=e.message)
            if e.status == 403:
                return VideoCheck(VideoStatus.FORBIDDEN, http_status=403, detail=e.message)
            if e.status == 401:
                return VideoCheck(VideoStatus.AUTH_REQUIRED, http_status=401, detail=e.message)
            if e.status == 429:
                return VideoCheck(VideoStatus.RATE_LIMITED, http_status=429, detail=e.message)
            return VideoCheck(VideoStatus.CHECK_FAILED, http_status=e.status or None, detail=e.message)
        if not data:
            return VideoCheck(VideoStatus.UNAVAILABLE_OR_INACCESSIBLE, http_status=200, detail="empty data")
        return VideoCheck(VideoStatus.AVAILABLE, video=data[0], http_status=200)

    async def get_user_videos(self, user_id: str, video_type: str = "archive", first: int = 5) -> list[dict]:
        params = {"user_id": user_id, "type": video_type, "first": str(first)}
        return (await self.request("GET", "/videos", params=params))["data"]

    # --- clips -------------------------------------------------------------
    async def create_clip(self, broadcaster_id: str, duration: float | None = None, title: str | None = None) -> dict:
        params: dict[str, Any] = {"broadcaster_id": broadcaster_id}
        if duration is not None:
            params["duration"] = f"{duration:.1f}"
        if title:
            params["title"] = title
        return (await self.request("POST", "/clips", params=params, retries=0))["data"][0]

    async def create_clip_from_vod(self, editor_id: str, broadcaster_id: str, vod_id: str,
                                   vod_offset: int, duration: float, title: str) -> dict:
        params = {"editor_id": editor_id, "broadcaster_id": broadcaster_id, "vod_id": vod_id,
                  "vod_offset": str(vod_offset), "duration": f"{duration:.1f}", "title": title}
        return (await self.request("POST", "/videos/clips", params=params, retries=0))["data"][0]

    async def get_clip(self, clip_id: str) -> dict | None:
        data = (await self.request("GET", "/clips", params={"id": clip_id}))["data"]
        return data[0] if data else None

    async def wait_for_clip(self, clip_id: str, timeout_s: float = 60.0, interval_s: float = 3.0) -> dict | None:
        """Docs: if Get Clips hasn't returned the clip after 60 s, assume creation failed."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            clip = await self.get_clip(clip_id)
            if clip:
                return clip
            await asyncio.sleep(interval_s)
        return None

    async def get_clip_downloads(self, editor_id: str, broadcaster_id: str, clip_ids: list[str]) -> list[dict]:
        params = [("editor_id", editor_id), ("broadcaster_id", broadcaster_id)] + [("clip_id", c) for c in clip_ids]
        return (await self.request("GET", "/clips/downloads", params=params))["data"]

    # --- eventsub ----------------------------------------------------------
    async def create_subscription(self, sub_type: str, version: str, condition: dict, session_id: str) -> dict:
        body = {"type": sub_type, "version": version, "condition": condition,
                "transport": {"method": "websocket", "session_id": session_id}}
        return await self.request("POST", "/eventsub/subscriptions", json=body, retries=0)

    async def delete_subscription(self, sub_id: str) -> None:
        await self.request("DELETE", "/eventsub/subscriptions", params={"id": sub_id}, retries=1)


async def download_file(url: str, dest, timeout: float = 120.0) -> int:
    """Download a clip file from a URL returned by Get Clips Download. Returns bytes written."""
    from pathlib import Path
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    f.write(chunk)
                    total += len(chunk)
    tmp.replace(dest)
    return total


def pick_download(entry: dict, want: str = "portrait") -> tuple[str, str, bool]:
    """Return (orientation_downloaded, url, needs_local_crop) from a Get Clips Download entry.

    Both URLs are nullable. For portrait output, prefer Twitch's portrait file and
    fall back to the landscape file + local FFmpeg crop when it's missing.
    """
    portrait = entry.get("portrait_download_url")
    landscape = entry.get("landscape_download_url")
    if want == "portrait":
        if portrait:
            return "portrait", portrait, False
        if landscape:
            return "landscape", landscape, True
    else:
        if landscape:
            return "landscape", landscape, False
    raise ValueError(f"no usable download URL for clip {entry.get('clip_id')} (want={want})")
