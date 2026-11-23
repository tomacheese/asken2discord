import unittest
from unittest import mock

import requests
import sentry_sdk
from sentry_sdk.transport import Transport

import main
from discord_client import DiscordError, DiscordWebhookClient


class EventTransport(Transport):
    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.get_event())


class DiscordFailureTest(unittest.TestCase):
    def test_接続失敗の例外チェーンをログと_Sentry_に送らない(self):
        token = "dummy-webhook-token-17"
        url = f"https://discord.com/api/webhooks/123/{token}"
        for method, slot in (("post", None), ("patch", {"message_id": "456"})):
            with self.subTest(method=method):
                transport = EventTransport()
                sentry_client = sentry_sdk.Client(
                    dsn="https://public@example.com/1", transport=transport
                )
                self.addCleanup(sentry_client.close)
                with sentry_sdk.isolation_scope():
                    sentry_sdk.get_current_scope().set_client(sentry_client)
                    with DiscordWebhookClient(url) as client:
                        try:
                            raise OSError(f"connection failed: {url}")
                        except OSError as cause:
                            error = requests.ConnectionError(
                                f"request failed: {url}"
                            )
                            error.__cause__ = cause

                        def fail(*args, **kwargs):
                            sentry_sdk.add_breadcrumb(
                                category="httplib", data={"url": url}
                            )
                            raise error

                        with mock.patch.object(
                            client.session, method, side_effect=fail
                        ):
                            with self.assertLogs(
                                "asken2discord", level="ERROR"
                            ) as logs:
                                result = main._post_or_edit(
                                    client, slot, "content", [],
                                    log_context="test"
                                )
                self.assertIsNone(result)
                self.assertEqual(len(transport.events), 1)
                self.assertIn("ConnectionError", logs.output[0])
                self.assertNotIn(token, str(logs.output))
                self.assertNotIn(token, str(transport.events))
                self.assertIsNone(logs.records[0].exc_info)

    def test_Discord_API_エラーの本文をログに送らない(self):
        token = "dummy-webhook-token-17"
        client = mock.Mock()
        client.post_message.side_effect = DiscordError(f"failed: {token}")
        with self.assertLogs("asken2discord", level="ERROR") as logs:
            result = main._post_or_edit(
                client, None, "content", [], log_context="test"
            )
        self.assertIsNone(result)
        self.assertNotIn(token, str(logs.output))
        self.assertIsNone(logs.records[0].exc_info)
