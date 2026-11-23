# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First public release.

### Added
- **Pipeline:** live chat-spike clipping, local Whisper captions, vertical and face-on-top editing, VOD tools, YouTube Shorts upload, TikTok post packs, posting limits and queue, Discord notifications, live alerts and a daily recap.
- **Desktop app:** local video and chat analysis, clip export, rolling capture with Clip that, and a clip library.
- User configuration through `config.toml`, with credentials in `.env` or the OS keychain.
- `configure.py` for setup, health checks and keychain storage.
- Optional automatic posting with `post_queue.py` and a Windows scheduling script.
- Automated tests, a credential scan and GitHub Actions CI.

### Changed
- Live alerts, top clips and VOD listing use the official Twitch API with each user's own app.
- Recap view counts use the official YouTube Data API.
- Uploads default to private.

### Security
- Unofficial integrations are off by default and require explicit opt-in.
- Discord, Twitch and YouTube errors no longer include credentials.
