import io
import json
import struct
import unittest
import urllib.error
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import websocket

from aw_watcher_discord import gateway
from aw_watcher_discord.gateway import BotGatewayClient, DiscordGatewayError

RESUME_URL = "wss://resume.example"
RESUME_CONNECT_URL = f"{RESUME_URL}{gateway.GATEWAY_QUERY}"
READY = {
    "op": 0,
    "s": 1,
    "t": "READY",
    "d": {"session_id": "abc", "resume_gateway_url": RESUME_URL, "user": {}},
}
ACK = {"op": 11}


def text(msg: Dict[str, Any]) -> Tuple[int, bytes]:
    return gateway.WS_OPCODE_TEXT, json.dumps(msg).encode()


def close_frame(code: int) -> Tuple[int, bytes]:
    return gateway.WS_OPCODE_CLOSE, struct.pack("!H", code)


class FakeWebSocket:
    def __init__(self, frames: List[Tuple[int, bytes]], heartbeat_interval: int = 41250):
        self.frames = list(frames)
        self.heartbeat_interval = heartbeat_interval
        self.sent: List[Dict[str, Any]] = []
        self.close_status: Optional[int] = None

    def recv(self) -> str:
        return json.dumps({"op": 10, "d": {"heartbeat_interval": self.heartbeat_interval}})

    def recv_data(self) -> Tuple[int, bytes]:
        if not self.frames:
            raise websocket.WebSocketConnectionClosedException("script exhausted")
        return self.frames.pop(0)

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def settimeout(self, timeout: float) -> None:
        pass

    def close(self, status: int = 1000) -> None:
        self.close_status = status

    def ops(self) -> List[int]:
        return [msg["op"] for msg in self.sent]


def gateway_bot_response(remaining: int = 900) -> mock.MagicMock:
    body = {
        "url": "wss://gateway.example",
        "session_start_limit": {
            "total": 1000,
            "remaining": remaining,
            "reset_after": 0,
            "max_concurrency": 1,
        },
    }
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(body).encode()
    return resp


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = BotGatewayClient("token")
        self.client.user_id = "1"
        urlopen = mock.patch.object(
            gateway.urllib.request, "urlopen", return_value=gateway_bot_response()
        )
        connect = mock.patch.object(gateway.websocket, "create_connection")
        self.urlopen = urlopen.start()
        self.connect = connect.start()
        self.addCleanup(mock.patch.stopall)

    def run_session(self, ws: FakeWebSocket) -> None:
        self.connect.return_value = ws
        try:
            self.client._session()
        except (DiscordGatewayError, websocket.WebSocketConnectionClosedException):
            pass

    def test_unacked_heartbeat_from_previous_session_does_not_kill_the_next(self) -> None:
        self.client._heartbeat_ack.clear()
        ws = FakeWebSocket([text(READY), text(ACK), close_frame(4000)], heartbeat_interval=0)

        self.run_session(ws)

        self.assertEqual(
            ws.ops(), [gateway.OP_IDENTIFY, gateway.OP_HEARTBEAT, gateway.OP_HEARTBEAT]
        )

    def test_missed_ack_within_a_session_ends_it(self) -> None:
        ws = FakeWebSocket([text(READY), text({"op": 0, "s": 2, "t": "X"})], heartbeat_interval=0)

        self.connect.return_value = ws
        with self.assertRaisesRegex(DiscordGatewayError, "heartbeat ACK"):
            self.client._session()

    def test_reconnect_resumes_without_a_new_identify(self) -> None:
        first = FakeWebSocket([text(READY), text({"op": 7})])
        self.run_session(first)
        self.assertEqual(first.close_status, gateway.CLOSE_STATUS_RESUMABLE)
        self.urlopen.reset_mock()

        second = FakeWebSocket([text({"op": 0, "s": 2, "t": "RESUMED", "d": {}})])
        self.run_session(second)

        self.connect.assert_called_with(RESUME_CONNECT_URL, timeout=15.0)
        self.urlopen.assert_not_called()
        self.assertEqual(second.ops(), [gateway.OP_RESUME])
        self.assertEqual(second.sent[0]["d"]["session_id"], "abc")
        self.assertEqual(second.sent[0]["d"]["seq"], 1)
        self.assertIsNotNone(self.client._ready_at)

    def test_invalid_session_resumes_only_when_resumable(self) -> None:
        self.run_session(FakeWebSocket([text(READY), text({"op": 9, "d": True})]))
        self.assertEqual(self.client._session_id, "abc")

        self.run_session(FakeWebSocket([text(READY), text({"op": 9, "d": False})]))
        self.assertIsNone(self.client._session_id)

    def test_expired_session_close_codes_start_a_new_session(self) -> None:
        for code in gateway.NEW_SESSION_CLOSE_CODES:
            self.client._session_id = None
            self.run_session(FakeWebSocket([text(READY), close_frame(code)]))
            self.assertIsNone(self.client._session_id)

    def test_low_session_start_budget_does_not_connect(self) -> None:
        self.urlopen.return_value = gateway_bot_response(gateway.IDENTIFY_RESERVE)

        self.client._session()

        self.connect.assert_not_called()

    def test_rejected_token_is_fatal_over_rest(self) -> None:
        self.urlopen.side_effect = urllib.error.HTTPError(
            gateway.GATEWAY_BOT_API, 401, "Unauthorized", {}, io.BytesIO()  # type: ignore[arg-type]
        )

        with self.assertRaises(urllib.error.HTTPError):
            self.client._session()

        self.assertIn("bot token", self.client._fatal or "")
        self.connect.assert_not_called()

    def test_rejected_token_is_fatal_over_gateway(self) -> None:
        self.run_session(FakeWebSocket([close_frame(4004)]))

        self.assertIn("bot token", self.client._fatal or "")

    def test_stop_closes_with_normal_status(self) -> None:
        ws = FakeWebSocket([])
        self.client._ws = ws

        self.client.stop()

        self.assertEqual(ws.close_status, gateway.CLOSE_STATUS_NORMAL)


class ReconnectDelayTest(unittest.TestCase):
    def delays_for(self, session_side_effects: List[Any]) -> List[float]:
        client = BotGatewayClient("token")
        delays: List[float] = []
        client._stop = mock.Mock()
        client._stop.wait.side_effect = delays.append
        client._stop.is_set.side_effect = lambda: len(delays) >= len(session_side_effects)
        with mock.patch.object(client, "_session", side_effect=session_side_effects):
            client._run()
        return delays

    def test_failing_sessions_back_off_to_the_cap(self) -> None:
        delays = self.delays_for([DiscordGatewayError("boom")] * 8)

        self.assertEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300])

    def test_clean_return_without_ready_does_not_reset_the_delay(self) -> None:
        delays = self.delays_for([None] * 4)

        self.assertEqual(delays, [5, 10, 20, 40])

    def test_cap_stays_below_the_daily_session_start_limit(self) -> None:
        self.assertLess(86400.0 / gateway.MAX_RECONNECT_DELAY, 1000)

    def test_stable_session_resets_the_delay(self) -> None:
        client = BotGatewayClient("token")
        delays: List[float] = []
        client._stop = mock.Mock()
        client._stop.wait.side_effect = delays.append
        client._stop.is_set.side_effect = lambda: len(delays) >= 4
        calls = iter([False, False, True, False])

        def session() -> None:
            if next(calls):
                client._ready_at = gateway.time.monotonic() - gateway.STABLE_SESSION_SECONDS
            raise DiscordGatewayError("boom")

        with mock.patch.object(client, "_session", side_effect=session):
            client._run()

        self.assertEqual(delays, [5, 10, 5, 10])


if __name__ == "__main__":
    unittest.main()
