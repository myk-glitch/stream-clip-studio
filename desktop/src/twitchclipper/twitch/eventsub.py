"""EventSub WebSocket client (documented transport).

Docs: dev.twitch.tv/docs/eventsub/handling-websocket-events/
  * subscribe within 10 s of the Welcome message
  * no message for keepalive_timeout_seconds -> assume the connection is lost
  * session_reconnect: connect to reconnect_url, wait for its Welcome, then close the old one
  * never send messages except Pong (the library answers Pings automatically)
  * lost events are NOT replayed; gaps are recorded so the UI can mark them
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import websockets

WS_URL = "wss://eventsub.wss.twitch.tv/ws"


@dataclass
class Gap:
    start: float
    end: float | None = None
    reason: str = ""


@dataclass
class SessionStats:
    welcomes: int = 0
    reconnects_planned: int = 0
    reconnects_unplanned: int = 0
    keepalives: int = 0
    notifications: int = 0
    duplicates: int = 0
    revocations: int = 0
    gaps: list[Gap] = field(default_factory=list)


OnWelcome = Callable[[str, bool], Awaitable[None]]       # (session_id, subscriptions_survive)
OnNotification = Callable[[dict, dict], Awaitable[None]]  # (metadata, payload)


class EventSubClient:
    def __init__(self, on_welcome: OnWelcome, on_notification: OnNotification,
                 url: str = WS_URL, keepalive_s: int | None = 30, grace_s: float = 5.0,
                 on_revocation: Callable[[dict], Awaitable[None]] | None = None):
        self.base_url = url if keepalive_s is None else f"{url}?keepalive_timeout_seconds={keepalive_s}"
        self.on_welcome = on_welcome
        self.on_notification = on_notification
        self.on_revocation = on_revocation
        self.grace_s = grace_s
        self.stats = SessionStats()
        self.session_id: str | None = None
        self._seen: dict[str, float] = {}
        self._consecutive_failures = 0   # resets after every successful Welcome

    def _backoff_s(self) -> float:
        """2, 4, 8, 16, 30, 30… s for consecutive failed attempts; a drop after a good connection starts at 2 s."""
        return min(30.0, 2.0 ** min(max(self._consecutive_failures, 1), 5))

    def _dedupe(self, message_id: str) -> bool:
        now = time.monotonic()
        if len(self._seen) > 5000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 600}
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False

    async def _welcome(self, ws) -> tuple[str, float]:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        msg = json.loads(raw)
        if msg["metadata"]["message_type"] != "session_welcome":
            raise RuntimeError(f"expected session_welcome, got {msg['metadata']['message_type']}")
        s = msg["payload"]["session"]
        self.stats.welcomes += 1
        return s["id"], float(s.get("keepalive_timeout_seconds") or 10)

    async def run(self, stop: asyncio.Event) -> None:
        url = self.base_url
        survive = False
        ws = None
        while not stop.is_set():
            try:
                if ws is None:
                    ws = await websockets.connect(url, max_size=2**22)
                    self.session_id, keepalive = await self._welcome(ws)
                    self._consecutive_failures = 0
                    if self.stats.gaps and self.stats.gaps[-1].end is None:
                        self.stats.gaps[-1].end = time.time()
                    await self.on_welcome(self.session_id, survive)
                    survive = False
                ws, url, survive = await self._loop(ws, keepalive, stop)
            except (OSError, asyncio.TimeoutError, websockets.ConnectionClosed, RuntimeError,
                    ValueError, KeyError, TypeError) as exc:
                # ValueError/KeyError/TypeError: a malformed or unexpected frame. Reconnect rather than
                # let one bad message end chat monitoring for the rest of the stream.
                self.stats.reconnects_unplanned += 1
                self._consecutive_failures += 1
                if not self.stats.gaps or self.stats.gaps[-1].end is not None:
                    self.stats.gaps.append(Gap(time.time(), reason=type(exc).__name__))
                if ws is not None:
                    await ws.close()
                ws, url, survive = None, self.base_url, False
                await asyncio.sleep(self._backoff_s())
        if ws is not None:
            await ws.close()

    async def _loop(self, ws, keepalive: float, stop: asyncio.Event):
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=keepalive + self.grace_s)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError("no keepalive within keepalive_timeout_seconds")
            msg = json.loads(raw)
            meta, payload = msg["metadata"], msg.get("payload", {})
            mtype = meta["message_type"]
            if mtype == "session_keepalive":
                self.stats.keepalives += 1
            elif mtype == "notification":
                if self._dedupe(meta["message_id"]):
                    self.stats.duplicates += 1
                    continue
                self.stats.notifications += 1
                await self.on_notification(meta, payload)
            elif mtype == "session_reconnect":
                self.stats.reconnects_planned += 1
                new_url = payload["session"]["reconnect_url"]
                new_ws = await websockets.connect(new_url, max_size=2**22)
                try:
                    self.session_id, keepalive = await self._welcome(new_ws)
                except BaseException:
                    await new_ws.close()   # don't leak the half-open replacement connection
                    raise
                await ws.close()
                await self.on_welcome(self.session_id, True)
                ws = new_ws
            elif mtype == "revocation":
                self.stats.revocations += 1
                if self.on_revocation:
                    await self.on_revocation(payload)
        return ws, self.base_url, False
