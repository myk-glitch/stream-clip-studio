"""Preview behaviour: first frame shown before Play, portrait (1080x1920) playback with each
working encoder, and the FFmpeg "unspecified pixel format" probe warning (regression check)."""

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QAudioBufferOutput, QMediaPlayer

from twitchclipper.export.hwaccel import detect
from twitchclipper.pipeline import local as pipeline
from twitchclipper.timing.window import Window
from twitchclipper.ui.widgets.video_preview import VideoPreview

PROBE_WARNINGS = ("unspecified pixel format", "Could not find codec parameters")


@pytest.fixture
def preview(qtbot):
    w = VideoPreview(closable=True)
    qtbot.addWidget(w)
    w.resize(420, 560)
    w.show()
    yield w
    w.release()


@pytest.fixture(scope="session")
def encoders(ffmpeg_install):
    rep = detect(ffmpeg_install)
    names = ["libx264"]
    if rep.auto_choice != "libx264":
        names.append(rep.auto_choice)          # the hardware encoder exports use on this PC (QSV here)
    return names


def wait_status(qtbot, w, *statuses, timeout=10000):
    qtbot.waitUntil(lambda: w.player.mediaStatus() in statuses, timeout=timeout)


def test_first_frame_is_shown_before_play_without_sound(qtbot, preview, tmp_path, test_clip):
    copy = tmp_path / "clip.mp4"
    shutil.copyfile(test_clip[0], copy)
    frames, audio = [], []
    preview.video.videoSink().videoFrameChanged.connect(lambda f: frames.append(f))
    tap = QAudioBufferOutput()
    preview.player.setAudioBufferOutput(tap)
    tap.audioBufferReceived.connect(lambda b: audio.append(b))
    preview.load(copy)
    qtbot.waitUntil(lambda: len(frames) >= 1, timeout=10000)
    qtbot.wait(500)
    assert preview.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    assert preview.player.position() == 0 and audio == []        # a still frame, no sound
    assert preview.play_btn.text() == "Play"
    img = frames[-1].toImage()
    assert img.pixelColor(100, 250).red() > 200                  # the red test bar
    # Play from there works, and seeking while paused shows the new frame
    qtbot.mouseClick(preview.play_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: preview.player.position() > 800, timeout=5000)
    preview.toggle()
    n = len(frames)
    preview.seek(6.0)
    qtbot.waitUntil(lambda: len(frames) > n, timeout=5000)
    assert abs(frames[-1].startTime() / 1e6 - 6.0) < 0.1
    preview.player.setAudioBufferOutput(None)
    preview.release()
    copy.unlink()                                                # released → deletable


def test_seek_before_load_finishes_shows_that_frame(qtbot, preview, tmp_path, test_clip):
    """The Analyze page loads the video and immediately seeks to the selected highlight."""
    copy = tmp_path / "clip.mp4"
    shutil.copyfile(test_clip[0], copy)
    frames = []
    preview.video.videoSink().videoFrameChanged.connect(lambda f: frames.append(f))
    preview.load(copy)
    preview.seek(7.5)
    qtbot.waitUntil(lambda: len(frames) >= 1, timeout=10000)
    qtbot.wait(300)
    assert abs(frames[-1].startTime() / 1e6 - 7.5) < 0.1
    assert preview.player.playbackState() == QMediaPlayer.PlaybackState.PausedState


def test_portrait_export_plays_in_preview(qtbot, preview, tmp_path, sample_media, ffmpeg_install, encoders, capfd):
    """Portrait clips from the low-detail synthetic footage (where the probe warning was first seen)."""
    for enc in encoders:
        out = tmp_path / f"portrait_{enc}.mp4"
        res = pipeline.export(ffmpeg_install.ffmpeg, ffmpeg_install.ffprobe,
                              pipeline.ExportJob(sample_media["video"], Window(69.0, 96.0), out, "portrait", 0.5, enc))
        assert (res.width, res.height) == (1080, 1920)
        capfd.readouterr()                                       # only count what Qt's backend logs from here
        frames = []
        conn = preview.video.videoSink().videoFrameChanged.connect(lambda f: frames.append(f))
        preview.load(out)
        wait_status(qtbot, preview, QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia)
        assert preview.player.hasVideo() and "video ✓" in preview.info.text(), enc
        assert preview.video.aspectRatioMode() == Qt.AspectRatioMode.KeepAspectRatio   # pillarboxed, not stretched
        preview.toggle()
        qtbot.waitUntil(lambda: preview.player.position() >= 2000, timeout=10000)
        assert len(frames) >= 40, f"{enc}: only {len(frames)} frames in 2 s"
        img = frames[-1].toImage()
        assert (img.width(), img.height()) == (1080, 1920), enc
        # not a blank frame: the footage has a white clock strip on a dark background
        lum = {img.pixelColor(x, y).lightness() for x in range(0, 1080, 60) for y in range(0, 1920, 60)}
        assert max(lum) - min(lum) > 100, enc
        err = capfd.readouterr().err
        assert not any(w in err for w in PROBE_WARNINGS), f"{enc}: {err[-500:]}"
        preview.video.videoSink().videoFrameChanged.disconnect(conn)
        preview.release()
        Path(out).unlink()


def test_analyze_preview_shows_selected_highlight_frame(qtbot, qapp, app_home, sample_media):
    from twitchclipper.core.context import create_context
    from twitchclipper.ui import theme
    from twitchclipper.ui.main_window import MainWindow

    theme.apply(qapp, dark=False)
    win = MainWindow(create_context(), detect_encoders=False)
    qtbot.addWidget(win)
    win.show()
    page = win.pages["analyze"]
    win.navigate("analyze")
    frames = []
    page.preview.video.videoSink().videoFrameChanged.connect(lambda f: frames.append(f))
    page.video_edit.setText(str(sample_media["video"]))
    page.chat_edit.setText(str(sample_media["chat"]))
    page.start_analysis()
    qtbot.waitUntil(lambda: page.result is not None, timeout=60000)
    start = page.result.highlights[0].window_start
    qtbot.waitUntil(lambda: len(frames) >= 1, timeout=10000)
    qtbot.wait(300)
    assert abs(frames[-1].startTime() / 1e6 - start) < 0.2      # the selected highlight, without pressing Play
    page.table.selectRow(1)
    second = page.result.highlights[1].window_start
    qtbot.waitUntil(lambda: abs(frames[-1].startTime() / 1e6 - second) < 0.2, timeout=5000)
    page.preview.release()
    win.runner.wait(30000)
