"""Thin client for posting to and editing messages on a Discord webhook."""
from __future__ import annotations

import json
import logging
from types import TracebackType

import requests

logger = logging.getLogger(__name__)


class DiscordError(RuntimeError):
    """Raised when the Discord API returns a non-success status."""


class MessageNotFound(Exception):
    """Raised when editing a message that no longer exists (e.g. manually deleted)."""

    def __init__(self, message_id: str):
        super().__init__(f"message {message_id} not found")
        self.message_id = message_id


class DiscordWebhookClient:
    """Session-backed wrapper around a single Discord incoming webhook URL."""

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
        """Close the underlying HTTP session."""
        self.session.close()

    def _multipart(self, content: str, files: list[tuple[str, bytes, str]]) -> dict:
        """Build the multipart payload for a webhook create/edit request.

        `attachments` and `embeds` are always sent explicitly: on an edit the
        Discord API otherwise keeps the message's existing attachments and embeds,
        so old files pile up and embeds from an earlier message format linger.
        """
        attachments = [
            {"id": idx, "filename": filename} for idx, (filename, _, _) in enumerate(files)
        ]
        payload = {"content": content, "attachments": attachments, "embeds": []}
        multipart_files = {
            "payload_json": (None, json.dumps(payload, ensure_ascii=False), "application/json")
        }
        for idx, (filename, file_content, content_type) in enumerate(files):
            multipart_files[f"files[{idx}]"] = (filename, file_content, content_type)
        return multipart_files

    def post_message(self, content: str, files: list[tuple[str, bytes, str]]) -> str:
        """Post a new message and return its message id."""
        resp = self.session.post(
            f"{self.webhook_url}?wait=true",
            files=self._multipart(content, files),
            timeout=30,
        )
        if resp.status_code >= 300:
            raise DiscordError(f"Discord POST failed: {resp.status_code} {resp.text}")
        return resp.json()["id"]

    def edit_message(
        self, message_id: str, content: str, files: list[tuple[str, bytes, str]]
    ) -> None:
        """Edit an existing message, replacing its attachments wholesale.

        Raises MessageNotFound on a 404 so the caller can fall back to posting a
        new message when the original was deleted.
        """
        resp = self.session.patch(
            f"{self.webhook_url}/messages/{message_id}",
            files=self._multipart(content, files),
            timeout=30,
        )
        if resp.status_code == 404:
            logger.warning("message %s not found, will re-create", message_id)
            raise MessageNotFound(message_id)
        if resp.status_code >= 300:
            raise DiscordError(f"Discord PATCH failed: {resp.status_code} {resp.text}")
