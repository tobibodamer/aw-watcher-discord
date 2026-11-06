import json
import logging
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

LANYARD_API_URL = "https://api.lanyard.rest/v1/users/{user_id}"


class LanyardClient:
    """Polls the public Lanyard REST API for a user's Discord presence.

    Lanyard only monitors users who have joined https://discord.gg/lanyard.
    Joining that server publishes the user's presence (games, Spotify, custom
    status) to an unauthenticated public endpoint, which this client polls.
    """

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._warned_unmonitored = False

    def fetch(self, user_id: str) -> Optional[Dict[str, Any]]:
        url = LANYARD_API_URL.format(user_id=str(user_id))
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "aw-watcher-discord/0.1.0"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        if not body.get("success"):
            if not self._warned_unmonitored:
                logger.info(
                    f"Lanyard is not monitoring user {user_id} yet. "
                    "Join https://discord.gg/lanyard so your presence shows up "
                    f"at {url}"
                )
                self._warned_unmonitored = True
            return None

        data = body.get("data") or {}
        return {
            "status": data.get("discord_status"),
            "activities": data.get("activities") or [],
        }
