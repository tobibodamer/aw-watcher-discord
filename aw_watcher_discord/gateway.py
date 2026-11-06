import json
import logging
import random
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import websocket

logger = logging.getLogger(__name__)

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
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

GATEWAY_BOT_API = "https://discord.com/api/v10/gateway/bot"
GATEWAY_QUERY = "/?v=10&encoding=json"
USER_AGENT = "DiscordBot (https://github.com/tobibodamer/aw-watcher-discord, 0.1.0)"

MIN_RECONNECT_DELAY = 5.0
MAX_RECONNECT_DELAY = 300.0

# A session that stays up this long is healthy and resets the reconnect delay.
STABLE_SESSION_SECONDS = 60.0

# Discord resets the token of a bot that exhausts its daily session starts
# (normally 1000), so identifying pauses well before reaching the limit.
IDENTIFY_RESERVE = 50

# Closing with 1000 or 1001 would invalidate the session and prevent a resume.
CLOSE_STATUS_NORMAL = 1000
CLOSE_STATUS_RESUMABLE = 4000

# Gateway close codes after which the session cannot be resumed.
NEW_SESSION_CLOSE_CODES = {4007, 4009}

# Permanent gateway close codes that retrying cannot fix.
FATAL_CLOSE_CODES = {
    4004: "Discord rejected the bot token. Check bot_token in the config.",
    4010: "Discord rejected the shard configuration.",
    4011: "Discord requires sharding for this bot, which is not supported.",
    4012: "Discord rejected the gateway API version.",
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
    session alive for as long as the client runs, resuming it after connection
    losses, since only a new session counts against Discord's daily limit. The
    latest presence snapshot is available via snapshot() while connected.
    """

    def __init__(self, token: str):
        self.token = token
        self.user_id = ""
        self._ws: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False
        self._latest: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._seq: Optional[int] = None
        self._session_id: Optional[str] = None
        self._resume_url: Optional[str] = None
        self._ready_at: Optional[float] = None
        self._heartbeat_ack = threading.Event()
        self._ever_seen = False
        self._warned_unseen = False
        self._fatal: Optional[str] = None
        self._started_at = 0.0

    def start(self, user_id: str) -> None:
        self.user_id = str(user_id)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="discord-gateway", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_ws(CLOSE_STATUS_NORMAL)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._connected:
                return self._latest
        return None

    def _run(self) -> None:
        delay = MIN_RECONNECT_DELAY
        while not self._stop.is_set():
            self._ready_at = None
            reason = "connection closed"
            try:
                self._session()
            except Exception as exc:
                reason = str(exc)
            if self._stop.is_set():
                break
            if self._fatal:
                logger.error(self._fatal)
                return
            healthy = (
                self._ready_at is not None
                and time.monotonic() - self._ready_at >= STABLE_SESSION_SECONDS
            )
            if healthy:
                delay = MIN_RECONNECT_DELAY
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
            logger.log(
                logging.INFO if healthy else logging.WARNING,
                f"Gateway connection lost ({reason}), reconnecting in {delay:.0f}s",
            )
            self._stop.wait(delay)
            delay = min(delay * 2.0, MAX_RECONNECT_DELAY)

    def _session(self) -> None:
        resume_url = self._resume_url if self._session_id else None
        url = resume_url or self._identify_url()
        if not url:
            return

        ws = websocket.create_connection(url, timeout=15.0)
        self._ws = ws
        self._heartbeat_ack.set()
        try:
            hello = json.loads(ws.recv())
            if hello.get("op") != OP_HELLO:
                raise DiscordGatewayError(f"Expected HELLO from gateway, got {hello}")
            interval = float((hello.get("d") or {}).get("heartbeat_interval", 41250)) / 1000.0

            if resume_url:
                self._send(ws, OP_RESUME, {
                    "token": self.token,
                    "session_id": self._session_id,
                    "seq": self._seq,
                })
            else:
                self._seq = None
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
            next_beat = time.monotonic() + interval * random.random()
            while not self._stop.is_set():
                try:
                    opcode, data = ws.recv_data()
                except websocket.WebSocketTimeoutException:
                    opcode = None
                    data = None
                if opcode == WS_OPCODE_CLOSE:
                    self._handle_close(data)
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
            self._close_ws(CLOSE_STATUS_RESUMABLE)

    def _handle_close(self, data: Optional[bytes]) -> None:
        close_code: Optional[int] = None
        if data and len(data) >= 2:
            close_code = struct.unpack("!H", data[:2])[0]
        if close_code in FATAL_CLOSE_CODES:
            self._fatal = f"{FATAL_CLOSE_CODES[close_code]} (close code {close_code})"
        if close_code in NEW_SESSION_CLOSE_CODES:
            self._session_id = None
        raise DiscordGatewayError(f"Gateway closed the connection (code {close_code})")

    def _identify_url(self) -> Optional[str]:
        """Returns the URL for a new session, or None if the daily limit is near."""
        info = self._fetch_gateway_bot()
        limit = info["session_start_limit"]
        remaining = int(limit["remaining"])
        if remaining <= IDENTIFY_RESERVE:
            wait = float(limit["reset_after"]) / 1000.0
            logger.warning(
                f"Only {remaining} of {limit['total']} Discord session starts left, "
                f"pausing presence tracking for {wait / 60.0:.0f} minutes"
            )
            self._stop.wait(wait)
            return None
        logger.info(f"Starting a new gateway session ({remaining} session starts left)")
        return f"{str(info['url']).rstrip('/')}{GATEWAY_QUERY}"

    def _fetch_gateway_bot(self) -> Dict[str, Any]:
        req = urllib.request.Request(
            GATEWAY_BOT_API,
            headers={
                "Authorization": f"Bot {self.token}",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._fatal = f"{FATAL_CLOSE_CODES[4004]} (HTTP 401)"
            raise

    def _send(self, ws: Any, op: int, data: Any) -> None:
        ws.send(json.dumps({"op": op, "d": data}))

    def _handle_message(self, ws: Any, msg: Dict[str, Any]) -> None:
        op = msg.get("op")
        if op == OP_HEARTBEAT_ACK:
            self._heartbeat_ack.set()
        elif op == OP_HEARTBEAT:
            self._send(ws, OP_HEARTBEAT, self._seq)
        elif op == OP_RECONNECT:
            raise DiscordGatewayError("Gateway requested a reconnect")
        elif op == OP_INVALID_SESSION:
            if not msg.get("d"):
                self._session_id = None
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
            self._session_id = data.get("session_id")
            resume_url = data.get("resume_gateway_url")
            self._resume_url = (
                f"{str(resume_url).rstrip('/')}{GATEWAY_QUERY}" if resume_url else None
            )
            self._ready_at = time.monotonic()
            with self._lock:
                self._connected = True
                self._latest = None
        elif evt == "RESUMED":
            logger.info("Resumed Discord gateway session")
            self._ready_at = time.monotonic()
            with self._lock:
                self._connected = True
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

    def _close_ws(self, status: int) -> None:
        ws = self._ws
        self._ws = None
        if ws:
            try:
                ws.close(status=status)
            except Exception:
                pass
        with self._lock:
            self._connected = False
