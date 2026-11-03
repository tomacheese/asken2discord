"""Discord Webhook へのメッセージ投稿・編集を行う薄いクライアント。"""
from __future__ import annotations

import json
import logging
from types import TracebackType

import requests

logger = logging.getLogger(__name__)


class DiscordError(RuntimeError):
    """Discord API が成功以外のステータスを返したときに送出する。"""


class MessageNotFound(Exception):
    """存在しないメッセージ (手動削除など) を編集しようとしたときに送出する。"""

    def __init__(self, message_id: str):
        super().__init__(f"message {message_id} not found")
        self.message_id = message_id


class DiscordWebhookClient:
    """単一の Discord Incoming Webhook URL を扱う、セッション付きのラッパー。"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.rstrip("/")
        self.session = requests.Session()

    def __enter__(self) -> DiscordWebhookClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """内部の HTTP セッションを閉じる。"""
        self.session.close()

    def _multipart(
        self,
        content: str,
        files: list[tuple[str, bytes, str]],
        embeds: list[dict] | None = None,
    ) -> dict:
        """Webhook の作成・編集リクエスト用の multipart ペイロードを組み立てる。

        `attachments` と `embeds` は常に明示的に送る。編集時に省略すると Discord API が
        既存の添付ファイルと embed を保持するため、古いファイルが溜まり、
        過去のメッセージ形式の embed が残ってしまう。
        """
        attachments = [
            {"id": idx, "filename": filename} for idx, (filename, _, _) in enumerate(files)
        ]
        payload = {"content": content, "attachments": attachments, "embeds": embeds or []}
        multipart_files = {
            "payload_json": (None, json.dumps(payload, ensure_ascii=False), "application/json")
        }
        for idx, (filename, file_content, content_type) in enumerate(files):
            multipart_files[f"files[{idx}]"] = (filename, file_content, content_type)
        return multipart_files

    def post_message(
        self,
        content: str,
        files: list[tuple[str, bytes, str]],
        embeds: list[dict] | None = None,
    ) -> str:
        """新規メッセージを投稿し、そのメッセージ ID を返す。"""
        resp = self.session.post(
            f"{self.webhook_url}?wait=true",
            files=self._multipart(content, files, embeds),
            timeout=30,
        )
        if resp.status_code >= 300:
            raise DiscordError(f"Discord POST failed: {resp.status_code} {resp.text}")
        return resp.json()["id"]

    def edit_message(
        self,
        message_id: str,
        content: str,
        files: list[tuple[str, bytes, str]],
        embeds: list[dict] | None = None,
    ) -> None:
        """既存メッセージを編集し、添付ファイルと embed を丸ごと置き換える。

        404 のときは MessageNotFound を送出する。元のメッセージが削除されていた場合に、
        呼び出し側が新規投稿へフォールバックできるようにするため。
        """
        resp = self.session.patch(
            f"{self.webhook_url}/messages/{message_id}",
            files=self._multipart(content, files, embeds),
            timeout=30,
        )
        if resp.status_code == 404:
            logger.warning("message %s not found, will re-create", message_id)
            raise MessageNotFound(message_id)
        if resp.status_code >= 300:
            raise DiscordError(f"Discord PATCH failed: {resp.status_code} {resp.text}")
