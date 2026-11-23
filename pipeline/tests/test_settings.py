from pathlib import Path

import pytest

import settings

PIPELINE = Path(settings.__file__).parent


def test_defaults_are_safe_without_a_config_file():
    cfg = settings.get()
    assert cfg["posting"]["privacy"] == "private"
    assert cfg["auto_post"]["enabled"] is False
    assert cfg["tiktok"]["mode"] == "manual"
    assert not any(cfg["unofficial"].values())
    assert cfg["streamers"] == {}


def test_example_config_is_valid_and_keeps_risky_features_off():
    cfg = settings.load(PIPELINE / "config.example.toml")
    assert cfg["posting"]["privacy"] == "private"
    assert cfg["auto_post"]["enabled"] is False
    assert not any(cfg["unofficial"].values())
    assert cfg["streamers"]["example_streamer"]["watch"] is False


def test_example_env_only_has_known_names_and_placeholders(monkeypatch):
    names = {}
    for line in (PIPELINE / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            names[key] = value
    assert set(names) == {*settings.SECRET_NAMES, "YOUTUBE_CLIENT_SECRET_FILE"}
    assert all(v.startswith(settings.PLACEHOLDER_PREFIX) for v in names.values())
    for key, value in names.items():
        monkeypatch.setenv(key, value)
        assert settings.env(key) == ""            # an unedited placeholder counts as not set


@pytest.mark.parametrize("text", [
    '[nope]\nx = 1\n',
    '[posting]\ntypo = 1\n',
    '[posting]\nprivacy = "secret"\n',
    '[posting]\nmax_posts_per_day = 0\n',
    '[auto_post]\nstart_hour = 25\n',
    '[tiktok]\nmode = "autotok"\n',
    '[streamers.UPPER]\ntag = "X"\n',
    '[streamers.a]\ncam = [1, 2, 3]\n',
    '[streamers.a]\ncolor = "purple"\n',
    '[campaigns.a]\nkind = "other"\n',
    'not toml',
])
def test_bad_config_gives_a_clear_error(tmp_path, text):
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(settings.ConfigError):
        settings.load(path)


def test_secret_lookup_order(isolated, tmp_path, monkeypatch):
    assert settings.secret("TWITCH_CLIENT_SECRET") == ""
    isolated["TWITCH_CLIENT_SECRET"] = "from-keychain"
    assert settings.secret("TWITCH_CLIENT_SECRET") == "from-keychain"
    (tmp_path / ".env").write_text("TWITCH_CLIENT_SECRET=from-dotenv\n", encoding="utf-8")
    settings.reload()
    assert settings.secret("TWITCH_CLIENT_SECRET") == "from-dotenv"
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "from-environment")
    assert settings.secret("TWITCH_CLIENT_SECRET") == "from-environment"


def test_streamer_defaults_lookup_and_paths(configure, tmp_path):
    configure('[streamers.some_login]\ncam = [0, 0, 10, 10]\n')
    assert settings.streamer_by_tag("#SOME_LOGIN")["display_name"] == "some_login"
    assert settings.streamer_by_tag("some_login")["cam"] == [0, 0, 10, 10]
    assert settings.streamer_by_tag("missing") is None
    assert settings.path("clips_dir") == tmp_path / "clips"


def test_game_filter(configure):
    configure('[game]\nname = "Grand Theft Auto VI"\n')
    assert settings.game_matches("Grand Theft Auto VI")
    assert not settings.game_matches("Just Chatting")
    configure('[game]\nname = ""\n')
    assert settings.game_matches("Just Chatting")
