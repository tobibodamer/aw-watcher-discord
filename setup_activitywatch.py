import re
import shutil
from pathlib import Path


def parse_env(env_path: Path) -> dict:
    values = {}
    if not env_path.exists():
        return values
    # utf-8-sig tolerates a BOM (e.g. from PowerShell 5.1's Set-Content -Encoding utf8),
    # which would otherwise end up in the first key and silently blank DISCORD_CLIENT_ID.
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


WATCHER_MODULE = "aw-watcher-discord"
# aw-qt's built-in default, used when the config has no active autostart_modules line
# and no commented-out template line to copy it from.
DEFAULT_AUTOSTART_MODULES = ["aw-server", "aw-watcher-afk", "aw-watcher-window"]

_SECTION_RE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")


def parse_toml_section(text: str, section: str) -> dict:
    """Parse the flat `key = value` pairs of a single TOML section as strings."""
    values = {}
    in_section = False
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            in_section = m.group(1) == section
            continue
        if in_section and "=" in line:
            key, _, value = line.partition("=")
            value = value.split("#", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


# [^\]]* also spans newlines, so multi-line arrays are matched as a whole.
_AUTOSTART_RE = re.compile(r"^([ \t]*autostart_modules[ \t]*=[ \t]*\[)([^\]]*)(\])", re.MULTILINE)
# A fresh ActivityWatch install ships the setting commented out, e.g.
#   #autostart_modules = ["aw-server", "aw-watcher-afk", "aw-watcher-window"]
_COMMENTED_AUTOSTART_RE = re.compile(r"^[ \t]*#[ \t]*autostart_modules[ \t]*=[ \t]*\[([^\]\n]*)\]", re.MULTILINE)


def _parse_modules(array_body: str) -> list:
    items = []
    for line in array_body.splitlines():
        for part in line.split("#", 1)[0].split(","):
            part = part.strip().strip('"').strip("'")
            if part:
                items.append(part)
    return items


def _format_modules(modules: list) -> str:
    return ", ".join(f'"{m}"' for m in modules)


def add_autostart_module(text: str, module: str) -> tuple:
    """Ensure `module` is in [aw-qt].autostart_modules.

    Returns (new_text, status) where status is "present", "appended" or "created".
    Only the [aw-qt] section is touched, never [aw-qt-testing].
    """
    lines = text.splitlines()
    section_start = None
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if not m:
            continue
        if section_start is not None:
            section_end = i
            break
        if m.group(1) == "aw-qt":
            section_start = i

    if section_start is None:
        new_line = f"autostart_modules = [{_format_modules(DEFAULT_AUTOSTART_MODULES + [module])}]"
        prefix = lines + [""] if lines and lines[-1].strip() else lines
        return "\n".join(prefix + ["[aw-qt]", new_line]) + "\n", "created"

    head = lines[: section_start + 1]
    body = "\n".join(lines[section_start + 1 : section_end])
    tail = lines[section_end:]

    def _join(new_body: str) -> str:
        return "\n".join(head + ([new_body] if new_body else []) + tail) + "\n"

    m = _AUTOSTART_RE.search(body)
    if m:
        items = _parse_modules(m.group(2))
        if module in items:
            return text, "present"
        replacement = f"{m.group(1)}{_format_modules(items + [module])}{m.group(3)}"
        return _join(body[: m.start()] + replacement + body[m.end() :]), "appended"

    commented = _COMMENTED_AUTOSTART_RE.search(body)
    defaults = _parse_modules(commented.group(1)) if commented else DEFAULT_AUTOSTART_MODULES
    if module not in defaults:
        defaults = defaults + [module]
    new_line = f"autostart_modules = [{_format_modules(defaults)}]"
    return _join(new_line + ("\n" + body if body else "")), "created"


def main() -> None:
    repo_dir = Path(__file__).parent.resolve()
    local_appdata = Path.home() / "AppData" / "Local"
    aw_base = local_appdata / "activitywatch" / "activitywatch"

    # 1. Create AppData config file: aw-watcher-discord/aw-watcher-discord.toml
    env_vars = parse_env(repo_dir / ".env")

    config_dir = aw_base / "aw-watcher-discord"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "aw-watcher-discord.toml"
    # .env wins over the existing config file, which wins over defaults, so
    # re-running setup never wipes manually set options like bot_token.
    existing = parse_toml_section(
        config_file.read_text(encoding="utf-8-sig") if config_file.exists() else "",
        WATCHER_MODULE,
    )

    def resolve(env_key: str, toml_key: str, default: str) -> str:
        env_val = env_vars.get(env_key, "")
        if env_val:
            return env_val
        if toml_key in existing and existing[toml_key] != "":
            return existing[toml_key]
        return default

    client_id = resolve("DISCORD_CLIENT_ID", "client_id", "")
    client_secret = resolve("DISCORD_CLIENT_SECRET", "client_secret", "")
    redirect_uri = (
        resolve("DISCORD_REDIRECT_URI", "redirect_uri", "http://127.0.0.1")
        or "http://127.0.0.1"
    )
    try:
        poll_time = float(resolve("", "poll_time", "5.0"))
    except ValueError:
        poll_time = 5.0
    include_pings = resolve("", "include_pings", "false").lower() in ("true", "1")
    include_raw = resolve("", "include_raw", "false").lower() in ("true", "1")
    presence_source = resolve("PRESENCE_SOURCE", "presence_source", "off").strip().lower()
    bot_token = resolve("DISCORD_BOT_TOKEN", "bot_token", "")
    presence_user_id = resolve("DISCORD_PRESENCE_USER_ID", "presence_user_id", "")

    toml_content = (
        "\n[aw-watcher-discord]\n"
        f'client_id = "{client_id}"\n'
        f'client_secret = "{client_secret}"\n'
        f'redirect_uri = "{redirect_uri}"\n'
        f"poll_time = {poll_time}\n"
        f"include_pings = {str(include_pings).lower()}\n"
        f"include_raw = {str(include_raw).lower()}\n"
        f'presence_source = "{presence_source}"\n'
        f'bot_token = "{bot_token}"\n'
        f'presence_user_id = "{presence_user_id}"\n'
    )
    config_file.write_text(toml_content, encoding="utf-8")
    print(f"[1/4] Wrote config to {config_file}")

    # 2. Copy token_cache.json to AppData Cache/aw-watcher-discord/token_cache.json
    cache_dir = aw_base / "Cache" / "aw-watcher-discord"
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_token = repo_dir / "token_cache.json"
    if local_token.exists():
        target_token = cache_dir / "token_cache.json"
        shutil.copy2(local_token, target_token)
        print(f"[2/4] Copied OAuth2 token cache to {target_token}")
    else:
        print(f"[2/4] Cache directory ready at {cache_dir}")

    # 3. Copy .venv\Scripts\aw-watcher-discord.exe to ~/.local/bin/aw-watcher-discord.exe
    venv_exe = repo_dir / ".venv" / "Scripts" / "aw-watcher-discord.exe"
    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    target_exe = local_bin / "aw-watcher-discord.exe"
    if venv_exe.exists():
        shutil.copy2(venv_exe, target_exe)
        print(f"[3/4] Installed system module binary at {target_exe}")
    else:
        print(f"[3/4] Warning: {venv_exe} not found yet.")

    # 4. Add "aw-watcher-discord" to aw-qt/aw-qt.toml autostart_modules
    aw_qt_toml = aw_base / "aw-qt" / "aw-qt.toml"
    text = aw_qt_toml.read_text(encoding="utf-8-sig") if aw_qt_toml.exists() else ""
    updated, status = add_autostart_module(text, WATCHER_MODULE)
    if status == "present":
        print(f"[4/4] '{WATCHER_MODULE}' is already in autostart_modules in {aw_qt_toml}")
    else:
        aw_qt_toml.parent.mkdir(parents=True, exist_ok=True)
        aw_qt_toml.write_text(updated, encoding="utf-8")
        print(f"[4/4] Added '{WATCHER_MODULE}' to autostart_modules in {aw_qt_toml}")


if __name__ == "__main__":
    main()
