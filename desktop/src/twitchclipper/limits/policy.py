"""Clip limit policy and publish approval rules.

- Clip limit default: STOP creating clips; the monitor itself keeps running and
  logs skipped events. Optional: REPLACE_WEAKEST.
- Twitch clips on channels the user does not own/edit are public and posted under
  the user's account, so automatic creation there is OFF by default and every such
  clip needs manual approval before it is published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..timing.window import TimingConfig


class LimitPolicy(str, Enum):
    STOP = "stop"
    REPLACE_WEAKEST = "replace_weakest"


class Action(str, Enum):
    CREATE = "create"
    SKIP_LIMIT = "skip_limit_reached"
    REPLACE = "replace"


@dataclass
class ClipRecord:
    clip_id: str
    score: float
    local_file: bool = True        # we hold a local file we may delete
    twitch_hosted: bool = False    # a clip exists on Twitch (cannot be deleted via documented API)
    replaced: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    victim: ClipRecord | None = None
    note: str = ""


@dataclass
class ClipBook:
    limit: int
    policy: LimitPolicy = LimitPolicy.STOP
    clips: list[ClipRecord] = field(default_factory=list)
    skipped: list[tuple[str, float]] = field(default_factory=list)

    def active(self) -> list[ClipRecord]:
        return [c for c in self.clips if not c.replaced]

    def decide(self, event_id: str, score: float) -> Decision:
        active = self.active()
        if len(active) < self.limit:
            return Decision(Action.CREATE)
        if self.policy is LimitPolicy.REPLACE_WEAKEST:
            weakest = min(active, key=lambda c: c.score)
            if score > weakest.score:
                note = ""
                if weakest.twitch_hosted and not weakest.local_file:
                    note = "weakest clip is removed from the app list only; it stays on Twitch"
                elif weakest.twitch_hosted:
                    note = "local copy deleted; the Twitch-hosted clip stays on Twitch"
                return Decision(Action.REPLACE, weakest, note)
        self.skipped.append((event_id, score))
        return Decision(Action.SKIP_LIMIT, note="limit reached; monitoring continues")

    def commit_create(self, record: ClipRecord) -> None:
        self.clips.append(record)

    def commit_replace(self, new: ClipRecord, victim: ClipRecord) -> None:
        """Call only AFTER the new clip exported successfully."""
        victim.replaced = True
        self.clips.append(new)


class Relation(str, Enum):
    OWN = "own"
    EDITOR = "editor"
    OTHER = "other"


@dataclass(frozen=True)
class PublishSettings:
    auto_create_own: bool = True
    auto_create_editor: bool = False
    auto_create_other: bool = False  # required default: off


def needs_approval(relation: Relation, settings: PublishSettings) -> bool:
    if relation is Relation.OWN:
        return not settings.auto_create_own
    if relation is Relation.EDITOR:
        return not settings.auto_create_editor
    return not settings.auto_create_other


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class ApprovalRequest:
    event_id: str
    event_time: float   # E'
    created_at: float
    cfg: TimingConfig = field(default_factory=TimingConfig)
    state: ApprovalState = ApprovalState.PENDING

    @property
    def full_preroll_deadline(self) -> float:
        return self.event_time - self.cfg.pre_roll_s + self.cfg.max_clip_s - self.cfg.capture_after_s

    @property
    def last_chance_deadline(self) -> float:
        """After this, the window has left Twitch's capture and can't be clipped at all."""
        return self.event_time - self.cfg.pre_roll_s + self.cfg.capture_before_s

    def approve(self, now: float) -> bool:
        if self.state is not ApprovalState.PENDING:
            return False
        if now > self.last_chance_deadline:
            self.state = ApprovalState.EXPIRED
            return False
        self.state = ApprovalState.APPROVED
        return True

    def reject(self) -> None:
        if self.state is ApprovalState.PENDING:
            self.state = ApprovalState.REJECTED

    def tick(self, now: float) -> None:
        if self.state is ApprovalState.PENDING and now > self.last_chance_deadline:
            self.state = ApprovalState.EXPIRED
