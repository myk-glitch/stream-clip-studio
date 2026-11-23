# Configuration reference

Preferences live in `pipeline/config.toml`, which is created from [`config.example.toml`](../pipeline/config.example.toml) by `python configure.py init`. Credentials are separate: see [the credentials guide](credentials.md).

Misspelled or invalid settings stop the program with a clear message. Check your file at any time:

```powershell
python configure.py doctor
```

To keep your files elsewhere, set the environment variables `CLIPPER_CONFIG` and `CLIPPER_ENV` to their full paths.

## Settings

### `[brand]`
| Setting | Default | Meaning |
|---|---|---|
| `name` | `Stream Clip Studio` | Name on Discord messages |

### `[paths]`
Relative paths are inside the `pipeline` folder.

| Setting | Default | Meaning |
|---|---|---|
| `clips_dir` | `clips` | Clips, queue, TikTok packs and the post log |
| `logs_dir` | `logs` | Watcher logs |
| `state_dir` | `.state` | Alert state and VOD scans |
| `sfx_dir` | `sfx` | Your sound effects |

### `[game]`
| Setting | Default | Meaning |
|---|---|---|
| `name` | empty | Only auto-watch this Twitch category. Empty means any game |
| `tag` | empty | Hashtag for that game, without `#` |

### `[watcher]`
Used when `live_alert.py` starts a watcher.

| Setting | Default | Meaning |
|---|---|---|
| `model` | `base` | Whisper model: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `before` / `after` | `30` / `10` | Seconds kept around a spike |
| `max_extend` | `25` | Extra seconds while chat stays hot |
| `cooldown` | `180` | Minimum seconds between clips |
| `hours` | `12` | Stop watching after this long |
| `extra_args` | `[]` | More `live_clipper.py` options, such as `["--realism"]` |

See every watcher option with `python live_clipper.py --help`.

### `[posting]`
| Setting | Default | Meaning |
|---|---|---|
| `privacy` | `private` | `private`, `unlisted` or `public` |
| `max_posts_per_day` | `2` | Daily limit for public posts |
| `min_hours_between_posts` | `4` | Minimum spacing |
| `title_template` | `{hook} #{tag} #{game_tag}` | YouTube title. Falls back to the hook above 100 characters |
| `description_footer` | empty | Extra line in every YouTube description |
| `youtube_tags` | `{tag}, {game_tag}, Shorts, gaming` | YouTube tags |
| `tiktok_tags` | `{tag}, {game_tag}, gaming, fyp` | TikTok hashtags |
| `default_tags` | `gaming, streamer` | Tags when a post has no `--streamer` |
| `youtube_category_id` | `20` | YouTube category (20 is Gaming) |

Templates can use `{hook}`, `{tag}` and `{game_tag}`. Empty hashtags are removed automatically.

### `[auto_post]`
| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Allow `post_queue.py` to publish |
| `start_hour` / `end_hour` | `9` / `22` | Local posting window. It may cross midnight, such as `22` to `6` |

### `[tiktok]`
| Setting | Default | Meaning |
|---|---|---|
| `mode` | `manual` | `manual` makes a pack you post. `autotok` needs the unofficial option |
| `profile` | empty | Your public handle, for the optional recap stats |
| `autotok_account` | empty | Account name inside autotok |
| `autotok_path` | empty | Path to autotok if it isn't on your PATH |

### `[unofficial]`
All are **off by default**. These features can stop working at any time and may violate a platform's terms. Turn one on only if you accept that risk.

| Setting | Enables |
|---|---|
| `twitch_chat_replay` | `vod.py scan`, exact chat matching in `rewind.py`, and game names in `vod.py list`, through Twitch's private web API |
| `tiktok_autotok` | Automatic TikTok upload with the third-party autotok tool |
| `tiktok_profile_stats` | Followers and likes in the recap, read from your public TikTok page |

### `[streamers.<login>]`
Add one block per channel. `<login>` is the lowercase name in their Twitch URL.

| Setting | Default | Meaning |
|---|---|---|
| `tag` | login in capitals | Hashtag and the value for `--streamer` |
| `display_name` | login | Name on Discord |
| `color` / `emoji` | `#5865F2` / 🎮 | Discord styling |
| `campaign` | empty | Optional label on Discord |
| `cta` | empty | Line added to captions automatically |
| `watch` | `true` | Include in live alerts and auto-watch |
| `min_msgs` | `0` | Spike floor for this channel. `0` uses the watcher's default of 20 |
| `cam` | empty | Facecam box `[x, y, width, height]` |
| `game` | `[0, 0, 1280, 720]` | Gameplay box |

### `[campaigns.<key>]` (optional)
Tracks paid clipping programs in the daily recap. A post counts when its title contains `match`.

| Setting | Meaning |
|---|---|
| `name` | Label in the recap |
| `match` | Word to find in post titles (defaults to the key) |
| `kind` | `cpm` (paid per 1,000 views) or `pool` (share of a prize pool) |
| `rate_per_1k` | Payment per 1,000 views for `cpm` |
| `min_views` | Clips below this pay nothing |
| `pool_threshold` | Total views needed for a `pool` |

Recap earnings are estimates. The program's own dashboard is the real number.

## Finding the camera and gameplay boxes

Boxes use a **1280x720** frame, even for 1080p clips.

1. Export one frame from a clip:

   ```powershell
   ffmpeg -ss 5 -i clips\my_clip\clip_16x9.mp4 -frames:v 1 -vf scale=1280:720 frame.png
   ```

2. Open `frame.png` in an image editor and note the top-left corner, width and height of the facecam, then of the gameplay area.
3. Save them:

   ```toml
   [streamers.their_twitch_login]
   cam = [0, 0, 384, 216]
   game = [400, 130, 700, 560]
   ```

Each box is center-cropped to fit its half of the video. If a streamer goes full-camera, add `--full` to `split_layout.py`.

## Scheduling

Three scripts are meant to run on a timer:

| Script | Suggested interval | Job |
|---|---|---|
| `live_alert.py` | Every 2 minutes | Alerts and starts watchers |
| `post_queue.py` | Every 30 minutes | Publishes the next queued clip when allowed |
| `recap.py` | Daily | Sends the recap |

**Windows:**

```powershell
.\schedule_windows.ps1 -Alerts -AutoPost -Recap -RecapTime 21:00
```

Add `-DryRun` to preview, or use `-Remove` to delete the tasks.

**Linux or macOS:** run `crontab -e` and add these lines with your own path:

```cron
*/2 * * * * cd /path/to/stream-clip-studio/pipeline && .venv/bin/python live_alert.py >> logs/cron.log 2>&1
*/30 * * * * cd /path/to/stream-clip-studio/pipeline && .venv/bin/python post_queue.py >> logs/cron.log 2>&1
0 21 * * * cd /path/to/stream-clip-studio/pipeline && .venv/bin/python recap.py >> logs/cron.log 2>&1
```

Create the `logs` folder first. Schedules run only while your computer is on.

## Adding clips to the queue

When the limit is reached, `post_everywhere.py` queues public clips automatically. To queue a clip yourself, put `name.mp4` in `clips/QUEUE/` with a `name.json` beside it:

```json
{"hook": "He did NOT see that coming", "streamer": "THEIRNAME", "cta": "", "queued_at": "2026-01-01T12:00:00"}
```

The oldest `queued_at` posts first. Posted clips move to `clips/POSTED/`.
