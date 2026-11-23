import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Real Windows fonts for the offscreen platform, so text sizes (and the layout tests) match the real app
# within a few pixels instead of using placeholder glyph boxes.
_win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
if _win_fonts.is_dir():
    os.environ.setdefault("QT_QPA_FONTDIR", str(_win_fonts))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from twitchclipper.export import ffmpeg as ft  # noqa: E402


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    """Isolated settings/database/clips folder for each test."""
    monkeypatch.setenv("TWITCHCLIPPER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture(scope="session")
def ffmpeg_install():
    inst = ft.best_install()
    if not inst:
        pytest.skip("ffmpeg not installed")
    return inst


@pytest.fixture(scope="session")
def test_clip(tmp_path_factory, ffmpeg_install):
    """The synthetic fixture test_media/twitchclipper_test_pattern_12s.mp4 (generated if missing).

    Returns (path, sha256). Tests only ever import *copies* of it and assert it is unchanged afterwards.
    """
    import importlib.util
    import subprocess

    spec = importlib.util.spec_from_file_location("make_test_media", ROOT / "scripts" / "make_test_media.py")
    mtm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtm)
    path = mtm.FIXTURE
    if not path.exists():
        path = tmp_path_factory.mktemp("fixture") / mtm.FIXTURE.name
        subprocess.run(mtm.build_command(ffmpeg_install.ffmpeg, path), check=True)
    return path, ft.sha256_file(path)


@pytest.fixture(scope="session")
def sample_media(tmp_path_factory, ffmpeg_install):
    """120 s of 640x360 footage with a burned-in clock, plus a chat log with 3 known events."""
    from tests.support.footage import generate
    from tests.support.sample_chat import write_sample_chat

    d = tmp_path_factory.mktemp("media")
    video = generate(ffmpeg_install.ffmpeg, d / "stream.mp4", 120.0, fps=30, width=640, height=360)
    chat, events = write_sample_chat(d / "chat.jsonl", duration_s=120.0, event_times=[40.0, 70.0, 100.0])
    return {"video": video, "chat": chat, "events": events, "dir": d}
