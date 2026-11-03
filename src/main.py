"""あすけんの食事記録と日次サマリーを取得し、Discord に投稿・編集する常駐プロセス。

食事はプレーンテキスト + 画像添付、日次サマリーは embed で投稿する。

スケジューリングは cron やタイマーではなくこのプロセス自身のループで行うため、
コンテナは起動し続けるだけでよい。
各サイクルでは、あすけんにログインして対象日・食事区分を取得し、
前回サイクルから出力内容が変わった食事・サマリーについて Discord メッセージを投稿または編集する。
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
import sentry_sdk

from asken_client import MEAL_TYPES, AskenClient, LoginError, MealRecord
from discord_client import DiscordError, DiscordWebhookClient, MessageNotFound
from message_builder import build_meal_message, build_summary_embed
import state as state_store

JST = ZoneInfo("Asia/Tokyo")

# SENTRY_DSN が未設定なら自動的に無効になる。そのため、変数の有無で分岐せず
# 無条件に呼んでも安全。
sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN"),
    traces_sample_rate=0.0,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("asken2discord")

# message_builder の出力形式を変えたときに上げる。再計算したハッシュが保存済みの
# ものと一致しなくなり、投稿済みメッセージが再描画される。
MESSAGE_FORMAT_VERSION = 6

# 日次サマリーの状態スロットのキー。"advice" のまま変えてはいけない。
# 投稿済みサマリーメッセージが state.json に保存されているキーであり、変更すると
# それらが孤立して、その場で編集されず重複投稿されてしまう。
SUMMARY_SLOT_KEY = "advice"

_shutdown = threading.Event()


@dataclass(frozen=True)
class Config:
    """プロセスの環境変数から解決した実行時設定。"""

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
        """環境変数から Config を組み立てる。

        必須変数が未設定なら、黙ってデフォルト値を使わず KeyError を送出する。
        秘匿情報でない変数の既定値は Dockerfile の ENV にあるため、コード側で
        二重に既定値を持たず、ここでは必須として読む。
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


def content_hash(meal: MealRecord) -> str:
    """食事メッセージの出力に影響するすべての要素を対象にしたハッシュを計算する。"""
    payload = {
        "format_version": MESSAGE_FORMAT_VERSION,
        "items": meal.items,
        "photo_urls": meal.photo_urls,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def day_meals_hash(meals: list[MealRecord]) -> str:
    """1 日分の入力済みメニュー項目のハッシュを計算する。サマリー更新の要否判定に使う。

    あすけんは、何も入力が変わらなくても取得のたびにサマリーコメントをランダムに別の
    言い回しへ差し替える。コメント本文をハッシュすると、毎サイクルメッセージが
    「更新」されてしまう。そこで 1 日分のメニュー項目をハッシュし、実際の入力内容が
    変わったときだけサマリーを更新する。
    """
    payload = {
        "format_version": MESSAGE_FORMAT_VERSION,
        "items": [meal.items for meal in meals],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def track_dates(today: dt.date, num_days: int) -> list[dt.date]:
    """追跡対象の日付を、古い順で今日が最後になるように返す。"""
    return [today - dt.timedelta(days=i) for i in range(num_days - 1, -1, -1)]


def _post_or_edit(
    discord: DiscordWebhookClient,
    slot: dict | None,
    content: str,
    files: list[tuple[str, bytes, str]],
    *,
    log_context: str,
    embeds: list[dict] | None = None,
) -> str | None:
    """新規メッセージを投稿するか、スロットの既存メッセージを編集し、メッセージ ID を返す。

    Discord への失敗時は送出せず None を返す。呼び出し側がこの項目だけを
    スキップし、サイクルの残りの処理を続けられるようにするため。
    """
    try:
        if slot:
            try:
                discord.edit_message(slot["message_id"], content, files, embeds)
                return slot["message_id"]
            except MessageNotFound:
                return discord.post_message(content, files, embeds)
        return discord.post_message(content, files, embeds)
    except (DiscordError, requests.RequestException):
        logger.exception("Discord post/edit failed for %s", log_context)
        return None


def run_cycle(
    client: AskenClient,
    discord: DiscordWebhookClient,
    state: dict,
    *,
    num_days: int,
    data_dir: Path,
) -> dict:
    """取得と同期を 1 サイクル実行し、更新後の状態を返す。"""
    client.login()
    today = dt.datetime.now(JST).date()

    for date in track_dates(today, num_days):
        date_str = date.isoformat()
        meals = {meal_type: client.fetch_meal(meal_type, date_str) for meal_type in MEAL_TYPES}

        for meal_type, meal in meals.items():
            if not meal.has_content:
                continue

            new_hash = content_hash(meal)

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
                photo_files=photo_files,
            )

            message_id = _post_or_edit(
                discord, slot, message_content, files, log_context=f"{date_str} {meal_type}"
            )
            if message_id is None:
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

        summary = client.fetch_summary(date_str)
        if summary:
            new_day_hash = day_meals_hash(list(meals.values()))
            summary_slot = state_store.get_slot(state, date_str, SUMMARY_SLOT_KEY)
            if not summary_slot or summary_slot["hash"] != new_day_hash:
                embed = build_summary_embed(date_str=date_str, summary=summary)
                message_id = _post_or_edit(
                    discord,
                    summary_slot,
                    "",
                    [],
                    log_context=f"{date_str} summary",
                    embeds=[embed],
                )
                if message_id is not None:
                    state_store.set_slot(
                        state,
                        date_str,
                        SUMMARY_SLOT_KEY,
                        content_hash=new_day_hash,
                        message_id=message_id,
                    )
                    logger.info(
                        "%s summary -> %s (message_id=%s)",
                        date_str,
                        "edited" if summary_slot else "posted",
                        message_id,
                    )

    return state


def _request_shutdown(signum: int, frame: FrameType | None) -> None:
    """現在のサイクルの終了後にメインループを止めるよう要求するシグナルハンドラ。"""
    logger.info("Received signal %s, shutting down after current cycle", signum)
    _shutdown.set()


def main() -> None:
    """設定を読み込み、中断されるまで取得サイクルを繰り返す。"""
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
                # 1 サイクルの失敗で常駐プロセスを落としてはならない。
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
