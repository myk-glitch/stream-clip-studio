"""Desktop reliability: file release before delete, delete safety, error handling for Open /
Show in folder, unique export names. Destructive tests use temporary copies only."""

import os
import shutil
import stat
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QMediaPlayer

from tests.support.helpers import RecordingLauncher, import_via_ui
from twitchclipper.core.context import create_context
from twitchclipper.export import ffmpeg as ft
from twitchclipper.pipeline import library
from twitchclipper.storage.db import Database
from twitchclipper.ui import theme
from twitchclipper.ui.main_window import MainWindow
from twitchclipper.ui.pages import results as results_mod
from twitchclipper.ui.widgets.video_preview import VideoPreview


@pytest.fixture
def window(qtbot, qapp, app_home):
    theme.apply(qapp, dark=False)
    win = MainWindow(create_context(), detect_encoders=False)
    qtbot.addWidget(win)
    win.show()
    win.navigate("results")
    yield win
    for key in ("results", "analyze"):
        win.pages[key].preview.release()
    win.runner.wait(30000)


@pytest.fixture(autouse=True)
def fixture_untouched(test_clip):
    path, digest = test_clip
    yield
    assert path.exists() and ft.sha256_file(path) == digest, "the original test fixture changed"


def wait_loaded(qtbot, preview):
    qtbot.waitUntil(lambda: preview.player.mediaStatus() in (
        QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia), timeout=10000)


def collect_warnings(page):
    shown = []
    page.warn = lambda title, text: shown.append((title, text))
    return shown


# --- 1. file release ---------------------------------------------------------

def test_delete_releases_file_held_by_another_preview(qtbot, window, test_clip):
    """The Analyze page's player holding the same file used to block the delete (WinError 32)."""
    page, analyze = window.pages["results"], window.pages["analyze"]
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    analyze.preview.load(copy)                   # e.g. the imported copy chosen as an analysis source
    wait_loaded(qtbot, analyze.preview)
    analyze.preview.toggle()                     # and playing
    qtbot.waitUntil(lambda: analyze.preview.player.position() > 300, timeout=10000)
    shown = collect_warnings(page)
    page.confirm = lambda _t: True
    t0 = time.perf_counter()
    page.delete_selected()
    assert shown == [] and not copy.exists() and page.table.rowCount() == 0
    assert time.perf_counter() - t0 < 1.0
    assert analyze.preview.source_path == "" and analyze.preview.info.text() == "No clip loaded"


def test_release_everywhere_only_touches_matching_previews(qtbot, tmp_path, test_clip):
    a_file, b_file = tmp_path / "a.mp4", tmp_path / "b.mp4"
    shutil.copyfile(test_clip[0], a_file)
    shutil.copyfile(test_clip[0], b_file)
    p1, p2, p3 = VideoPreview(), VideoPreview(), VideoPreview()
    for w in (p1, p2, p3):
        qtbot.addWidget(w)
    p1.load(a_file)
    p2.load(Path(str(a_file).upper()))            # same file, different spelling (Windows is case-insensitive)
    p3.load(b_file)
    gone = VideoPreview()
    gone.deleteLater()                            # a destroyed preview must not break the broadcast
    qtbot.wait(50)
    assert VideoPreview.release_everywhere(a_file) == 2
    assert p1.source_path == "" and p2.source_path == "" and Path(p3.source_path) == b_file
    os.remove(a_file)                             # released → deletable
    p3.release()


def test_delete_refused_while_a_job_reads_the_file(qtbot, window, test_clip):
    page = window.pages["results"]
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    shown = collect_warnings(page)
    asked = []
    page.confirm = lambda t: asked.append(t) or True
    with window.ctx.using_file(copy):            # what an export / frame extraction does while running
        assert window.ctx.file_in_use(copy)
        page.delete_selected()
    assert shown and shown[0][0] == "Clip is in use" and asked == []     # refused before asking
    assert copy.exists() and page.table.rowCount() == 1
    assert not window.ctx.file_in_use(copy)
    page.delete_selected()
    assert not copy.exists() and page.table.rowCount() == 0


def test_using_file_is_reentrant(app_home):
    ctx = create_context()
    p = app_home / "x.mp4"
    with ctx.using_file(p):
        with ctx.using_file(p):
            pass
        assert ctx.file_in_use(p)
    assert not ctx.file_in_use(p)
    with pytest.raises(RuntimeError):
        with ctx.using_file(p):
            raise RuntimeError("job failed")
    assert not ctx.file_in_use(p)              # released even when the job fails
    ctx.db.close()


def test_delete_while_external_program_holds_file(qtbot, window, test_clip):
    """Python's open() on Windows doesn't allow delete sharing, like a player that locks the file."""
    page = window.pages["results"]
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    shown = collect_warnings(page)
    page.confirm = lambda _t: True
    with open(copy, "rb"):
        t0 = time.perf_counter()
        page.delete_selected()
        elapsed = time.perf_counter() - t0
    assert elapsed < 1.5, f"UI blocked for {elapsed:.1f} s"
    assert shown and shown[0][0] == "Couldn't delete file" and "close it and try again" in shown[0][1].lower()
    assert copy.exists() and page.table.rowCount() == 1                 # entry kept, nothing half-done
    page.delete_selected()                                              # handle closed → works
    assert not copy.exists() and page.table.rowCount() == 0


def test_read_only_file_is_not_forced(qtbot, window, test_clip):
    page = window.pages["results"]
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    os.chmod(copy, stat.S_IREAD)
    try:
        shown = collect_warnings(page)
        page.confirm = lambda _t: True
        page.delete_selected()
        assert shown and "read-only" in shown[0][1]
        assert copy.exists() and page.table.rowCount() == 1
        assert not os.stat(copy).st_mode & stat.S_IWRITE              # attribute left as the user set it
    finally:
        os.chmod(copy, stat.S_IREAD | stat.S_IWRITE)


# --- 2. delete safety: never delete unrelated source files ----------------------

def _row(db, path, **kw):
    return db.add_clip(path=str(path), orientation="landscape", window_start=0, window_end=1, **kw)


def test_plan_delete_protects_source_files(tmp_path):
    db = Database(tmp_path / "lib.db")
    src = tmp_path / "originals" / "my stream.mp4"
    src.parent.mkdir()
    src.write_bytes(b"original")
    copy = tmp_path / "clips" / "imported" / "my stream.mp4"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"copy")
    imp = _row(db, copy, source="import", original_path=str(src))
    assert library.plan_delete(db, imp) == library.DeletePlan(copy, True)

    # an entry that points at the original of an import (e.g. legacy / manual DB row)
    bad = _row(db, src)
    plan = library.plan_delete(db, bad)
    assert not plan.delete_file and "original" in plan.reason
    # same check with a different spelling of the path
    bad2 = _row(db, Path(str(src).upper()))
    assert not library.plan_delete(db, bad2).delete_file

    # two entries sharing one file: deleting one must not break the other
    shared = tmp_path / "clips" / "shared.mp4"
    shared.write_bytes(b"x")
    s1, s2 = _row(db, shared), _row(db, shared)
    assert "another entry" in library.plan_delete(db, s1).reason and not library.plan_delete(db, s2).delete_file

    # the source video of an analysis
    vid = tmp_path / "clips" / "analysed.mp4"
    vid.write_bytes(b"x")
    db.save_analysis("local", "a", str(vid), None, 0, 1, 0, {}, [])
    a = _row(db, vid)
    assert "source video" in library.plan_delete(db, a).reason

    # missing file / directory: only the entry goes
    assert library.plan_delete(db, _row(db, tmp_path / "gone.mp4")) == library.DeletePlan(
        tmp_path / "gone.mp4", False, "the file is already missing from disk")
    assert not library.plan_delete(db, _row(db, tmp_path / "clips")).delete_file
    with pytest.raises(KeyError):
        library.plan_delete(db, 99999)


def test_protected_file_is_kept_when_entry_removed(qtbot, window, tmp_path, test_clip):
    page = window.pages["results"]
    src = tmp_path / "source.mp4"
    shutil.copyfile(test_clip[0], src)
    clip_id = _row(window.ctx.db, src)
    window.ctx.db.save_analysis("local", "src", str(src), None, 0, 12, 0, {}, [])
    page.refresh()
    page.select_clip(clip_id)
    asked = []
    page.confirm = lambda t: asked.append(t) or True
    page.delete_selected()
    assert asked[0].startswith("Remove source.mp4 from the list?") and "stays on disk" in asked[0]
    assert src.exists() and page.table.rowCount() == 0
    assert page.status.text() == "Removed source.mp4 from the list"


def test_delete_cancel_changes_nothing(qtbot, window, test_clip):
    page = window.pages["results"]
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    wait_loaded(qtbot, page.preview)
    page.confirm = lambda _t: False
    page.delete_selected()
    assert copy.exists() and page.table.rowCount() == 1
    assert Path(page.preview.source_path) == copy          # cancelling doesn't close the preview


def test_unique_destination_reserves_queued_names(tmp_path):
    reserved = set()
    a = library.unique_destination(tmp_path, "clip.mp4", reserved)
    b = library.unique_destination(tmp_path, "clip.mp4", reserved)     # a isn't written yet
    (tmp_path / "other.mp4").write_bytes(b"")
    c = library.unique_destination(tmp_path, "other.mp4", reserved)
    assert [a.name, b.name, c.name] == ["clip.mp4", "clip (2).mp4", "other (2).mp4"]


def test_exporting_same_highlight_twice_keeps_both_clips(qtbot, window, sample_media):
    """Used to overwrite the first export and leave two library entries pointing at one file."""
    ctx = window.ctx
    ctx.settings.export.encoder = "libx264"
    page = window.pages["analyze"]
    page.video_edit.setText(str(sample_media["video"]))
    page.chat_edit.setText(str(sample_media["chat"]))
    page.start_analysis()
    qtbot.waitUntil(lambda: page.result is not None, timeout=60000)
    for r in range(1, page.table.rowCount()):
        page.table.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
    page.orientation.setCurrentIndex(page.orientation.findData("landscape"))
    page.export_checked()
    page.export_checked()                                  # queued again before the first one finished
    qtbot.waitUntil(lambda: len(ctx.db.clips()) == 2, timeout=120000)
    paths = [Path(c["path"]) for c in ctx.db.clips()]
    assert len(set(paths)) == 2 and all(p.exists() for p in paths)
    first = min((p.name for p in paths), key=len)
    assert {p.name for p in paths} == {first, first[:-4] + " (2).mp4"}
    assert not page._reserved_outputs                      # reservations released when jobs finish


# --- 4. Open / Show in folder -------------------------------------------------------

def test_open_when_file_vanished_after_selection(qtbot, window, test_clip):
    page = window.pages["results"]
    page.launcher = RecordingLauncher()
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    page.preview.release()
    copy.unlink()                                          # deleted outside the app, row still selected
    shown = collect_warnings(page)
    page.open_selected()
    assert page.launcher.calls == [] and shown[0][0] == "Clip not found"
    assert "(file missing)" in page.table.item(0, 0).text()   # list refreshed


def test_show_in_folder_when_file_missing_opens_folder(qtbot, window, test_clip):
    page = window.pages["results"]
    page.launcher = RecordingLauncher()
    copy = Path(import_via_ui(qtbot, page, test_clip[0])["path"])
    page.preview.release()
    copy.unlink()
    page.refresh()
    page.table.selectRow(0)
    assert page.folder_btn.isEnabled() and not page.open_btn.isEnabled()
    shown = collect_warnings(page)
    qtbot.mouseClick(page.folder_btn, Qt.MouseButton.LeftButton)
    assert page.launcher.calls == [("open", copy.parent)] and shown == []
    assert "no longer in this folder" in page.status.text()


def test_show_in_folder_when_folder_missing(qtbot, window, tmp_path):
    page = window.pages["results"]
    page.launcher = RecordingLauncher()
    clip_id = _row(window.ctx.db, tmp_path / "deleted folder" / "x.mp4")
    page.refresh()
    page.select_clip(clip_id)
    assert not page.folder_btn.isEnabled() and not page.open_btn.isEnabled() and page.delete_btn.isEnabled()
    shown = collect_warnings(page)
    page.show_in_folder()                                  # e.g. triggered anyway
    assert page.launcher.calls == [] and shown[0][0] == "Clip not found"


@pytest.mark.parametrize("exc, expected", [
    (OSError(None, "No application is associated", None, 1155), "Default apps"),
    (FileNotFoundError(2, "The system cannot find the file specified"), "no longer exists"),
    (PermissionError(13, "Access is denied"), "Windows couldn't open clip.mp4: Access is denied"),
])
def test_launch_errors_are_explained(qtbot, window, test_clip, exc, expected):
    page = window.pages["results"]
    import_via_ui(qtbot, page, test_clip[0])

    class Failing:
        def open(self, p):
            raise exc
        reveal = open

    page.launcher = Failing()
    shown = collect_warnings(page)
    page.open_selected()
    page.show_in_folder()
    assert [t for t, _ in shown] == ["Couldn't open the clip", "Couldn't show the clip in its folder"]
    msg = shown[0][1].replace("twitchclipper_test_pattern_12s.mp4", "clip.mp4")
    assert expected in msg


def test_dashboard_open_clips_folder(window, tmp_path):
    dash = window.pages["dashboard"]
    dash.launcher = RecordingLauncher()
    shown = collect_warnings(dash)
    window.ctx.settings.export.output_dir = str(tmp_path / "new clips")
    dash.open_clips_folder()                               # created on demand, then opened
    assert dash.launcher.calls == [("open", tmp_path / "new clips")] and shown == []
    blocker = tmp_path / "a file"
    blocker.write_text("x")
    window.ctx.settings.export.output_dir = str(blocker / "clips")   # can't be created (like a missing drive)
    dash.open_clips_folder()
    assert shown and shown[0][0] == "Couldn't open the clips folder" and "Settings" in shown[0][1]
    assert len(dash.launcher.calls) == 1


def test_reveal_falls_back_when_explorer_cannot_start(monkeypatch, tmp_path):
    monkeypatch.setattr(results_mod.sys, "platform", "win32")
    monkeypatch.setattr(results_mod, "shell_select", lambda p: False)

    def no_explorer(*a, **k):
        raise FileNotFoundError(2, "explorer not found")

    monkeypatch.setattr(results_mod.subprocess, "Popen", no_explorer)
    with pytest.raises(OSError):                            # surfaces to the page, which shows it
        results_mod.SystemLauncher().reveal(tmp_path / "x.mp4")
