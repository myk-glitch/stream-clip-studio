"""Optional FFmpeg download: checksum verification and safe extraction (mocked download)."""

import hashlib
import io
import zipfile

import httpx
import pytest

from twitchclipper.export import ffmpeg_setup as fs


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _build(payload: bytes, sha: str | None = None) -> fs.PinnedBuild:
    return fs.PinnedBuild("9.9.9", "https://example.invalid/ffmpeg.zip", sha or hashlib.sha256(payload).hexdigest(),
                          len(payload), "GPLv3", "https://ffmpeg.org/releases/x.tar.xz", "https://example.invalid/")


def _transport(payload: bytes):
    return httpx.MockTransport(lambda req: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))}))


def test_install_verifies_and_extracts(tmp_path):
    payload = _zip({"ffmpeg-9.9.9-essentials_build/bin/ffmpeg.exe": b"MZ fake", "ffmpeg-9.9.9-essentials_build/LICENSE": b"GPL"})
    seen = []
    exe = fs.install(tmp_path, _build(payload), progress=lambda d, t: seen.append((d, t)), transport=_transport(payload))
    assert exe == tmp_path / "9.9.9" / "bin" / "ffmpeg.exe" and exe.read_bytes() == b"MZ fake"
    assert (tmp_path / "9.9.9" / "TWITCHCLIPPER_LICENSE_NOTE.txt").read_text().startswith("FFmpeg is a separate program")
    assert seen and seen[-1][0] == len(payload)
    assert not list(tmp_path.glob("*.part"))


def test_checksum_mismatch_leaves_nothing(tmp_path):
    payload = _zip({"x/bin/ffmpeg.exe": b"tampered"})
    with pytest.raises(fs.InstallError, match="checksum mismatch"):
        fs.install(tmp_path, _build(payload, sha="0" * 64), transport=_transport(payload))
    assert not (tmp_path / "9.9.9").exists() and not list(tmp_path.glob("*.part"))


def test_unsafe_archive_paths_rejected(tmp_path):
    payload = _zip({"../../evil.exe": b"x", "x/bin/ffmpeg.exe": b"y"})
    with pytest.raises(fs.InstallError, match="unsafe"):
        fs.install(tmp_path, _build(payload), transport=_transport(payload))
    assert not (tmp_path.parent / "evil.exe").exists()


def test_pinned_constant_matches_known_release():
    assert fs.PINNED.sha256 == "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
    assert fs.PINNED.url.endswith("ffmpeg-9.0.2-essentials_build.zip")
