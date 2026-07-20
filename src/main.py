"""Long-running process that fetches asken meal records and posts or edits them
in Discord as plain text with image attachments.

Scheduling is handled by this process's own loop rather than cron or a timer, so
the container only needs to stay running. Each cycle logs in to asken, fetches the
tracked days and meal types, and posts or edits the Discord message for any meal
whose rendered content has changed since the last cycle.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

import requests

from asken_client import MEAL_TYPES, Advice, AskenClient, LoginError, MealRecord
from discord_client import DiscordError, DiscordWebhookClient, MessageNotFound
from message_builder import build_meal_message
import state as state_store

JST = ZoneInfo("Asia/Tokyo")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("asken2discord")

# Bump when message_builder's output format changes: the recomputed hash then
# stops matching the stored one, forcing already-posted messages to be re-rendered.
MESSAGE_FORMAT_VERSION = 3

_shutdown = threading.Event()


@dataclass(frozen=True)
class Config:
    """Runtime configuration resolved from the process environment."""

    asken_username: str
    asken_password: str
    discord_webhook: str
    track_days: int
    interval_seconds: int
    state_path: Path
    data_dir: Path
    run_once: bool

    @classmethod
    def from_env(cls) -> Config:
        """Build a Config from environment variables.

        Missing required variables raise KeyError rather than being silently
        defaulted. Defaults for the non-secret variables live in the Dockerfile's
        ENV instructions, so they are read as required here instead of being
        defaulted a second time in code.
        """
        return cls(
            asken_username=os.environ["ASKEN_USERNAME"],
            asken_password=os.environ["ASKEN_PASSWORD"],
            discord_webhook=os.environ["DISCORD_WEBHOOK"],
            track_days=int(os.environ["ASKEN_TRACK_DAYS"]),
            interval_seconds=int(os.environ["INTERVAL_SECONDS"]),
            state_path=Path(os.environ["STATE_FILE"]),
            data_dir=Path(os.environ["DATA_DIR"]),
            run_once=os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes"),
        )


def content_hash(meal: MealRecord, advice_text: str | None) -> str:
    """Compute a hash covering everything that affects the rendered message."""
    payload = {
        "format_version": MESSAGE_FORMAT_VERSION,
        "items": meal.items,
        "photo_urls": meal.photo_urls,
        "advice": advice_text,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def matched_advice(advice: Advice | None, meal_label: str) -> Advice | None:
    """Return the advice only when its title refers to this meal.

    asken exposes a single advice per day (for the last meal recorded that day),
    so the meal it applies to is inferred from the meal label in its title.
    """
    if advice and meal_label in advice.title:
        return advice
    return None


def track_dates(today: dt.date, num_days: int) -> list[dt.date]:
    """Return the dates to track, oldest first, ending at today."""
    return [today - dt.timedelta(days=i) for i in range(num_days - 1, -1, -1)]


def run_cycle(
    client: AskenClient,
    discord: DiscordWebhookClient,
    state: dict,
    *,
    num_days: int,
    data_dir: Path,
) -> dict:
    """Run one fetch-and-sync cycle, returning the updated state."""
    client.login()
    today = dt.datetime.now(JST).date()

    for date in track_dates(today, num_days):
        date_str = date.isoformat()
        advice = client.fetch_advice(date_str)

        for meal_type in MEAL_TYPES:
            meal = client.fetch_meal(meal_type, date_str)
            if not meal.has_content:
                continue

            advice_for_meal = matched_advice(advice, meal.meal_label)
            advice_text = advice_for_meal.body if advice_for_meal else None
            new_hash = content_hash(meal, advice_text)

            slot = state_store.get_slot(state, date_str, meal_type)
            if slot and slot["hash"] == new_hash:
                continue

            photo_files = []
            for i, photo_url in enumerate(meal.photo_urls, start=1):
                photo_bytes = client.download_photo(photo_url)
                photo_files.append((f"{date_str}_{meal_type}_{i}.jpg", photo_bytes))
                photos_dir = data_dir / date_str / "photos"
                photos_dir.mkdir(parents=True, exist_ok=True)
                (photos_dir / f"{meal_type}_{i}.jpg").write_bytes(photo_bytes)

            message_content, files = build_meal_message(
                date_str=date_str,
                meal=meal,
                advice=advice_for_meal,
                photo_files=photo_files,
            )

            try:
                if slot:
                    try:
                        discord.edit_message(slot["message_id"], message_content, files)
                        message_id = slot["message_id"]
                    except MessageNotFound:
                        message_id = discord.post_message(message_content, files)
                else:
                    message_id = discord.post_message(message_content, files)
            except (DiscordError, requests.RequestException):
                # Skip this meal but keep processing the rest of the cycle.
                logger.exception("Discord post/edit failed for %s %s", date_str, meal_type)
                continue

            state_store.set_slot(
                state, date_str, meal_type, content_hash=new_hash, message_id=message_id
            )
            logger.info(
                "%s %s -> %s (message_id=%s)",
                date_str,
                meal.meal_label,
                "edited" if slot else "posted",
                message_id,
            )

    return state


def _request_shutdown(signum: int, frame: FrameType | None) -> None:
    """Signal handler that asks the main loop to stop after the current cycle."""
    logger.info("Received signal %s, shutting down after current cycle", signum)
    _shutdown.set()


def main() -> None:
    """Load configuration and run fetch cycles until interrupted."""
    config = Config.from_env()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    with (
        AskenClient(config.asken_username, config.asken_password) as client,
        DiscordWebhookClient(config.discord_webhook) as discord,
    ):
        while not _shutdown.is_set():
            state = state_store.load_state(config.state_path)
            try:
                state = run_cycle(
                    client,
                    discord,
                    state,
                    num_days=config.track_days,
                    data_dir=config.data_dir,
                )
            except LoginError:
                logger.exception("Login to asken.jp failed")
            except Exception:
                # A single failed cycle must not kill the long-running daemon.
                logger.exception("Unexpected error during cycle")
            finally:
                state_store.save_state(config.state_path, state)

            if config.run_once:
                break
            logger.info("Sleeping %s seconds until next cycle", config.interval_seconds)
            if _shutdown.wait(config.interval_seconds):
                break


if __name__ == "__main__":
    main()
