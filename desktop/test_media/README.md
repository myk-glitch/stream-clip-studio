# test_media: synthetic test clip

The sample clip isn't stored in the repository. Create it from the `desktop` folder:

```powershell
uv run --frozen python scripts/make_test_media.py
```

It makes `twitchclipper_test_pattern_12s.mp4`, generated locally with FFmpeg. It contains no Twitch content.

| Property | Value |
|---|---|
| Video | 1280×720, 30 fps, H.264, FFmpeg `testsrc2` colour bars |
| Overlay | "TwitchClipper TEST CLIP" with a running timestamp; frame counter at the bottom |
| Audio | AAC stereo 48 kHz: a 440 Hz tone with a short 1760 Hz beep at the start of every second |
| Length | 12.0 s, about 4.8 MB |

The file is marked read-only. The app and tests only import a *copy* of it, into `<clips folder>\imported\`. To regenerate it, add `--force`. If it's missing, the tests make a temporary one.
