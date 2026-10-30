import copy
import json
import logging
import os
import struct
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"

CHANNEL_TYPE_NAMES = {
    1: "dm_call",
    2: "guild_voice",
    3: "group_dm_call",
    13: "guild_stage",
}


class DiscordIPCError(Exception):
    pass


class DiscordRPCClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str = "http://127.0.0.1",
        cache_dir: Optional[Path] = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_cache_path = (cache_dir or Path.cwd()) / "token_cache.json"
        self.pipe: Optional[Any] = None
        self.user_id: Optional[str] = None
        self.username: Optional[str] = None
        self.last_connection_status: Optional[Dict[str, Any]] = None

    def _open_ipc_pipe(self) -> Any:
        if sys.platform == "win32":
            for i in range(10):
                pipe_path = rf"\\?\pipe\discord-ipc-{i}"
                try:
                    return open(pipe_path, "r+b", buffering=0)
                except OSError:
                    continue
            raise DiscordIPCError("Could not find a running Discord desktop client on discord-ipc-0..9.")
        else:
            import socket

            runtime_dirs = [
                os.environ.get("XDG_RUNTIME_DIR"),
                os.environ.get("TMPDIR"),
                os.environ.get("TMP"),
                os.environ.get("TEMP"),
                "/tmp",
            ]
            for d in filter(None, runtime_dirs):
                for i in range(10):
                    sock_path = os.path.join(d, f"discord-ipc-{i}")
                    if os.path.exists(sock_path):
                        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        sock.connect(sock_path)
                        return sock.makefile("r+b", buffering=0)
            raise DiscordIPCError("Could not find a running Discord desktop client socket.")

    def _send_frame(self, opcode: int, payload: Dict[str, Any]) -> None:
        if not self.pipe:
            raise DiscordIPCError("IPC pipe is not open.")
        raw = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", opcode, len(raw))
        self.pipe.write(header + raw)
        self.pipe.flush()

    def _recv_frame(self) -> Tuple[int, Dict[str, Any]]:
        if not self.pipe:
            raise DiscordIPCError("IPC pipe is not open.")
        header = self.pipe.read(8)
        if not header or len(header) < 8:
            raise DiscordIPCError("Discord closed the IPC pipe.")
        opcode, length = struct.unpack("<II", header)
        data = self.pipe.read(length)
        if len(data) < length:
            raise DiscordIPCError("Truncated frame from Discord IPC.")
        return opcode, json.loads(data.decode("utf-8"))

    def request(self, cmd: str, args: Optional[Dict[str, Any]] = None, evt: Optional[str] = None) -> Dict[str, Any]:
        nonce = str(uuid.uuid4())
        payload: Dict[str, Any] = {"cmd": cmd, "nonce": nonce}
        if args is not None:
            payload["args"] = args
        if evt is not None:
            payload["evt"] = evt

        self._send_frame(OP_FRAME, payload)

        while True:
            opcode, msg = self._recv_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, msg)
                continue
            if opcode == OP_CLOSE:
                raise DiscordIPCError(f"Discord closed connection: {msg}")
            if msg.get("nonce") == nonce:
                if msg.get("evt") == "ERROR":
                    err_data = msg.get("data") or {}
                    if "redirect_uri" in str(err_data.get("message", "")):
                        raise DiscordIPCError(
                            "Your Discord app has no Redirect URI saved in the Developer Portal. "
                            "Go to OAuth2 -> Redirects, add http://127.0.0.1, and click Save Changes."
                        )
                    raise DiscordIPCError(f"RPC error on {cmd}: {err_data}")
                return msg
            self._handle_dispatch(msg)

    def _handle_dispatch(self, msg: Dict[str, Any]) -> None:
        if msg.get("cmd") != "DISPATCH":
            return
        if msg.get("evt") == "VOICE_CONNECTION_STATUS":
            self.last_connection_status = msg.get("data")

    def _load_cached_token(self) -> Optional[Dict[str, Any]]:
        if not self.token_cache_path.exists():
            return None
        try:
            return json.loads(self.token_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_cached_token(self, token_data: Dict[str, Any]) -> None:
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        token_data["expires_at"] = time.time() + float(token_data.get("expires_in", 604800)) - 300
        self.token_cache_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")

    def _exchange_or_refresh_token(self, form_data: Dict[str, str]) -> Dict[str, Any]:
        encoded = urllib.parse.urlencode(form_data).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_TOKEN_URL,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "aw-watcher-discord/0.1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self._save_cached_token(body)
        return body

    def _get_valid_access_token(self) -> str:
        cached = self._load_cached_token()
        if cached:
            if time.time() < cached.get("expires_at", 0):
                return str(cached["access_token"])
            if "refresh_token" in cached:
                try:
                    refreshed = self._exchange_or_refresh_token(
                        {
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "grant_type": "refresh_token",
                            "refresh_token": cached["refresh_token"],
                        }
                    )
                    return str(refreshed["access_token"])
                except Exception as exc:
                    logger.warning(f"Cached token refresh failed, requesting new authorization: {exc}")

        logger.info("Requesting local RPC authorization from Discord client...")
        res = self.request(
            "AUTHORIZE",
            args={
                "client_id": self.client_id,
                "scopes": ["rpc", "rpc.voice.read"],
            },
        )
        code = res["data"]["code"]
        token_data = self._exchange_or_refresh_token(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        return str(token_data["access_token"])

    def connect(self) -> None:
        self.close()
        self.pipe = self._open_ipc_pipe()
        self._send_frame(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        opcode, ready_msg = self._recv_frame()
        if opcode != OP_FRAME or ready_msg.get("evt") != "READY":
            raise DiscordIPCError(f"Unexpected handshake response: {ready_msg}")

        access_token = self._get_valid_access_token()
        try:
            auth_res = self.request("AUTHENTICATE", args={"access_token": access_token})
        except DiscordIPCError:
            if self.token_cache_path.exists():
                self.token_cache_path.unlink()
            access_token = self._get_valid_access_token()
            auth_res = self.request("AUTHENTICATE", args={"access_token": access_token})

        user = auth_res.get("data", {}).get("user", {})
        self.user_id = str(user.get("id", ""))
        self.username = user.get("username", "unknown")
        logger.info(f"Authenticated with local Discord RPC as {self.username} ({self.user_id})")

        self.request("SUBSCRIBE", evt="VOICE_CHANNEL_SELECT")
        self.request("SUBSCRIBE", evt="VOICE_CONNECTION_STATUS")
        self.request("SUBSCRIBE", evt="VOICE_SETTINGS_UPDATE")

    def close(self) -> None:
        if self.pipe:
            try:
                self.pipe.close()
            except Exception:
                pass
            self.pipe = None

    def fetch_voice_event_data(self, include_volatile_pings: bool = False) -> Optional[Dict[str, Any]]:
        channel_res = self.request("GET_SELECTED_VOICE_CHANNEL")
        channel = channel_res.get("data")
        if not channel or not channel.get("id"):
            return None

        settings_res = self.request("GET_VOICE_SETTINGS")
        voice_settings = settings_res.get("data") or {}

        conn_status = copy.deepcopy(self.last_connection_status or {})
        if not include_volatile_pings and conn_status:
            conn_status.pop("pings", None)
            conn_status.pop("last_ping", None)
            conn_status.pop("average_ping", None)

        voice_states = channel.get("voice_states") or []
        own_vs = {}
        participants = []
        for entry in voice_states:
            u = entry.get("user") or {}
            uid = str(u.get("id", ""))
            vs = entry.get("voice_state") or {}
            participants.append(
                {
                    "id": uid,
                    "username": u.get("username"),
                    "global_name": u.get("global_name"),
                    "nick": entry.get("nick"),
                    "self_mute": bool(vs.get("self_mute")),
                    "self_deaf": bool(vs.get("self_deaf")),
                    "server_mute": bool(vs.get("mute")),
                    "server_deaf": bool(vs.get("deaf")),
                }
            )
            if self.user_id and uid == self.user_id:
                own_vs = vs

        self_mute = bool(own_vs.get("self_mute", voice_settings.get("mute", False)))
        self_deaf = bool(own_vs.get("self_deaf", voice_settings.get("deaf", False)))
        server_mute = bool(own_vs.get("mute", False))
        server_deaf = bool(own_vs.get("deaf", False))

        raw_type = channel.get("type", 2)
        channel_type = CHANNEL_TYPE_NAMES.get(raw_type, f"type_{raw_type}")

        return {
            "title": f"{channel.get('name') or 'Private Call'} ({len(participants)} in call)",
            "channel_id": str(channel.get("id")),
            "channel_name": channel.get("name") or "Private Call",
            "channel_type": channel_type,
            "guild_id": channel.get("guild_id"),
            "self_mute": self_mute,
            "self_deaf": self_deaf,
            "server_mute": server_mute,
            "server_deaf": server_deaf,
            "participant_count": len(participants),
            "participants": participants,
            "selected_voice_channel": channel,
            "voice_connection_status": conn_status,
            "voice_settings": voice_settings,
        }
