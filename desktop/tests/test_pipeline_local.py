"""Mode F end to end on generated footage: analysis finds the known events, exports are frame-exact."""

import pytest

from twitchclipper.config.settings import Settings
from twitchclipper.export import ffmpeg as ft
from twitchclipper.pipeline import local as pipeline
from twitchclipper.timing.window import Window
from tests.support.footage import decode_file


def _analyze(media, inst, offset=0.0, limit=5):
    s = Settings()
    return pipeline.analyze(inst.ffprobe, media["video"], media["chat"], offset, limit,
                            s.detector_config(), s.timing_config())


def test_analysis_finds_known_events(sample_media, ffmpeg_install):
    res = _analyze(sample_media, ffmpeg_install)
    assert abs(res.duration_s - 120.0) < 0.2
    assert len(res.highlights) == 3
    for e in sample_media["events"]:
        # each true event must be inside one highlight's clip window
        assert any(h.window_start <= e <= h.window_end for h in res.highlights), e
    for h in res.highlights:
        assert h.window_end - h.window_start == pytest.approx(27.0)
        assert h.event_t == pytest.approx(h.burst_start - 8.0)
    assert [h.rank for h in res.highlights] == [1, 2, 3]
    assert len(res.times) == len(res.scores) > 100


def test_clip_limit_and_offset_warning(sample_media, ffmpeg_install):
    res = _analyze(sample_media, ffmpeg_install, limit=1)
    assert len(res.highlights) == 1 and res.candidates == 3
    shifted = _analyze(sample_media, ffmpeg_install, offset=-60.0)
    assert any("outside the video" in w for w in shifted.warnings)
    with pytest.raises(ValueError, match="no chat messages"):
        _analyze(sample_media, ffmpeg_install, offset=10_000.0)


@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_export_is_frame_exact(sample_media, ffmpeg_install, tmp_path, orientation):
    inst = ffmpeg_install
    win = Window(33.0, 60.0)
    out = tmp_path / f"clip_{orientation}.mp4"
    progress = []
    r = pipeline.export(inst.ffmpeg, inst.ffprobe,
                        pipeline.ExportJob(sample_media["video"], win, out, orientation, 0.5, ft.CPU_ENCODER),
                        lambda f, m: progress.append(f))
    assert out.exists() and not list(tmp_path.glob("*.part.*"))
    assert r.duration_s == pytest.approx(27.0, abs=0.06)
    assert progress and progress[-1] > 0.9
    if orientation == "portrait":
        assert (r.width, r.height) == (1080, 1920)
    else:
        assert (r.width, r.height) == (640, 360)
        dec = decode_file(inst.ffmpeg, out)
        assert dec.frames == 810
        assert dec.first_ms == 33000                # exact first frame
        assert dec.last_ms == 60000 - 33 or dec.last_ms == 59967


def test_export_with_hardware_encoder_if_available(sample_media, ffmpeg_install, tmp_path):
    from twitchclipper.export.hwaccel import detect
    rep = detect(ffmpeg_install)
    hw = [t["name"] for t in rep.tests if t["works"] and t["name"] != "libx264"]
    if not hw:
        pytest.skip("no working hardware encoder on this PC")
    out = tmp_path / "hw.mp4"
    r = pipeline.export(ffmpeg_install.ffmpeg, ffmpeg_install.ffprobe,
                        pipeline.ExportJob(sample_media["video"], Window(60.0, 87.0), out, "landscape", 0.5, hw[0]))
    dec = decode_file(ffmpeg_install.ffmpeg, out)
    assert r.encoder == hw[0] and dec.first_ms == 60000 and abs(dec.frames - 810) <= 1


def test_export_cancel_removes_partial_file(sample_media, ffmpeg_install, tmp_path):
    out = tmp_path / "cancel.mp4"
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(InterruptedError):
        pipeline.export(ffmpeg_install.ffmpeg, ffmpeg_install.ffprobe,
                        pipeline.ExportJob(sample_media["video"], Window(0, 27), out), cancel=cancel)
    assert not out.exists() and not list(tmp_path.glob("*"))


def test_clip_filename_is_safe():
    h = pipeline.Highlight(2, 0, 0, 1, 5, 3725.0, 0, 27)
    from pathlib import Path
    name = pipeline.clip_filename(Path('C:/x/my:stream?.mp4'), h, "portrait")
    assert name == "my_stream__#2_01h02m05s_9x16.mp4"
