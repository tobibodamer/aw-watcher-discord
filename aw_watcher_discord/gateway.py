import json
import logging
import struct
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

import websocket

logger = logging.getLogger(__name__)

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# WebSocket-level opcodes (the OP_* constants above are Discord gateway ops)
WS_OPCODE_TEXT = 1
WS_OPCODE_BINARY = 2
WS_OPCODE_CLOSE = 8

INTENT_GUILDS = 1 << 0
INTENT_GUILD_PRESENCES = 1 << 8

GATEWAY_URL_API = "https://discord.com/api/v10/gateway"

# Permanent gateway close codes that retrying cannot fix.
FATAL_CLOSE_CODES = {
    4004: "Discord rejected the bot token. Check bot_token in the config.",
    4013: "Discord rejected the requested gateway intents.",
    4014: (
        "The bot is not allowed to use the Presence Intent. Enable "
        "'Privileged Gateway Intents -> PRESENCE INTENT' for the bot in the "
        "Discord Developer Portal and restart the watcher."
    ),
}


class DiscordGatewayError(Exception):
    pass


class BotGatewayClient:
    """Reads the local user's presence from the Discord gateway as a bot.

    The bot needs the Presence Intent enabled in the Developer Portal and must
    share a guild with the watched user. A background thread keeps a single
    websocket connection alive; the latest presence snapshot is available via
    snapshot() while the connection is up.
    """

    def __init__(self, token: str):
        self.token = token
        self.user_id = ""
        self._gateway_url: Optional[str] = None
        self._ws: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._latest: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._seq: Optional[int] = None
        self._heartbeat_ack = threading.Event()
        self._heartbeat_ack.set()
        self._ever_seen = False
        self._warned_unseen = False
        self._fatal: Optional[str] = None
        self._started_at = 0.0

    def start(self, user_id: str) -> None:
        self.user_id = str(user_id)
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="discord-gateway", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._close_ws()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._connected:
                return self._latest
        return None

    def _run(self) -> None:
        backoff = 5.0
        while self._running:
            try:
                self._session()
                backoff = 5.0
            except Exception as exc:
                logger.debug(f"Gateway session ended: {exc}")
            if not self._running:
                break
            if self._fatal:
                logger.error(self._fatal)
                return
            if (
                not self._warned_unseen
                and not self._ever_seen
                and time.monotonic() - self._started_at > 60.0
            ):
                logger.warning(
                    f"No presence for user {self.user_id} received yet. Is the bot "
                    "in a Discord server together with this user?"
                )
                self._warned_unseen = True
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)

    def _session(self) -> None:
        ws = websocket.create_connection(self._fetch_gateway_url(), timeout=15.0)
        self._ws = ws
        try:
            hello = json.loads(ws.recv())
            if hello.get("op") != OP_HELLO:
                raise DiscordGatewayError(f"Expected HELLO from gateway, got {hello}")
            interval = float((hello.get("d") or {}).get("heartbeat_interval", 41250)) / 1000.0

            self._send(ws, OP_IDENTIFY, {
                "token": self.token,
                "intents": INTENT_GUILDS | INTENT_GUILD_PRESENCES,
                "properties": {
                    "os": sys.platform,
                    "browser": "aw-watcher-discord",
                    "device": "aw-watcher-discord",
                },
            })

            # Waking up regularly from recv keeps heartbeats on schedule and
            # lets stop() close the socket promptly.
            ws.settimeout(5.0)
            next_beat = time.monotonic()
            while self._running:
                try:
                    opcode, data = ws.recv_data()
                except websocket.WebSocketTimeoutException:
                    opcode = None
                    data = None
                if opcode == WS_OPCODE_CLOSE:
                    self._handle_close(data)
                    break
                if opcode in (WS_OPCODE_TEXT, WS_OPCODE_BINARY) and data:
                    self._handle_message(ws, json.loads(data))
                now = time.monotonic()
                if now >= next_beat:
                    if not self._heartbeat_ack.is_set():
                        raise DiscordGatewayError("Gateway missed a heartbeat ACK")
                    self._send(ws, OP_HEARTBEAT, self._seq)
                    self._heartbeat_ack.clear()
                    next_beat = now + interval
        finally:
            self._close_ws()

    def _handle_close(self, data: Optional[bytes]) -> None:
        close_code: Optional[int] = None
        if data and len(data) >= 2:
            close_code = struct.unpack("!H", data[:2])[0]
        if close_code in FATAL_CLOSE_CODES:
            self._fatal = f"{FATAL_CLOSE_CODES[close_code]} (close code {close_code})"
        else:
            logger.debug(f"Gateway closed the connection (code {close_code})")

    def _fetch_gateway_url(self) -> str:
        if self._gateway_url:
            return self._gateway_url
        req = urllib.request.Request(
            GATEWAY_URL_API,
            headers={"User-Agent": "aw-watcher-discord/0.1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self._gateway_url = f"{str(body['url']).rstrip('/')}/?v=10&encoding=json"
        return self._gateway_url

    def _send(self, ws: Any, op: int, data: Any) -> None:
        ws.send(json.dumps({"op": op, "d": data}))

    def _handle_message(self, ws: Any, msg: Dict[str, Any]) -> None:
        op = msg.get("op")
        if op == OP_HEARTBEAT_ACK:
            self._heartbeat_ack.set()
        elif op == OP_HEARTBEAT:
            self._send(ws, OP_HEARTBEAT, self._seq)
            self._heartbeat_ack.clear()
        elif op == OP_RECONNECT:
            raise DiscordGatewayError("Gateway requested a reconnect")
        elif op == OP_INVALID_SESSION:
            # Sessions are re-identified instead of resumed, so back off and retry.
            raise DiscordGatewayError("Gateway invalidated the session")
        elif op == OP_DISPATCH:
            seq = msg.get("s")
            if seq is not None:
                self._seq = int(seq)
            self._handle_dispatch(msg.get("t"), msg.get("d") or {})

    def _handle_dispatch(self, evt: Optional[str], data: Dict[str, Any]) -> None:
        if evt == "READY":
            user = data.get("user") or {}
            logger.info(
                f"Connected to Discord gateway as {user.get('username')} "
                f"({user.get('id')}), watching presence of user {self.user_id}"
            )
            with self._lock:
                self._connected = True
                self._latest = None
        elif evt == "GUILD_CREATE":
            for presence in data.get("presences") or []:
                self._maybe_store(presence)
        elif evt == "PRESENCE_UPDATE":
            self._maybe_store(data)

    def _maybe_store(self, presence: Dict[str, Any]) -> None:
        user = presence.get("user") or {}
        if str(user.get("id", "")) != self.user_id:
            return
        self._ever_seen = True
        self._warned_unseen = False
        snapshot = {
            "status": presence.get("status"),
            "activities": presence.get("activities") or [],
        }
        with self._lock:
            self._latest = snapshot

    def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        with self._lock:
            self._connected = False
