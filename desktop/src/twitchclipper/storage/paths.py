"""Where TwitchClipper keeps its files on Windows.

  %APPDATA%\\TwitchClipper\\settings.toml        settings
  %LOCALAPPDATA%\\TwitchClipper\\twitchclipper.db  database
  %LOCALAPPDATA%\\TwitchClipper\\{buffer,work,logs,ffmpeg}
  %USERPROFILE%\\Videos\\TwitchClipper           finished clips (changeable)

Set TWITCHCLIPPER_HOME to put everything under one folder (used by tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_videos_dir

APP = "TwitchClipper"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    default_clips_dir: Path

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.toml"

    @property
    def database(self) -> Path:
        return self.data_dir / "twitchclipper.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def buffer_dir(self) -> Path:
        return self.data_dir / "buffer"

    def ensure(self) -> "AppPaths":
        for d in (self.config_dir, self.data_dir, self.work_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


def app_paths() -> AppPaths:
    home = os.environ.get("TWITCHCLIPPER_HOME")
    if home:
        base = Path(home)
        return AppPaths(base / "config", base / "data", base / "clips")
    return AppPaths(
        Path(user_config_dir(APP, appauthor=False, roaming=True)),
        Path(user_data_dir(APP, appauthor=False)),
        Path(user_videos_dir()) / APP,
    )
