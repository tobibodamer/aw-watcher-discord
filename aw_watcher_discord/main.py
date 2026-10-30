#!/usr/bin/env python3

import argparse
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from aw_client.client import ActivityWatchClient
from aw_core import dirs
from aw_core.config import load_config_toml
from aw_core.log import setup_logging
from aw_core.models import Event

from aw_watcher_discord.rpc import DiscordIPCError, DiscordRPCClient

WATCHER_NAME = "aw-watcher-discord"
EVENT_TYPE = "general.stopwatch.discord"

DEFAULT_CONFIG = """
[aw-watcher-discord]
client_id = ""
client_secret = ""
redirect_uri = "http://127.0.0.1"
poll_time = 5.0
include_pings = false
"""


def load_config():
    return load_config_toml(WATCHER_NAME, DEFAULT_CONFIG)


def main() -> None:
    argparser = argparse.ArgumentParser(description="ActivityWatch Discord voice RPC watcher")
    argparser.add_argument("--testing", action="store_true")
    argparser.add_argument("--verbose", action="store_true")
    args = argparser.parse_args()

    setup_logging(
        name=WATCHER_NAME,
        testing=args.testing,
        verbose=args.verbose,
        log_stderr=True,
        log_file=True,
    )

    config_dir = dirs.get_config_dir(WATCHER_NAME)
    cache_dir = Path(dirs.get_cache_dir(WATCHER_NAME))
    config = load_config()

    section = config[WATCHER_NAME]
    poll_time = float(section.get("poll_time", 5.0))
    client_id = str(section.get("client_id", "")).strip()
    client_secret = str(section.get("client_secret", "")).strip()
    redirect_uri = str(section.get("redirect_uri", "http://127.0.0.1")).strip()
    include_pings = bool(section.get("include_pings", False))

    if not client_id or not client_secret:
        logging.warning(
            f"client_id or client_secret not specified in config file (in folder {config_dir}). "
            "Get your client_id and client_secret at https://discord.com/developers/applications"
        )
        sys.exit(1)

    aw = ActivityWatchClient(WATCHER_NAME, testing=args.testing)
    bucketname = f"{aw.client_name}_{aw.client_hostname}"
    aw.create_bucket(bucketname, EVENT_TYPE, queued=True)
    aw.connect()

    rpc = DiscordRPCClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        cache_dir=cache_dir,
    )

    was_in_voice = False
    last_title = None

    while True:
        try:
            if rpc.pipe is None:
                rpc.connect()

            voice_data = rpc.fetch_voice_event_data(include_volatile_pings=include_pings)
            if voice_data:
                title = voice_data["title"]
                if not was_in_voice or title != last_title:
                    logging.info(
                        f"Active voice call: {title} "
                        f"(mute={voice_data['self_mute']}, deaf={voice_data['self_deaf']})"
                    )
                    was_in_voice = True
                    last_title = title

                event = Event(timestamp=datetime.now(timezone.utc), data=voice_data)
                aw.heartbeat(
                    bucketname,
                    event,
                    pulsetime=poll_time + 5.0,
                    queued=True,
                    commit_interval=poll_time,
                )
            else:
                if was_in_voice:
                    logging.info(f"Left voice call: {last_title}")
                    was_in_voice = False
                    last_title = None

        except DiscordIPCError as exc:
            if was_in_voice:
                logging.info(f"Disconnected from voice call ({exc})")
                was_in_voice = False
                last_title = None
            rpc.close()
            logging.debug(f"Waiting for Discord desktop client: {exc}")
            sleep(max(10.0, poll_time))
            continue
        except KeyboardInterrupt:
            logging.info("Stopping aw-watcher-discord")
            rpc.close()
            break
        except Exception:
            logging.error("Unexpected error in aw-watcher-discord loop")
            logging.error(traceback.format_exc())
            rpc.close()
            sleep(5.0)
            continue

        sleep(poll_time)


if __name__ == "__main__":
    main()
