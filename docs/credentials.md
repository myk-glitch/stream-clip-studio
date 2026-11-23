# Connecting your accounts

Everything here is optional unless you want that feature. You create your own apps, so nobody else's account or quota is involved.

**Security rules**
- This project never asks for your Twitch, Google, TikTok or Discord password. Sign-in happens on each company's own page.
- Keep credentials in `pipeline/.env` or your OS keychain. Never put them in `config.toml`, screenshots, issues or commits.
- `.env`, OAuth files and tokens are ignored by git. Run `python scripts/check_secrets.py` from the repository root before you publish a fork.

## Storing a credential

Choose one:

- **`.env` file:** open `pipeline/.env` and replace the `your_...` value.
- **OS keychain (more secure):** run this and paste the value at the hidden prompt:

  ```powershell
  python configure.py secret TWITCH_CLIENT_SECRET
  ```

Lookup order is environment variable, then `.env`, then keychain. To remove a keychain value, add `--delete`.

## Twitch

Needed for live alerts, top clips and VOD listing. Plain live clipping works without it.

1. Sign in at the [Twitch Developer Console](https://dev.twitch.tv/console/apps). Twitch may require two-factor authentication on your account.
2. Click **Register Your Application**.
3. Enter any unique name, set **OAuth Redirect URLs** to `http://localhost`, choose a category, and set **Client Type** to **Confidential**.
4. Open the app, copy the **Client ID**, click **New Secret**, and copy the secret.
5. Add both:

   ```dotenv
   TWITCH_CLIENT_ID=paste_client_id
   TWITCH_CLIENT_SECRET=paste_client_secret
   ```

6. Test it:

   ```powershell
   python configure.py doctor --online
   ```

The pipeline uses an app access token, which only reads public data and can't act as your account.

The **desktop app** uses a different sign-in. If you want it, register a second app with **Client Type: Public** and paste its Client ID on the app's **Account** page.

## YouTube uploads

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. Go to **APIs & Services > Library**, search for **YouTube Data API v3**, and click **Enable**.
3. Go to **Google Auth Platform** (or **OAuth consent screen**) and start setup. Choose **External**, and enter an app name and your contact email.
4. Under **Audience**, add the Google account that owns your channel as a **Test user**.
5. Under **Data Access**, add the scope `https://www.googleapis.com/auth/youtube.upload`.
6. Go to **Clients > Create client**, choose **Desktop app**, create it, and **download the JSON file**.
7. Save it outside the repository. Run `python configure.py doctor` to see the default location:
   - Windows: `%APPDATA%\stream-clip-studio\client_secret.json`
   - Linux and macOS: `~/.config/stream-clip-studio/client_secret.json`

   To use another location, set `YOUTUBE_CLIENT_SECRET_FILE` in `.env` to the full path.
8. Sign in:

   ```powershell
   python post_youtube.py auth
   ```

   Your browser opens Google's page. Choose the account and channel, then approve **upload videos**. The token is saved in your OS keychain.
9. Test without uploading:

   ```powershell
   python post_youtube.py upload clips\READY_TO_POST\my_clip.mp4 --title "Test" --dry-run
   ```

**Good to know**
- While your Google app is in **Testing**, sign-ins expire after 7 days. Run `auth` again, or publish the app in the consent screen settings.
- Uploads count against your own project's quota, which resets at midnight Pacific Time.
- Some accounts need phone verification before they can upload Shorts or use custom features.
- For a Brand Account channel, pick that channel during sign-in.

## YouTube view counts (optional)

The daily recap reads public view counts with an API key.

1. In the same Google project, go to **APIs & Services > Credentials > Create credentials > API key**.
2. Click the key and restrict it to **YouTube Data API v3**.
3. Add it as `YOUTUBE_API_KEY`.

## Discord (optional)

1. In your server, open **Edit Channel > Integrations > Webhooks > New Webhook**.
2. Click **Copy Webhook URL**.
3. Add it to the matching setting. You can reuse one URL for several, or leave any empty to skip it.

| Setting | Receives |
|---|---|
| `DISCORD_WEBHOOK_LIVE` | A streamer went live |
| `DISCORD_WEBHOOK_TOPOST` | A clip for you to post on TikTok |
| `DISCORD_WEBHOOK_POSTED` | Published clips and reports |
| `DISCORD_WEBHOOK_RECAP` | The daily recap |
| `DISCORD_WEBHOOK_ASK` | Messages from `notify.py ask` |

Test them:

```powershell
python notify.py test
```

A webhook URL works like a password: anyone who has it can post to that channel. If one leaks, delete it in Discord and create a new one.

## TikTok

**Default (recommended):** nothing to connect. The pipeline saves the video and caption in `clips/TIKTOK_TO_POST/`, and you upload them in the TikTok app.

**Optional and unofficial:** automatic upload through the separately installed third-party `autotok` tool. It isn't an official TikTok integration, may break, may violate TikTok's terms, and may cause low reach. It's your decision and your account. Set it up with that tool's own login, then enable it:

```toml
[unofficial]
tiktok_autotok = true

[tiktok]
mode = "autotok"
autotok_account = "the_account_name_in_autotok"
```

## If a credential leaks

1. **Revoke it first:** create a new Twitch secret, delete the Google client or API key, or delete the Discord webhook.
2. Remove Google access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) if needed.
3. Replace the value locally. For YouTube, sign in again with `python post_youtube.py auth` to replace the saved token.
4. Deleting a commit isn't enough once it's pushed. Treat anything that reached GitHub as public.
