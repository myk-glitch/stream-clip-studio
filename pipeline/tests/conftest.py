"""Every test runs with its own settings and no access to real credentials, keychains or the network."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import settings  # noqa: E402
import twitch_api  # noqa: E402


class Response:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code, self._data, self.text, self.headers = status, data, text, headers or {}

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("CLIPPER_ENV", str(tmp_path / ".env"))
    for name in (*settings.SECRET_NAMES, "YOUTUBE_CLIENT_SECRET_FILE"):
        monkeypatch.delenv(name, raising=False)
    keychain = {}
    monkeypatch.setattr(settings, "keychain_get", lambda name: keychain.get(name, ""))
    monkeypatch.setattr(settings, "keychain_set", lambda name, value: keychain.__setitem__(name, value) or True)
    monkeypatch.setattr(settings, "keychain_delete", lambda name: keychain.pop(name, None) is not None)

    def blocked(*a, **k):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
    twitch_api._token = None
    settings.reload()
    yield keychain
    settings._config = None


@pytest.fixture
def configure(tmp_path):
    """Write a config.toml whose folders are inside this test's temp directory."""
    def write(extra=""):
        clips, logs, state = (tmp_path / n for n in ("clips", "logs", "state"))
        text = f'[paths]\nclips_dir = "{clips.as_posix()}"\nlogs_dir = "{logs.as_posix()}"\nstate_dir = "{state.as_posix()}"\n{extra}'
        (tmp_path / "config.toml").write_text(text, encoding="utf-8")
        return settings.reload()
    return write
