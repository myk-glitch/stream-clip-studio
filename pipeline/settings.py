"""User configuration.

Preferences live in config.toml. Secrets come from the environment, a .env file or the OS keychain, in that
order, and are never written to config.toml or to logs. Run `python configure.py init` to create both files.
Set CLIPPER_CONFIG or CLIPPER_ENV to keep them somewhere else.
"""
import copy, os, tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYRING_SERVICE = "stream-clip-studio"
SECRET_NAMES = ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "YOUTUBE_API_KEY", "DISCORD_WEBHOOK_ASK", "DISCORD_WEBHOOK_TOPOST",
                "DISCORD_WEBHOOK_POSTED", "DISCORD_WEBHOOK_LIVE", "DISCORD_WEBHOOK_RECAP")
PLACEHOLDER_PREFIX = "your_"

DEFAULTS = {
    "brand": {"name": "Stream Clip Studio"},
    "paths": {"clips_dir": "clips", "logs_dir": "logs", "state_dir": ".state", "sfx_dir": "sfx"},
    "game": {"name": "", "tag": ""},
    "watcher": {"model": "base", "before": 30, "after": 10, "max_extend": 25, "cooldown": 180, "hours": 12, "extra_args": []},
    "posting": {"privacy": "private", "max_posts_per_day": 2, "min_hours_between_posts": 4.0,
                "title_template": "{hook} #{tag} #{game_tag}", "description_footer": "",
                "youtube_tags": ["{tag}", "{game_tag}", "Shorts", "gaming"],
                "tiktok_tags": ["{tag}", "{game_tag}", "gaming", "fyp"],
                "default_tags": ["gaming", "streamer"], "youtube_category_id": "20"},
    "auto_post": {"enabled": False, "start_hour": 9, "end_hour": 22},
    "tiktok": {"mode": "manual", "profile": "", "autotok_account": "", "autotok_path": ""},
    "unofficial": {"twitch_chat_replay": False, "tiktok_autotok": False, "tiktok_profile_stats": False},
    "streamers": {},
    "campaigns": {},
}
STREAMER_DEFAULTS = {"tag": "", "display_name": "", "color": "#5865F2", "emoji": "🎮", "campaign": "", "cta": "", "watch": True,
                     "min_msgs": 0, "cam": [], "game": [0, 0, 1280, 720]}
CAMPAIGN_DEFAULTS = {"name": "", "match": "", "kind": "cpm", "rate_per_1k": 0.0, "min_views": 0, "pool_threshold": 0}

_config = None
_env_loaded = False


class ConfigError(SystemExit):
    """A configuration problem, shown to the user as a plain message."""


def config_path() -> Path:
    return Path(os.environ.get("CLIPPER_CONFIG") or HERE / "config.toml").expanduser()


def env_path() -> Path:
    return Path(os.environ.get("CLIPPER_ENV") or HERE / ".env").expanduser()


def _merge_section(name, defaults, values, where):
    unknown = set(values) - set(defaults)
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}")
    return {**copy.deepcopy(defaults), **values}


def _validate(cfg):
    p, a, t = cfg["posting"], cfg["auto_post"], cfg["tiktok"]
    if p["privacy"] not in ("private", "unlisted", "public"):
        raise ConfigError("config: posting.privacy must be private, unlisted or public")
    if p["max_posts_per_day"] < 1 or p["min_hours_between_posts"] < 0:
        raise ConfigError("config: posting.max_posts_per_day must be at least 1 and min_hours_between_posts can't be negative")
    if not (0 <= a["start_hour"] <= 23 and 0 <= a["end_hour"] <= 24):
        raise ConfigError("config: auto_post hours must be between 0 and 24")
    if t["mode"] not in ("manual", "autotok"):
        raise ConfigError("config: tiktok.mode must be manual or autotok")
    if t["mode"] == "autotok" and not cfg["unofficial"]["tiktok_autotok"]:
        raise ConfigError("config: tiktok.mode = autotok also needs unofficial.tiktok_autotok = true (see docs/configuration.md)")
    for login, s in cfg["streamers"].items():
        if login != login.lower():
            raise ConfigError(f"config: streamer '{login}' must be the lowercase Twitch login")
        for box in ("cam", "game"):
            if s[box] and (len(s[box]) != 4 or not all(isinstance(v, int) for v in s[box])):
                raise ConfigError(f"config: streamers.{login}.{box} must be [x, y, width, height] in whole pixels")
        try:
            int(s["color"].lstrip("#"), 16)
        except ValueError:
            raise ConfigError(f"config: streamers.{login}.color must be a hex colour like #9146FF") from None
    for key, c in cfg["campaigns"].items():
        if c["kind"] not in ("cpm", "pool"):
            raise ConfigError(f"config: campaigns.{key}.kind must be cpm or pool")


def load(path=None) -> dict:
    """Read and validate config.toml on top of the defaults. A missing file means all defaults."""
    path = Path(path) if path else config_path()
    raw = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path.name}: {e}") from None
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"{path.name}: unknown section(s): {', '.join(sorted(unknown))}")
    cfg = {}
    for name, defaults in DEFAULTS.items():
        if name not in ("streamers", "campaigns"):
            cfg[name] = _merge_section(name, defaults, raw.get(name, {}), path.name)
    cfg["streamers"] = {}
    for login, values in raw.get("streamers", {}).items():
        s = _merge_section(f"streamers.{login}", STREAMER_DEFAULTS, values, path.name)
        s["tag"] = s["tag"] or login.upper()
        s["display_name"] = s["display_name"] or login
        cfg["streamers"][login] = s
    cfg["campaigns"] = {}
    for key, values in raw.get("campaigns", {}).items():
        c = _merge_section(f"campaigns.{key}", CAMPAIGN_DEFAULTS, values, path.name)
        c["name"] = c["name"] or key
        c["match"] = (c["match"] or key).lower()
        cfg["campaigns"][key] = c
    _validate(cfg)
    return cfg


def get() -> dict:
    global _config
    if _config is None:
        _config = load()
    return _config


def reload() -> dict:
    """Forget cached settings (after editing the file, and in tests)."""
    global _config, _env_loaded
    _config, _env_loaded = None, False
    return get()


def path(key: str) -> Path:
    """A folder from [paths]. Relative paths are inside the pipeline folder."""
    p = Path(get()["paths"][key]).expanduser()
    return p if p.is_absolute() else HERE / p


def streamers() -> dict:
    return get()["streamers"]


def streamer_by_tag(tag) -> dict | None:
    wanted = (tag or "").lstrip("#").lower()
    return next((s for login, s in streamers().items() if wanted and wanted in (s["tag"].lower(), login)), None)


def unofficial(feature: str) -> bool:
    return bool(get()["unofficial"][feature])


def game_matches(name) -> bool:
    """True when no target game is configured, or the Twitch category contains it."""
    target = get()["game"]["name"].lower()
    return not target or target in (name or "").lower()


def _load_env():
    global _env_loaded
    if not _env_loaded:
        _env_loaded = True
        if env_path().exists():
            from dotenv import load_dotenv
            load_dotenv(env_path(), override=False)


def env(name: str) -> str:
    """A plain environment or .env value; unedited example placeholders count as empty."""
    _load_env()
    value = os.environ.get(name, "").strip()
    return "" if value.startswith(PLACEHOLDER_PREFIX) else value


def secret(name: str) -> str:
    """A credential from the environment, .env or the OS keychain. Returns "" when it isn't set."""
    return env(name) or keychain_get(name)


def keychain_get(name: str) -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, name) or ""
    except Exception:  # no keychain on this machine (for example a headless server)
        return ""


def keychain_set(name: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, name, value)
        return True
    except Exception:
        return False


def keychain_delete(name: str) -> bool:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, name)
        return True
    except Exception:
        return False


def youtube_client_file() -> Path:
    """The user's own Google OAuth client file, kept outside the repository by default."""
    configured = env("YOUTUBE_CLIENT_SECRET_FILE")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "stream-clip-studio" / "client_secret.json"
