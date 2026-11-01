import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .colors import extract_dominant_color

logger = logging.getLogger(__name__)

ACTIVITY_TYPE_NAMES: Dict[Any, str] = {
    0: "playing",
    1: "streaming",
    2: "listening",
    3: "watching",
    5: "competing",
}
VERBS: Dict[Any, str] = {
    0: "Playing",
    1: "Streaming",
    2: "Listening to",
    3: "Watching",
    5: "Competing in",
}

# Mirrors what the Discord client shows as the status line: games outrank
# streams, which outrank listening/watching activities. Custom statuses are
# skipped so a static status text does not drown out actual activities.
_PRIORITY: Dict[Any, int] = {0: 0, 5: 1, 1: 2, 2: 3, 3: 4}

MAX_LABEL_LEN = 28


def pick_primary_activity(activities: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_rank = 999
    for activity in activities:
        try:
            rank = _PRIORITY.get(activity.get("type"))
        except TypeError:
            continue
        if rank is None:
            continue
        if rank < best_rank:
            best = activity
            best_rank = rank
    return best


def activity_image_url(activity: Dict[str, Any]) -> Optional[str]:
    assets = activity.get("assets") or {}
    image = assets.get("large_image") or assets.get("small_image")
    if not image or not isinstance(image, str):
        return None
    if image.startswith("spotify:"):
        return f"https://i.scdn.co/image/{image.removeprefix('spotify:')}"
    if image.startswith("mp:external/"):
        return (
            "https://media.discordapp.net/external/"
            f"{image.removeprefix('mp:external/')}"
        )
    application_id = activity.get("application_id")
    if str(application_id or "").isdigit() and image.isdigit():
        return f"https://cdn.discordapp.com/app-assets/{application_id}/{image}.png"
    return None


def _iso_from_ms(ms: Optional[Any]) -> Optional[str]:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _slugify(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.lower())
    return "-".join(part for part in slug.split("-") if part) or "unknown"


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_label(verb: str, name: str, details: Optional[str]) -> str:
    if not details:
        return f"{verb} {name}"[:MAX_LABEL_LEN].rstrip()
    name_head = name[:14].rstrip()
    room = MAX_LABEL_LEN - len(name_head) - 2
    if room >= 8:
        return f"{name_head}: {details[:room].rstrip()}"
    return f"{verb} {name}"[:MAX_LABEL_LEN].rstrip()


class PresenceDataBuilder:
    """Builds ActivityWatch event data from Discord presence snapshots.

    Per-app colors are extracted from the activity artwork and cached in
    app_colors.json, keyed by application id (or name slug for activities
    without one, e.g. Discord's own game detection).
    """

    def __init__(self, cache_dir: Path):
        self.cache_path = cache_dir / "app_colors.json"
        self.cache: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self.cache, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.debug(f"Could not save app color cache: {exc}")

    def get_app_meta(self, activity: Dict[str, Any]) -> Dict[str, Any]:
        name = str(activity.get("name") or "Unknown")
        application_id = activity.get("application_id")
        app_key = str(application_id) if application_id else _slugify(name)
        cached = self.cache.get(app_key)
        if cached and cached.get("color"):
            return cached

        image_url = activity_image_url(activity)
        entry = {
            "key": app_key,
            "name": name,
            "icon_url": image_url,
            "color": extract_dominant_color(image_url),
        }
        self.cache[app_key] = entry
        self._save()
        return entry

    def build(self, snapshot: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
        activity = pick_primary_activity(snapshot.get("activities") or [])
        if not activity:
            return None

        name = str(activity.get("name") or "Unknown")
        details = activity.get("details")
        details = str(details) if details is not None else None
        state = activity.get("state")
        state = str(state) if state is not None else None
        raw_type = activity.get("type")
        type_name = ACTIVITY_TYPE_NAMES.get(raw_type, f"type_{raw_type}")
        verb = VERBS.get(raw_type, "Using")

        app_meta = self.get_app_meta(activity)
        title = f"{verb} {name}"
        if details:
            title += f" — {details}"
        if state:
            title += f" ({state})"

        assets = activity.get("assets") or {}
        party = activity.get("party") or {}
        size = party.get("size") or []
        timestamps = activity.get("timestamps") or {}

        data: Dict[str, Any] = {
            "label": build_label(verb, name, details),
            "title": title,
            "activity_type": type_name,
            "name": name,
            "details": details,
            "state": state,
            "url": activity.get("url"),
            "application_id": (
                str(activity["application_id"])
                if activity.get("application_id")
                else None
            ),
            "app_key": app_meta["key"],
            "icon_url": app_meta.get("icon_url"),
            "color": app_meta["color"],
            "color_key": f"discord-app:{app_meta['key']}",
            "user_id": str(user_id),
            "started_at": _iso_from_ms(timestamps.get("start")),
            "ends_at": _iso_from_ms(timestamps.get("end")),
            "track_id": activity.get("sync_id"),
            "party_current": _to_int(size[0]) if len(size) > 0 else None,
            "party_max": _to_int(size[1]) if len(size) > 1 else None,
            "large_text": assets.get("large_text"),
            "small_text": assets.get("small_text"),
        }
        return data
