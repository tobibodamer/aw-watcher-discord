import re
import shutil
from pathlib import Path


def parse_env(env_path: Path) -> dict:
    values = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def main() -> None:
    repo_dir = Path(__file__).parent.resolve()
    local_appdata = Path.home() / "AppData" / "Local"
    aw_base = local_appdata / "activitywatch" / "activitywatch"

    # 1. Create AppData config file: aw-watcher-discord/aw-watcher-discord.toml
    env_vars = parse_env(repo_dir / ".env")
    client_id = env_vars.get("DISCORD_CLIENT_ID", "")
    client_secret = env_vars.get("DISCORD_CLIENT_SECRET", "")
    redirect_uri = env_vars.get("DISCORD_REDIRECT_URI", "http://127.0.0.1") or "http://127.0.0.1"

    config_dir = aw_base / "aw-watcher-discord"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "aw-watcher-discord.toml"

    toml_content = (
        "\n[aw-watcher-discord]\n"
        f'client_id = "{client_id}"\n'
        f'client_secret = "{client_secret}"\n'
        f'redirect_uri = "{redirect_uri}"\n'
        "poll_time = 5.0\n"
        "include_pings = false\n"
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
    if aw_qt_toml.exists():
        text = aw_qt_toml.read_text(encoding="utf-8")
        if '"aw-watcher-discord"' not in text:
            def _inject(match: re.Match) -> str:
                prefix, items, suffix = match.group(1), match.group(2), match.group(3)
                return f'{prefix}{items}, "aw-watcher-discord"{suffix}'

            updated = re.sub(
                r'(^autostart_modules\s*=\s*\[)([^\]]*)(\])',
                _inject,
                text,
                count=1,
                flags=re.MULTILINE,
            )
            aw_qt_toml.write_text(updated, encoding="utf-8")
            print(f"[4/4] Added 'aw-watcher-discord' to autostart_modules in {aw_qt_toml}")
        else:
            print(f"[4/4] 'aw-watcher-discord' is already in {aw_qt_toml}")


if __name__ == "__main__":
    main()
