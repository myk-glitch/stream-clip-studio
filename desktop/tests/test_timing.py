import pytest

from twitchclipper.timing.window import (
    ModeAStatus, TimingConfig, Window, check_coverage, estimate_event, plan_mode_a,
    plan_mode_b, target_window,
)

CFG = TimingConfig()


def test_defaults_are_22_before_5_after():
    assert CFG.pre_roll_s == 22 and CFG.post_roll_s == 5 and CFG.target_length_s == 27
    w = target_window(100.0, CFG)
    assert (w.start, w.end) == (78.0, 105.0)


def test_event_estimate_subtracts_delay_and_reaction():
    assert estimate_event(110.0, CFG) == pytest.approx(102.0)  # 110 - 5 - 3
    custom = TimingConfig(viewer_delay_s=2, reaction_s=1.5)
    assert estimate_event(110.0, custom) == pytest.approx(106.5)


def test_mode_a_includes_the_5s_after_the_call():
    # E' = 100, request at T = 110. Capture is [25, 115]; clip ends at T+5 = 115.
    p = plan_mode_a(100.0, 110.0, CFG)
    assert p.capture == Window(25.0, 115.0)
    assert p.duration == pytest.approx(37.0)          # T - E' + 27
    assert p.approx_clip == Window(78.0, 115.0)
    assert p.status is ModeAStatus.COVERS_WINDOW_LONGER
    assert p.extra_tail_s == pytest.approx(10.0)      # never an exact 27 s clip
    assert p.full_preroll_deadline == pytest.approx(133.0)   # E' + 33
    assert p.edit_recoverable_deadline == pytest.approx(163.0)  # E' + 63


def test_mode_a_exact_length_only_when_call_at_event():
    p = plan_mode_a(100.0, 100.0, CFG)
    assert p.duration == pytest.approx(27.0)
    assert p.status is ModeAStatus.COVERS_WINDOW_EXACT_LENGTH


def test_mode_a_cap_truncates_preroll_but_edit_url_can_recover():
    p = plan_mode_a(100.0, 135.0, CFG)  # needs 62 s, capped at 60
    assert p.duration == 60.0
    assert p.approx_clip == Window(80.0, 140.0)
    assert p.missing_preroll_s == pytest.approx(2.0)
    assert p.status is ModeAStatus.PREROLL_TRUNCATED_EDIT_RECOVERABLE


def test_mode_a_window_outside_capture():
    p = plan_mode_a(100.0, 170.0, CFG)  # capture [85, 175] starts after E'-22 = 78
    assert p.status is ModeAStatus.OUTSIDE_CAPTURE


def test_mode_b_requests_margin_and_verifies_coverage():
    req = plan_mode_b(100.0, 110.0, CFG)
    assert req.duration == pytest.approx(38.0)  # 37 + 1 s margin
    measured = Window(77.2, 115.1)  # what the clock overlay says the clip really holds
    cov = check_coverage(req.target, measured)
    assert cov.covered
    assert cov.trim_in_s == pytest.approx(0.8)
    assert cov.trim_out_s == pytest.approx(27.8)


def test_mode_b_reports_missing_footage_instead_of_trimming():
    cov = check_coverage(Window(78, 105), Window(80.0, 115.0))
    assert not cov.covered
    assert cov.missing_start_s == pytest.approx(2.0)
