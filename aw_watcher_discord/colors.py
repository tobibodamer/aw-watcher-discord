import colorsys
import io
import json
import logging
import urllib.request
from typing import Dict, List, Optional, Tuple, cast

from PIL import Image

logger = logging.getLogger(__name__)

DISCORD_BLURPLE = "#5865F2"


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    ri = min(255, max(0, int(round(r))))
    gi = min(255, max(0, int(round(g))))
    bi = min(255, max(0, int(round(b))))
    return f"#{ri:02X}{gi:02X}{bi:02X}"


def _mix_rgb(
    rgb1: Tuple[int, int, int], rgb2: Tuple[int, int, int], weight2: float
) -> Tuple[int, int, int]:
    w1 = 1.0 - weight2
    return (
        int(round(rgb1[0] * w1 + rgb2[0] * weight2)),
        int(round(rgb1[1] * w1 + rgb2[1] * weight2)),
        int(round(rgb1[2] * w1 + rgb2[2] * weight2)),
    )


def extract_dominant_color(icon_url: Optional[str]) -> str:
    if not icon_url:
        return DISCORD_BLURPLE

    try:
        req = urllib.request.Request(
            icon_url,
            headers={"User-Agent": "aw-watcher-discord/0.1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_bytes = resp.read()

        img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA").resize((64, 64))
        buf = img.tobytes()
        pixels = [
            (buf[i], buf[i + 1], buf[i + 2])
            for i in range(0, len(buf), 4)
            if buf[i + 3] > 128
        ]
        if not pixels:
            return DISCORD_BLURPLE

        rgb_img = Image.new("RGB", (len(pixels), 1))
        rgb_img.putdata(pixels)
        pal_img = rgb_img.quantize(colors=12)
        palette = pal_img.getpalette() or []
        # P-mode getcolors returns (count, palette index) pairs; the Pillow
        # stubs union this with the RGB tuple shape, hence the cast.
        counts = sorted(
            cast("List[Tuple[int, int]]", pal_img.getcolors() or []),
            reverse=True,
        )

        best_score = -1.0
        best_rgb: Optional[Tuple[int, int, int]] = None
        total = len(pixels)

        for count, idx in counts:
            base = idx * 3
            if base + 2 >= len(palette):
                continue
            r, g, b = palette[base], palette[base + 1], palette[base + 2]
            h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            freq = count / total
            if 0.12 < l < 0.88 and s > 0.18:
                score = (freq**0.45) * (0.3 + s) * (1.0 - abs(l - 0.55))
                if score > best_score:
                    best_score = score
                    best_rgb = (r, g, b)

        if best_rgb is None:
            return DISCORD_BLURPLE

        h, l, s = colorsys.rgb_to_hls(
            best_rgb[0] / 255.0, best_rgb[1] / 255.0, best_rgb[2] / 255.0
        )
        s = max(s, 0.58)
        l = min(max(l, 0.48), 0.64)
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        return _rgb_to_hex(r * 255.0, g * 255.0, b * 255.0)
    except Exception as exc:
        logger.debug(f"Could not extract icon color from {icon_url}: {exc}")
        return DISCORD_BLURPLE


def build_state_colors(base_hex: str) -> Dict[str, str]:
    base_rgb = _hex_to_rgb(base_hex)
    h, l, s = colorsys.rgb_to_hls(
        base_rgb[0] / 255.0, base_rgb[1] / 255.0, base_rgb[2] / 255.0
    )

    # Solo: lighter, softer tint of the server color
    solo_r, solo_g, solo_b = colorsys.hls_to_rgb(
        h, min(0.74, l + 0.12), max(0.38, s * 0.72)
    )
    solo_hex = _rgb_to_hex(solo_r * 255.0, solo_g * 255.0, solo_b * 255.0)

    # Muted: blend server color with warm amber (#F59E0B) and slightly desaturate
    muted_mix = _mix_rgb(base_rgb, (245, 158, 11), 0.42)
    mh, ml, ms = colorsys.rgb_to_hls(
        muted_mix[0] / 255.0, muted_mix[1] / 255.0, muted_mix[2] / 255.0
    )
    mr, mg, mb = colorsys.hls_to_rgb(mh, min(max(ml, 0.44), 0.58), min(ms, 0.55))
    muted_hex = _rgb_to_hex(mr * 255.0, mg * 255.0, mb * 255.0)

    # Deafened / AFK: dark desaturated slate-rose tint of the server color
    deaf_mix = _mix_rgb(base_rgb, (100, 116, 139), 0.62)
    dh, dl, ds = colorsys.rgb_to_hls(
        deaf_mix[0] / 255.0, deaf_mix[1] / 255.0, deaf_mix[2] / 255.0
    )
    dr, dg, db = colorsys.hls_to_rgb(dh, min(max(dl * 0.78, 0.32), 0.42), min(ds, 0.28))
    deafened_hex = _rgb_to_hex(dr * 255.0, dg * 255.0, db * 255.0)

    return {
        "active": base_hex,
        "solo": solo_hex,
        "muted": muted_hex,
        "deafened": deafened_hex,
    }


def sync_app_categories_to_aw(
    server_url: str,
    app_key: str,
    app_name: str,
    color: str,
) -> bool:
    try:
        settings_url = f"{server_url.rstrip('/')}/api/0/settings"
        with urllib.request.urlopen(settings_url, timeout=5) as resp:
            settings = json.loads(resp.read().decode("utf-8"))

        classes = list(settings.get("classes") or [])

        desired_rules = [
            {
                "name": ["Discord", app_name],
                "rule": {
                    "type": "regex",
                    "regex": f"^discord-app:{app_key}$",
                    "select_keys": ["color_key"],
                    "priority": 50,
                },
                "data": {"color": color},
            },
        ]

        changed = False
        for desired in desired_rules:
            existing_idx = next(
                (i for i, c in enumerate(classes) if c.get("name") == desired["name"]),
                None,
            )
            if existing_idx is None:
                classes.append(desired)
                changed = True
            elif classes[existing_idx] != desired:
                classes[existing_idx] = desired
                changed = True

        if changed:
            payload = json.dumps(classes).encode("utf-8")
            req = urllib.request.Request(
                f"{settings_url}/classes",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            logger.info(
                f"Synced ActivityWatch timeline color for Discord activity '{app_name}' ({color})"
            )
        return True
    except Exception as exc:
        logger.warning(f"Failed to sync Discord activity colors to ActivityWatch: {exc}")
        return False


def sync_guild_categories_to_aw(
    server_url: str,
    guild_id: str,
    guild_name: str,
    state_colors: Dict[str, str],
) -> bool:
    try:
        settings_url = f"{server_url.rstrip('/')}/api/0/settings"
        with urllib.request.urlopen(settings_url, timeout=5) as resp:
            settings = json.loads(resp.read().decode("utf-8"))

        classes = list(settings.get("classes") or [])

        desired_rules = [
            {
                "name": ["Comms", "Discord"],
                "rule": {"type": "none"},
                "data": {"color": DISCORD_BLURPLE},
            },
            {
                "name": ["Comms", "Discord", guild_name],
                "rule": {
                    "type": "regex",
                    "regex": f"^discord:{guild_id}:active$",
                    "select_keys": ["color_key"],
                    "priority": 50,
                },
                "data": {"color": state_colors["active"]},
            },
            {
                "name": ["Comms", "Discord", guild_name, "Solo"],
                "rule": {
                    "type": "regex",
                    "regex": f"^discord:{guild_id}:solo$",
                    "select_keys": ["color_key"],
                    "priority": 55,
                },
                "data": {"color": state_colors["solo"]},
            },
            {
                "name": ["Comms", "Discord", guild_name, "Muted"],
                "rule": {
                    "type": "regex",
                    "regex": f"^discord:{guild_id}:muted$",
                    "select_keys": ["color_key"],
                    "priority": 60,
                },
                "data": {"color": state_colors["muted"]},
            },
            {
                "name": ["Comms", "Discord", guild_name, "Deafened"],
                "rule": {
                    "type": "regex",
                    "regex": f"^discord:{guild_id}:deafened$",
                    "select_keys": ["color_key"],
                    "priority": 65,
                },
                "data": {"color": state_colors["deafened"]},
            },
        ]

        changed = False
        for desired in desired_rules:
            existing_idx = next(
                (i for i, c in enumerate(classes) if c.get("name") == desired["name"]),
                None,
            )
            if existing_idx is None:
                classes.append(desired)
                changed = True
            elif classes[existing_idx] != desired:
                classes[existing_idx] = desired
                changed = True

        if changed:
            payload = json.dumps(classes).encode("utf-8")
            req = urllib.request.Request(
                f"{settings_url}/classes",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            logger.info(
                f"Synced ActivityWatch timeline colors for Discord server '{guild_name}' ({state_colors['active']})"
            )
        return True
    except Exception as exc:
        logger.warning(f"Failed to sync Discord category colors to ActivityWatch: {exc}")
        return False
