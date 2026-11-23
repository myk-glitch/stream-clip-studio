"""Results page with the synthetic test clip: import (copy), preview, real playback with audio,
Close, delete, Open and Show in folder. The original fixture must never change."""

import os
import stat
import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QAudioBufferOutput, QAudioFormat, QMediaDevices, QMediaPlayer

from tests.support.helpers import RecordingLauncher, import_via_ui
from twitchclipper.core.context import create_context
from twitchclipper.export import ffmpeg as ft
from twitchclipper.pipeline import library
from twitchclipper.storage.db import Database
from twitchclipper.ui import theme
from twitchclipper.ui.main_window import MainWindow
from twitchclipper.ui.pages import results as results_mod


@pytest.fixture
def window(qtbot, qapp, app_home):
    theme.apply(qapp, dark=False)
    win = MainWindow(create_context(), detect_encoders=False)
    qtbot.addWidget(win)
    win.show()
    win.navigate("results")
    yield win
    win.pages["results"].preview.release()
    win.runner.wait(30000)


@pytest.fixture(autouse=True)
def fixture_untouched(test_clip):
    """Every test in this file ends by proving the original fixture is intact."""
    path, digest = test_clip
    yield
    assert path.exists(), "the original test fixture was deleted"
    assert ft.sha256_file(path) == digest, "the original test fixture was modified"


# --- library (no UI) --------------------------------------------------------

def test_import_copies_file_and_never_touches_original(tmp_path, test_clip, ffmpeg_install):
    src, digest = test_clip
    db = Database(tmp_path / "lib.db")
    clips = tmp_path / "clips"
    a = library.import_video(ffmpeg_install.ffprobe, src, clips, db)
    b = library.import_video(ffmpeg_install.ffprobe, src, clips, db)
    assert a.path.parent == clips / "imported" and a.path.name == src.name
    assert b.path.name == f"{src.stem} (2){src.suffix}"          # never overwrites an earlier copy
    assert ft.sha256_file(a.path) == digest == ft.sha256_file(b.path)
    assert (a.width, a.height, a.has_audio) == (1280, 720, True) and abs(a.duration_s - 12.0) < 0.05
    assert os.access(a.path, os.W_OK)                              # copy is deletable even if the original is read-only
    row = db.clip(a.clip_id)
    assert row["source"] == "import" and row["original_path"] == str(src.resolve())
    assert row["orientation"] == "landscape" and row["encoder"] == "h264"
    assert not list((clips / "imported").glob("*.part"))


def test_import_rejects_non_video_and_leaves_nothing_behind(tmp_path, ffmpeg_install):
    db = Database(tmp_path / "lib.db")
    bogus = tmp_path / "notes.mp4"
    bogus.write_text("not a video")
    with pytest.raises(ValueError, match="isn't a video"):
        library.import_video(ffmpeg_install.ffprobe, bogus, tmp_path / "clips", db)
    with pytest.raises(FileNotFoundError):
        library.import_video(ffmpeg_install.ffprobe, tmp_path / "missing.mp4", tmp_path / "clips", db)
    assert db.clips() == [] and not (tmp_path / "clips" / "imported").exists()


def test_import_cancel_removes_partial_copy(tmp_path, test_clip, ffmpeg_install):
    db = Database(tmp_path / "lib.db")
    with pytest.raises(InterruptedError):
        library.import_video(ffmpeg_install.ffprobe, test_clip[0], tmp_path / "clips", db, cancelled=lambda: True)
    assert list((tmp_path / "clips" / "imported").iterdir()) == [] and db.clips() == []


# --- Results page -----------------------------------------------------------

def _dominant_frequencies(samples: np.ndarray, rate: int) -> tuple[float, float]:
    spec = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1 / rate)
    main = float(freqs[np.argmax(spec)])
    near_1760 = (freqs > 1700) & (freqs < 1820)
    beep_ratio = float(spec[near_1760].max() / np.median(spec))
    return main, beep_ratio


def test_import_preview_play_audio_close_and_delete(qtbot, window, test_clip):
    page = window.pages["results"]
    src, digest = test_clip
    clip = import_via_ui(qtbot, page, src)

    # appears as a selectable clip, loaded into the preview
    assert page.table.rowCount() == 1 and clip is not None
    copy = Path(clip["path"])
    assert copy != src and copy.exists() and copy.parent.name == "imported"
    assert page.table.item(0, 1).text() == "Imported file" and page.table.item(0, 2).text() == "landscape"
    assert page.table.item(0, 3).text() == "0:12"
    assert Path(page.preview.source_path) == copy
    assert page.open_btn.isEnabled() and page.folder_btn.isEnabled() and page.delete_btn.isEnabled()
    player = page.preview.player
    qtbot.waitUntil(lambda: player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadedMedia,
                                                     QMediaPlayer.MediaStatus.BufferedMedia), timeout=10000)
    assert player.hasVideo() and player.hasAudio()
    assert "video ✓" in page.preview.info.text() and "audio ✓" in page.preview.info.text()
    assert page.preview.time.text().endswith("/ 0:12")

    # tap the decoded audio and the rendered video frames while it plays
    frames, audio, fmt = [], [], {}
    page.preview.video.videoSink().videoFrameChanged.connect(lambda f: frames.append(f))
    tap = QAudioBufferOutput()
    player.setAudioBufferOutput(tap)

    def on_audio(buf):
        f = buf.format()
        fmt.update(rate=f.sampleRate(), ch=f.channelCount(), float=f.sampleFormat() == QAudioFormat.SampleFormat.Float)
        audio.append(np.frombuffer(bytes(buf.constData()), dtype=np.float32 if fmt["float"] else np.int16).copy())

    tap.audioBufferReceived.connect(on_audio)

    qtbot.mouseClick(page.preview.play_btn, Qt.MouseButton.LeftButton)       # press Play
    qtbot.waitUntil(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState, timeout=5000)
    assert page.preview.play_btn.text() == "Pause"
    qtbot.waitUntil(lambda: player.position() >= 2500, timeout=10000)
    assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    # video: frames keep arriving (~30 fps) and show the colour bars
    assert len(frames) >= 45, f"only {len(frames)} frames rendered in 2.5 s"
    img = frames[-1].toImage()
    assert (img.width(), img.height()) == (1280, 720)
    red, blue = img.pixelColor(100, 250), img.pixelColor(740, 250)
    assert red.red() > 200 and red.green() < 60 and red.blue() < 60, red.name()
    assert blue.blue() > 200 and blue.red() < 60 and blue.green() < 60, blue.name()

    # audio: routed to the system's default output, unmuted, and the decoded signal is the fixture's tone
    out = page.preview.audio
    assert not out.isMuted() and out.volume() > 0.5
    assert out.device().id() == QMediaDevices.defaultAudioOutput().id() and not out.device().isNull()
    pcm = np.concatenate(audio)
    if not fmt["float"]:
        pcm = pcm.astype(np.float32) / 32768
    left = pcm[0::fmt["ch"]]
    assert len(left) >= fmt["rate"] * 2, "less than 2 s of audio decoded"
    assert 0.05 < float(np.abs(left).max()) < 1.0                       # audible, not clipped
    main, beep_ratio = _dominant_frequencies(left[: fmt["rate"] * 2], fmt["rate"])
    assert abs(main - 440) < 3, main                                    # the 440 Hz tone
    assert beep_ratio > 20, beep_ratio                                  # plus the 1760 Hz second beeps
    player.setAudioBufferOutput(None)

    # pause works too
    qtbot.mouseClick(page.preview.play_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PausedState, timeout=5000)

    # Close the preview: the file handle is released immediately
    qtbot.mouseClick(page.preview.close_btn, Qt.MouseButton.LeftButton)
    assert page.preview.source_path == "" and not page.preview.play_btn.isEnabled()
    assert page.preview.info.text() == "No clip loaded"
    moved = copy.with_name("released-check.mp4")
    os.replace(copy, moved)          # fails with PermissionError on Windows if anything still holds the file
    os.replace(moved, copy)

    # delete the copy
    page.confirm = lambda text: "Only this imported copy is deleted" in text
    warnings = []
    page.warn = lambda title, text: warnings.append(text)
    t0 = time.perf_counter()
    qtbot.mouseClick(page.delete_btn, Qt.MouseButton.LeftButton)
    assert time.perf_counter() - t0 < 1.0 and warnings == []
    assert not copy.exists() and page.table.rowCount() == 0
    assert page.status.text() == f"Deleted {copy.name}"
    assert src.exists() and ft.sha256_file(src) == digest
    assert window.pages["dashboard"].card_clips.value.text() == "0"


def test_delete_while_playing_releases_file(qtbot, window, test_clip):
    page = window.pages["results"]
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    player = page.preview.player
    page.preview.toggle()
    qtbot.waitUntil(lambda: player.position() > 500, timeout=10000)
    page.confirm = lambda _t: True
    page.warn = lambda title, text: pytest.fail(text)
    page.delete_selected()                       # no Close first: delete must release the file itself
    assert not copy.exists() and page.table.rowCount() == 0
    assert page.preview.source_path == ""


def test_open_and_show_in_folder_buttons(qtbot, window, test_clip):
    page = window.pages["results"]
    page.launcher = RecordingLauncher()
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    qtbot.mouseClick(page.open_btn, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(page.folder_btn, Qt.MouseButton.LeftButton)
    assert page.launcher.calls == [("open", copy), ("reveal", copy)]     # the copy, never the original

    # errors from Windows are shown, not raised
    class Failing:
        def open(self, path):
            raise OSError("No application is associated with the specified file")
        reveal = open
    page.launcher = Failing()
    shown = []
    page.warn = lambda title, text: shown.append((title, text))
    page.open_selected()
    page.show_in_folder()
    assert [t for t, _ in shown] == ["Couldn't open the clip", "Couldn't show the clip in its folder"]

    # when the file is gone: Open is disabled; Show in folder stays available (opens the folder)
    page.preview.release()
    copy.unlink()
    page.refresh()
    page.table.selectRow(0)
    assert not page.open_btn.isEnabled() and page.folder_btn.isEnabled() and page.delete_btn.isEnabled()
    assert "(file missing)" in page.table.item(0, 0).text()


def test_system_launcher_commands(monkeypatch, tmp_path):
    started, popened, selected = [], [], []
    monkeypatch.setattr(results_mod.os, "startfile", lambda p: started.append(p), raising=False)
    monkeypatch.setattr(results_mod.subprocess, "Popen", lambda cmd, *a, **k: popened.append(cmd))
    monkeypatch.setattr(results_mod.sys, "platform", "win32")
    target = tmp_path / "my clips, part 1" / "clip (2).mp4"
    launcher = results_mod.SystemLauncher()
    launcher.open(target)
    assert started == [str(target)]
    # preferred: the Shell API selects the file
    monkeypatch.setattr(results_mod, "shell_select", lambda p: selected.append(p) or True)
    launcher.reveal(target)
    assert selected == [target] and popened == []
    # fallback when the API fails: explorer /select with the path quoted
    monkeypatch.setattr(results_mod, "shell_select", lambda p: False)
    launcher.reveal(target)
    assert popened == [f'explorer /select,"{target}"']


def test_shell_select_rejects_missing_path(tmp_path):
    if results_mod.sys.platform != "win32":
        pytest.skip("Windows only")
    assert results_mod.shell_select(tmp_path / "does-not-exist" / "x.mp4") is False   # no window opened


def test_import_button_uses_file_dialog_and_handles_cancel(qtbot, window, test_clip):
    page = window.pages["results"]
    page.choose_file = lambda: None                      # user cancelled the dialog
    assert page.import_video() is None and page.table.rowCount() == 0
    page.choose_file = lambda: str(test_clip[0])
    with qtbot.waitSignal(page.ctx.events.clips_changed, timeout=30000):
        qtbot.mouseClick(page.import_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.import_btn.isEnabled(), timeout=10000)
    assert page.table.rowCount() == 1


def test_import_failure_is_reported(qtbot, window, tmp_path):
    page = window.pages["results"]
    bogus = tmp_path / "broken.mp4"
    bogus.write_bytes(b"\x00" * 1000)
    shown = []
    page.warn = lambda title, text: shown.append((title, text))
    job = page.import_video(bogus)
    with qtbot.waitSignal(job.signals.failed, timeout=30000):
        pass
    qtbot.waitUntil(lambda: page.import_btn.isEnabled(), timeout=5000)
    assert shown and shown[0][0] == "Import failed" and "broken.mp4" in shown[0][1]
    assert page.table.rowCount() == 0 and page.status.text() == "Import failed"


def test_cli_import(app_home, test_clip, capsys):
    from twitchclipper.cli import main
    from twitchclipper.storage.paths import app_paths

    assert main(["import", str(test_clip[0])]) == 0
    assert "imported" in capsys.readouterr().out
    db = Database(app_paths().database)
    rows = db.clips()
    assert len(rows) == 1 and rows[0]["source"] == "import" and Path(rows[0]["path"]).exists()
    assert Path(rows[0]["path"]).parent == app_paths().default_clips_dir / "imported"
    db.close()


def test_fixture_is_read_only(test_clip):
    path, _ = test_clip
    if path.parent.name == "test_media":
        assert not os.stat(path).st_mode & stat.S_IWRITE, "test_media fixture should stay read-only"
