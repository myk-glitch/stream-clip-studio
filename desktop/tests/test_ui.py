"""Desktop UI tests (pytest-qt, offscreen): every page builds, navigation works, and the
local workflow runs end to end through the real widgets."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from twitchclipper.core.context import create_context
from twitchclipper.ui import theme
from twitchclipper.ui.main_window import NAV, MainWindow


@pytest.fixture
def window(qtbot, qapp, app_home):
    theme.apply(qapp, dark=False)
    ctx = create_context()
    win = MainWindow(ctx, detect_encoders=False)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.runner.wait(30000)


def test_all_pages_and_navigation(window):
    assert [k for k, _, _ in NAV] == ["dashboard", "live", "analyze", "results", "account", "settings"]
    for key, label, _ in NAV:
        window.navigate(key)
        assert window.current_key() == key
        assert window.stack.currentWidget() is window.scrolls[key] and window.scrolls[key].widget() is window.pages[key]
        assert window.nav.currentItem().text() == label
    assert window.sb_ffmpeg.text() in ("FFmpeg ✓", "FFmpeg ✗")
    assert window.sb_account.text() == "Twitch: not signed in"


def test_dark_theme_applies(qapp):
    t = theme.apply(qapp, dark=True)
    assert t is theme.DARK and theme.tokens().bg == theme.DARK.bg
    theme.apply(qapp, dark=False)


def test_encoder_detection_job_updates_status(qtbot, window, ffmpeg_install):
    with qtbot.waitSignal(window.ctx.events.ffmpeg_changed, timeout=60000):
        window.start_encoder_detection(force=True)
    assert window.ctx.encoders is not None
    assert window.sb_encoder.text().startswith("Encoder: ")
    assert window.ctx.encoders.works("libx264")
    card = window.pages["dashboard"].card_encoder.value.text()
    assert card == window.ctx.encoder_for_export()[0]


def test_live_monitor_channels(window):
    page = window.pages["live"]
    page.login_edit.setText("Some_Streamer")
    page.add_channel()
    page.login_edit.setText("bad name!")
    page.add_channel()
    assert "not a valid" in page.error.text()
    assert page.table.rowCount() == 1 and page.table.item(0, 0).text() == "some_streamer"
    assert page.table.item(0, 2).text() == "After your approval"   # other channels: approval by default
    assert not page.start_btn.isEnabled()
    page.confirm = lambda _t: True
    page.table.selectRow(0)
    page.remove_selected()
    assert page.table.rowCount() == 0


def test_settings_save_roundtrip(window):
    page = window.pages["settings"]
    assert page.pre.value() == 22.0 and page.post.value() == 5.0
    assert not page.auto_other.isChecked()
    page.pre.setValue(20.0)
    page.clip_limit.setValue(3)
    page.keywords.setPlainText("kekw = 2\nbad line\npog=1.5")
    page.save()
    s = window.ctx.settings
    assert s.timing.pre_roll_s == 20.0 and s.limits.clip_limit == 3
    assert s.detection.keywords == {"kekw": 2.0, "pog": 1.5}
    assert window.ctx.paths.settings_file.exists()
    assert window.pages["analyze"].limit.value() == 3      # other pages follow settings changes
    page.reset_defaults()
    assert page.pre.value() == 22.0


def test_account_page_without_client_id(window):
    page = window.pages["account"]
    assert not page.signin_btn.isEnabled()
    assert "Client ID" in page.status.text()
    assert page.selected_scopes() == ["user:read:chat"]
    page.client_id.setText("abc123")
    page.save_client_id()
    assert window.ctx.settings.twitch.client_id == "abc123" and page.signin_btn.isEnabled()


def test_local_workflow_end_to_end(qtbot, window, sample_media, ffmpeg_install):
    ctx = window.ctx
    ctx.settings.export.encoder = "libx264"
    page = window.pages["analyze"]
    window.navigate("analyze")
    page.video_edit.setText(str(sample_media["video"]))
    page.chat_edit.setText(str(sample_media["chat"]))
    assert page.analyze_btn.isEnabled()
    qtbot.mouseClick(page.analyze_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.result is not None, timeout=60000)
    assert page.table.rowCount() == 3
    assert page.analysis_id is not None and len(ctx.db.highlights(page.analysis_id)) == 3
    assert page.export_btn.isEnabled()

    # export only #1, as portrait and landscape
    for r in range(1, page.table.rowCount()):
        page.table.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
    page.orientation.setCurrentIndex(page.orientation.findData("both"))
    qtbot.mouseClick(page.export_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(ctx.db.clips()) == 2, timeout=120000)
    clips = ctx.db.clips()
    assert {c["orientation"] for c in clips} == {"landscape", "portrait"}
    assert all(Path(c["path"]).exists() and Path(c["path"]).parent == ctx.clips_dir for c in clips)
    assert all(c["duration_s"] == pytest.approx(27.0, abs=0.1) for c in clips)

    results = window.pages["results"]
    assert results.table.rowCount() == 2
    dash = window.pages["dashboard"]
    assert dash.card_clips.value.text() == "2"

    # Select a clip (loads it into the preview player), then delete it while it's loaded:
    # the player must release the file so Windows allows the delete.
    results.table.selectRow(0)
    victim = Path(results.selected()["path"])
    qtbot.wait(500)
    warnings = []
    results.warn = lambda title, text: warnings.append(text)
    results.confirm = lambda _t: True
    results.delete_selected()
    assert warnings == []
    assert not victim.exists() and results.table.rowCount() == 1 and dash.card_clips.value.text() == "1"


def test_analyze_reports_bad_inputs(window):
    page = window.pages["analyze"]
    page.video_edit.setText("C:/does/not/exist.mp4")
    page.chat_edit.setText("")
    page.start_analysis()
    assert "video file" in page.status.text() and "chat log" in page.status.text()
