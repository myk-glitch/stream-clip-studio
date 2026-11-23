# Third-party notices

## Original project
The pipeline is based on [live-clipper](https://github.com/akm72-bit/live-clipper), Copyright (c) 2026 tokenstoviews, under the MIT License. Its notice is kept in [LICENSE](LICENSE).

## Desktop app dependencies

| Component | License | Notes |
|---|---|---|
| PySide6 (Qt for Python) | LGPLv3 | Installed as separate, unmodified wheels (dynamically linked) |
| pyqtgraph | MIT | |
| numpy | BSD-3-Clause | |
| httpx | BSD-3-Clause | |
| websockets | BSD-3-Clause | |
| keyring | MIT | |
| platformdirs | MIT | |
| tomli-w | MIT | |

## Pipeline dependencies
Installed separately from PyPI by `pipeline/requirements.txt`; none are bundled.

| Component | License |
|---|---|
| Streamlink | BSD-2-Clause |
| faster-whisper | MIT |
| requests | Apache-2.0 |
| keyring | MIT |
| psutil | BSD-3-Clause |
| python-dotenv | BSD-3-Clause |
| Google API Client, google-auth, google-auth-oauthlib | Apache-2.0 |

Whisper model files are downloaded on first use and have their own license terms.

## FFmpeg
- This project runs FFmpeg as a **separate program**; it does not bundle or link it.
- If you use the optional in-app download, it fetches the gyan.dev "essentials" build 9.0.2. That build is licensed under the **GNU GPL v3** because it includes libx264 and libx265.
  - Source code: https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
  - Build information: https://www.gyan.dev/ffmpeg/builds/
- If a future installer bundles FFmpeg, it must meet the GPL's source-distribution requirements.
