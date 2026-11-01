# aw-watcher-discord

ActivityWatch watcher that tracks Discord voice sessions, mute and deafen states, call participants, connection status, and voice settings via Discord's official local RPC interface (`rpc.voice.read`).

Optionally it also tracks your Discord activity/presence — games, Spotify, VS Code, and anything else that reports Rich Presence to Discord — into a separate presence bucket (see [Presence tracking](#presence-tracking-games-spotify-vs-code-)).

## Setup

1. Create a Discord application at https://discord.com/developers/applications.
2. Under **OAuth2 -> Redirects**, add `http://127.0.0.1` and click **Save Changes**.
3. Create a virtual environment and install the package:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip.exe install -e .
   ```
4. Configure `client_id` and `client_secret` in `%LOCALAPPDATA%\activitywatch\activitywatch\aw-watcher-discord\aw-watcher-discord.toml`, or place them in `.env` and run:
   ```powershell
   python setup_activitywatch.py
   ```
   This installs `aw-watcher-discord.exe` into `~/.local/bin` and registers `aw-watcher-discord` in `aw-qt.toml`.

## Configuration

Options in `aw-watcher-discord.toml` (section `[aw-watcher-discord]`):

| Option | Default | Description |
| --- | --- | --- |
| `poll_time` | `5.0` | Seconds between polls of the Discord client. |
| `include_pings` | `false` | Include voice server ping samples in `voice_connection_status`. |
| `include_raw` | `false` | Also store the raw `selected_voice_channel` and `voice_settings` RPC payloads (all local audio devices, per-user volume/pan, avatars). Changes in these split a call into separate events, so leave this off unless you need the data. |
| `presence_source` | `"off"` | Presence tracking source: `off`, `bot` or `lanyard` (see below). |
| `bot_token` | `""` | Bot token used when `presence_source = "bot"`. |
| `presence_user_id` | `""` | User id to watch. Defaults to the id learned from the local RPC connection; set it explicitly to keep presence working while the desktop client is closed (bot mode). |

## Presence tracking (games, Spotify, VS Code, ...)

Discord's local RPC interface does not expose other applications' Rich Presence; presence is only available through the Discord gateway as `PRESENCE_UPDATE` events. The watcher supports two sources:

| Source | How it works | Requires |
| --- | --- | --- |
| `bot` (recommended) | Your own bot connects to the Discord gateway with the Presence Intent; presence data stays private to your watcher | Bot setup below |
| `lanyard` | Polls the public [Lanyard](https://github.com/phineas/lanyard) REST API | Joining [discord.gg/lanyard](https://discord.gg/lanyard); your presence becomes publicly readable by anyone with your user id |

### Bot setup

1. In your Discord application at https://discord.com/developers/applications (the same one used for voice tracking works), open **Bot** and enable **Privileged Gateway Intents -> PRESENCE INTENT**.
2. Invite the bot to a private server that your user account is a member of (OAuth2 -> URL Generator, scope `bot`).
3. Set `presence_source = "bot"` and `bot_token = "<bot token>"` in `aw-watcher-discord.toml`, or put `PRESENCE_SOURCE=bot` and `DISCORD_BOT_TOKEN=<bot token>` in `.env` and re-run `python setup_activitywatch.py`.

For Lanyard, join their server and set `presence_source = "lanyard"` instead — no bot needed.

### Notes

- Presence events land in a separate `aw-watcher-discord-presence_<host>` bucket (event type `general.stopwatch.discord.presence`); timeline colors are synced per app and extracted from the activity artwork.
- Events split whenever `details`, `state` or timestamps change (per track, per edited file). Applications that put running timers into their presence text will split frequently.
- Only what Discord shares publicly is available; custom statuses can be omitted depending on your profile privacy settings, and activities disappear when your Discord client (or linked console/mobile session) is not publishing them.
