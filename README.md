# aw-watcher-discord

ActivityWatch watcher that tracks Discord voice sessions, mute and deafen states, call participants, connection status, and voice settings via Discord's official local RPC interface (`rpc.voice.read`).

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
