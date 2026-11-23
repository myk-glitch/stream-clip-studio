"""Chat subscription queue that respects Twitch's limits for normal accounts.

Documented limits (dev.twitch.tv/docs/chat, checked 2026-11-20):
  * 20 join attempts per 10 seconds per user
  * 100 concurrent channel joins (channels where the user is broadcaster or
    moderator don't count toward this limit)
  * EventSub WebSocket: 300 enabled subscriptions per connection, 3 connections

Every subscription attempt counts toward the rate limit, successful or not.
Exempt (broadcaster/moderator) channels skip the concurrency cap but still use
attempt budget - a conservative reading of the docs.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable


class JoinState(str, Enum):
    QUEUED = "queued"
    JOINING = "joining"
    ACTIVE = "active"
    BACKOFF = "backoff"
    FAILED = "failed"


@dataclass
class ChannelEntry:
    channel_id: str
    exempt: bool = False
    priority: int = 0              # lower value = joined first
    state: JoinState = JoinState.QUEUED
    attempts: int = 0
    not_before: float = 0.0
    last_error: str = ""


@dataclass
class JoinResult:
    ok: bool
    retryable: bool = True
    retry_after_s: float | None = None
    error: str = ""


class JoinQueue:
    def __init__(
        self,
        max_concurrent: int = 100,
        max_attempts: int = 20,
        window_s: float = 10.0,
        max_subscriptions_per_connection: int = 300,
        max_retries: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_concurrent = max_concurrent
        self.max_attempts = max_attempts
        self.window_s = window_s
        self.max_per_connection = max_subscriptions_per_connection
        self.max_retries = max_retries
        self.clock = clock
        self.entries: dict[str, ChannelEntry] = {}
        self._heap: list[tuple[int, int, str]] = []
        self._seq = itertools.count()
        self._attempt_times: deque[float] = deque()
        self.attempt_log: list[float] = []

    # --- bookkeeping -------------------------------------------------------
    def request(self, channel_id: str, exempt: bool = False, priority: int = 0) -> ChannelEntry:
        e = self.entries.get(channel_id)
        if e and e.state in (JoinState.ACTIVE, JoinState.JOINING, JoinState.QUEUED, JoinState.BACKOFF):
            return e
        e = ChannelEntry(channel_id, exempt, priority)
        self.entries[channel_id] = e
        heapq.heappush(self._heap, (priority, next(self._seq), channel_id))
        return e

    def release(self, channel_id: str) -> None:
        """Unsubscribed (stream ended / user removed the channel): frees a slot."""
        self.entries.pop(channel_id, None)

    def active_counted(self) -> int:
        return sum(1 for e in self.entries.values()
                   if e.state in (JoinState.ACTIVE, JoinState.JOINING) and not e.exempt)

    def active_total(self) -> int:
        return sum(1 for e in self.entries.values() if e.state in (JoinState.ACTIVE, JoinState.JOINING))

    def _prune(self, now: float) -> None:
        while self._attempt_times and now - self._attempt_times[0] >= self.window_s:
            self._attempt_times.popleft()

    def attempts_available(self) -> int:
        self._prune(self.clock())
        return self.max_attempts - len(self._attempt_times)

    def seconds_until_attempt(self) -> float:
        now = self.clock()
        self._prune(now)
        if len(self._attempt_times) < self.max_attempts:
            return 0.0
        return self._attempt_times[0] + self.window_s - now

    # --- scheduling --------------------------------------------------------
    def next_ready(self) -> ChannelEntry | None:
        """Pop the next channel allowed to attempt a join right now, or None."""
        now = self.clock()
        self._prune(now)
        if len(self._attempt_times) >= self.max_attempts:
            return None
        if self.active_total() >= self.max_per_connection:
            return None
        deferred: list[tuple[int, int, str]] = []
        chosen = None
        while self._heap:
            item = heapq.heappop(self._heap)
            e = self.entries.get(item[2])
            if e is None or e.state not in (JoinState.QUEUED, JoinState.BACKOFF):
                continue
            if e.not_before > now or (not e.exempt and self.active_counted() >= self.max_concurrent):
                deferred.append(item)
                continue
            chosen = e
            break
        for item in deferred:
            heapq.heappush(self._heap, item)
        if chosen is None:
            return None
        chosen.state = JoinState.JOINING
        chosen.attempts += 1
        self._attempt_times.append(now)
        self.attempt_log.append(now)
        return chosen

    def on_result(self, channel_id: str, result: JoinResult) -> None:
        e = self.entries.get(channel_id)
        if e is None:
            return
        if result.ok:
            e.state = JoinState.ACTIVE
            e.last_error = ""
            return
        e.last_error = result.error
        if not result.retryable or e.attempts > self.max_retries:
            e.state = JoinState.FAILED
            return
        backoff = result.retry_after_s if result.retry_after_s is not None else min(300.0, 2.0 ** e.attempts)
        e.state = JoinState.BACKOFF
        e.not_before = self.clock() + backoff
        heapq.heappush(self._heap, (e.priority, next(self._seq), channel_id))

    def waiting(self) -> list[ChannelEntry]:
        return [e for e in self.entries.values() if e.state in (JoinState.QUEUED, JoinState.BACKOFF)]

    # --- async driver ------------------------------------------------------
    async def run(self, join: Callable[[str], Awaitable[JoinResult]], stop: asyncio.Event) -> None:
        while not stop.is_set():
            e = self.next_ready()
            if e is None:
                await asyncio.sleep(max(0.2, min(self.seconds_until_attempt(), 1.0)))
                continue
            try:
                result = await join(e.channel_id)
            except Exception as exc:  # network errors are retryable
                result = JoinResult(False, True, None, repr(exc))
            self.on_result(e.channel_id, result)


def max_attempts_in_any_window(times: list[float], window_s: float) -> int:
    """Verification helper: the largest number of attempts inside any sliding window."""
    best = 0
    j = 0
    s = sorted(times)
    for i, t in enumerate(s):
        while t - s[j] >= window_s:
            j += 1
        best = max(best, i - j + 1)
    return best
