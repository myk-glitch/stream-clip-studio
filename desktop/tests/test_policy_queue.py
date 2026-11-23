from twitchclipper.twitch.join_queue import JoinQueue, JoinResult, JoinState, max_attempts_in_any_window
from twitchclipper.limits.policy import (
    Action, ApprovalRequest, ApprovalState, ClipBook, ClipRecord, LimitPolicy, PublishSettings,
    Relation, needs_approval,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# --- clip limit -------------------------------------------------------------
def test_stop_policy_is_default_and_skips_without_stopping():
    book = ClipBook(limit=2)
    assert book.policy is LimitPolicy.STOP
    for i, s in enumerate([5.0, 6.0]):
        assert book.decide(f"e{i}", s).action is Action.CREATE
        book.commit_create(ClipRecord(f"c{i}", s))
    d = book.decide("e2", 99.0)
    assert d.action is Action.SKIP_LIMIT and "monitoring continues" in d.note
    assert book.skipped == [("e2", 99.0)]


def test_replace_weakest_only_for_stronger_events():
    book = ClipBook(limit=2, policy=LimitPolicy.REPLACE_WEAKEST)
    for i, s in enumerate([5.0, 8.0]):
        book.commit_create(ClipRecord(f"c{i}", s))
    assert book.decide("weak", 4.0).action is Action.SKIP_LIMIT
    d = book.decide("strong", 9.0)
    assert d.action is Action.REPLACE and d.victim.clip_id == "c0"
    book.commit_replace(ClipRecord("c2", 9.0), d.victim)
    assert sorted(c.clip_id for c in book.active()) == ["c1", "c2"]


def test_replace_notes_twitch_hosted_clip_stays():
    book = ClipBook(limit=1, policy=LimitPolicy.REPLACE_WEAKEST)
    book.commit_create(ClipRecord("tw", 1.0, local_file=False, twitch_hosted=True))
    assert "stays on Twitch" in book.decide("x", 2.0).note


# --- publish approval -------------------------------------------------------
def test_other_channels_need_approval_by_default():
    s = PublishSettings()
    assert needs_approval(Relation.OTHER, s)
    assert needs_approval(Relation.EDITOR, s)
    assert not needs_approval(Relation.OWN, s)


def test_approval_deadlines_and_expiry():
    req = ApprovalRequest("e", event_time=100.0, created_at=110.0)
    assert req.full_preroll_deadline == 133.0
    assert req.last_chance_deadline == 163.0
    req.tick(150.0)
    assert req.state is ApprovalState.PENDING
    assert not ApprovalRequest("e", 100.0, 110.0).approve(170.0)
    late = ApprovalRequest("e", 100.0, 110.0)
    late.tick(164.0)
    assert late.state is ApprovalState.EXPIRED


# --- join queue -------------------------------------------------------------
def _drive(q: JoinQueue, clock: FakeClock, seconds: float, fail=lambda cid: False, step=0.25):
    end = clock.t + seconds
    while clock.t < end:
        while (e := q.next_ready()) is not None:
            q.on_result(e.channel_id, JoinResult(False, True, None, "x") if fail(e.channel_id) else JoinResult(True))
            assert q.active_counted() <= q.max_concurrent
        clock.t += step


def test_join_queue_caps_concurrency_and_rate():
    clock = FakeClock()
    q = JoinQueue(clock=clock)
    for i in range(150):
        q.request(f"ch{i}", priority=i)
    _drive(q, clock, 120)
    assert q.active_counted() == 100
    assert len(q.waiting()) == 50
    assert max_attempts_in_any_window(q.attempt_log, 10.0) <= 20
    # 100 joins at 20 per 10 s: batches at t=0,10,20,30,40 -> the 100th is at >= 40 s.
    assert q.attempt_log[99] - q.attempt_log[0] >= 40.0


def test_release_admits_next_and_exempt_bypasses_cap():
    clock = FakeClock()
    q = JoinQueue(max_concurrent=3, clock=clock)
    for i in range(5):
        q.request(f"ch{i}", priority=i)
    q.request("mychannel", exempt=True, priority=99)
    _drive(q, clock, 20)
    assert q.active_counted() == 3
    assert q.entries["mychannel"].state is JoinState.ACTIVE
    q.release("ch0")
    _drive(q, clock, 20)
    assert q.entries["ch3"].state is JoinState.ACTIVE
    assert q.entries["ch4"].state is JoinState.QUEUED


def test_failed_attempts_count_toward_rate_and_back_off():
    clock = FakeClock()
    q = JoinQueue(clock=clock, max_retries=2)
    for i in range(30):
        q.request(f"ch{i}")
    _drive(q, clock, 200, fail=lambda cid: cid in {"ch0", "ch1"})
    assert max_attempts_in_any_window(q.attempt_log, 10.0) <= 20
    assert q.entries["ch0"].state is JoinState.FAILED
    assert q.entries["ch0"].attempts == 3
    assert q.active_counted() == 28
