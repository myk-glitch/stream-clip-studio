# Troubleshooting and FAQ

Start with the health check. It never prints your secrets:

```powershell
python configure.py doctor --online
```

## Setup

**`python` or `py` isn't recognized.** Install Python 3.11 or 3.12 and enable **Add Python to PATH**, then open a new terminal.

**`ffmpeg` isn't recognized.** Install it, then open a new terminal. Check with `ffmpeg -version`.

**PowerShell blocks `Activate.ps1`.** You don't need to change system settings. Call the environment's Python directly:

```powershell
.\.venv\Scripts\python.exe configure.py doctor
```

**`ModuleNotFoundError`.** The environment isn't active, or dependencies are missing:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**"unknown setting" or another config error.** The message names the section and setting. Compare it with `config.example.toml`.

## Clipping

**No clips appear.** Check that the channel is live and chat is moving. For smaller channels, try `--min-msgs 8` or `--spike 2`. Start a few minutes early so the tool learns normal chat speed.

**Test the whole clip path right now:**

```powershell
python live_clipper.py their_twitch_login --test-clip-after 30
```

**Too many weak clips.** Raise `--spike` to 3 or 4, and increase `--cooldown`.

**Clips disappear into `clips/_rejected/auto`.** They were mostly letterboxed, which usually means a cutscene. Use `--cutscene-share 1.1` to turn this off.

**Captions are slow.** Use `--model tiny` or `base`, or `--no-captions`. An NVIDIA GPU is used automatically when available.

**The facecam crop is wrong.** Streamers change layouts. Update the boxes using [these steps](configuration.md#finding-the-camera-and-gameplay-boxes).

**`rewind.py` refuses to run.** Wait until the stream ends. In-progress VODs don't line up reliably by time.

## Twitch

**"Twitch rejected your app credentials."** Check for extra spaces, or create a new secret. The pipeline needs a **Confidential** app.

**"Chat replay uses Twitch's private web API."** `vod.py scan` is off by default. Read [the risks](configuration.md#unofficial) before enabling it.

**`vod.py list --game` finds nothing.** The official API has no game field for videos, so this searches titles. Leave `--game` out to see all videos.

**Streamlink errors or "no playable streams".** The channel may be offline, or Streamlink may need an update:

```powershell
python -m pip install -U streamlink
```

## YouTube

**"Missing client_secret.json".** Save your OAuth client file to the path shown in the message. See [the setup steps](credentials.md#youtube-uploads).

**"Access blocked" during sign-in.** Add your Google account as a **Test user** in your project's OAuth settings.

**"Your YouTube sign-in expired".** Google apps in Testing mode expire after 7 days. Run `python post_youtube.py auth`.

**"This Google account has no YouTube channel".** Create a channel on YouTube, then try again.

**The upload is private.** `posting.privacy` starts as `private` on purpose. Change it to `public` when you're ready. Account settings can also override it.

**Upload quota is used up.** Quotas are per Google project and reset at midnight Pacific Time.

**My clip was queued instead of posted.** You reached `max_posts_per_day` or `min_hours_between_posts`. Change those settings, wait, or use `--force` for one post.

**My Shorts get few views.** New channels often start slowly. Complete your profile, verify your phone number, post at a steady pace, and favor short clips with a clear payoff. No tool can guarantee reach.

## Discord

**`discord posted: not configured, skipped`.** That channel has no webhook. It's optional.

**HTTP 401 or 404.** The webhook was deleted or copied incorrectly. Create a new one.

## Scheduling

**Scheduled tasks don't run on Windows.** Open **Task Scheduler** and look for the **Stream Clip Studio** tasks. Tasks run while you're signed in and the computer is awake.

**Linux has no keychain.** On a headless machine, keep Twitch and Discord values in `.env`. YouTube sign-in requires a working keyring backend.

## FAQ

**Is it free?** The software is free under the MIT license. Standard free tiers cover normal personal use of the APIs.

**Does it send my data anywhere?** Only to services you connect. There's no telemetry.

**Can I use it for streamers other than Twitch?** Live clipping is built around Twitch chat and URLs.

**Can I clip anyone?** Technically the tool works on public streams, but you should only publish clips you have permission to use.

**Why are sound effects missing?** Their licenses vary, so you add your own to `pipeline/sfx/`.

**How do I update?**

```powershell
git pull
```

```powershell
python -m pip install -r requirements.txt
```

Your `config.toml`, `.env` and clips are ignored by git and stay in place.
