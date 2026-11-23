"""run_cycle が record.json を Discord 処理から独立して保存することのテスト。"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import main
from asken_client import MEAL_LABEL, MEAL_TYPES, DailySummary, MealRecord
from discord_client import DiscordError

DATE = dt.date(2026, 11, 15)
DATE_STR = DATE.isoformat()
ITEM = {
    "item_hash": "h1",
    "menu_id": "1",
    "menu_name": "牛丼",
    "quantity": "1",
    "energy_kcal": "687",
}
SUMMARY = DailySummary(comment="コメント", nutrition=[])


def _meals():
    meals = {t: MealRecord(t, MEAL_LABEL[t]) for t in MEAL_TYPES}
    meals["lunch"] = MealRecord("lunch", "昼食", items=[ITEM])
    return meals


def _client(meals):
    client = mock.Mock()
    client.fetch_meal.side_effect = lambda meal_type, date: meals[meal_type]
    client.fetch_summary.return_value = SUMMARY
    return client


def _posted_state(meals):
    return {
        DATE_STR: {
            "lunch": {
                "hash": main.content_hash(meals["lunch"]),
                "message_id": "m1",
            },
            main.SUMMARY_SLOT_KEY: {
                "hash": main.day_meals_hash(list(meals.values())),
                "message_id": "m2",
            },
        }
    }


class RunCycleRecordTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(main, "track_dates", return_value=[DATE])
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)

    def test_投稿をスキップする日でも_record_json_を保存する(self):
        meals = _meals()
        discord = mock.Mock()

        main.run_cycle(
            _client(meals),
            discord,
            _posted_state(meals),
            num_days=1,
            data_dir=self.data_dir,
        )

        discord.post_message.assert_not_called()
        discord.edit_message.assert_not_called()
        record = json.loads(
            (self.data_dir / DATE_STR / "record.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["date"], DATE_STR)
        self.assertEqual(record["total_energy_kcal"], 687)
        self.assertEqual(record["meals"]["lunch"]["items"], [ITEM])
        self.assertEqual(record["summary"]["comment"], "コメント")

    def test_全削除した食事を編集し空の未投稿は投稿しない(self):
        old = _meals()
        old["lunch"].photo_urls = ["/photo"]
        state = _posted_state(old)
        meals = {t: MealRecord(t, MEAL_LABEL[t]) for t in MEAL_TYPES}
        client = _client(meals)
        client.fetch_summary.return_value = None
        discord = mock.Mock()

        main.run_cycle(
            client, discord, state, num_days=1, data_dir=self.data_dir
        )
        discord.edit_message.assert_called_once_with(
            "m1", "11/15 昼食\n(削除済み)", [], None
        )
        discord.post_message.assert_not_called()
        self.assertEqual(
            state[DATE_STR]["lunch"],
            {"hash": main.content_hash(meals["lunch"]), "message_id": "m1"},
        )
        main.run_cycle(
            client, discord, state, num_days=1, data_dir=self.data_dir
        )
        self.assertEqual(discord.edit_message.call_count, 1)

    def test_全削除の編集失敗後は状態を保持して次回再試行する(self):
        old = _meals()
        state = _posted_state(old)
        previous = state[DATE_STR]["lunch"].copy()
        meals = {t: MealRecord(t, MEAL_LABEL[t]) for t in MEAL_TYPES}
        client = _client(meals)
        client.fetch_summary.return_value = None
        discord = mock.Mock()
        discord.edit_message.side_effect = [DiscordError("failure"), None]

        with self.assertLogs("asken2discord", level="ERROR"):
            main.run_cycle(
                client, discord, state, num_days=1, data_dir=self.data_dir
            )
        self.assertEqual(state[DATE_STR]["lunch"], previous)
        main.run_cycle(
            client, discord, state, num_days=1, data_dir=self.data_dir
        )
        self.assertEqual(discord.edit_message.call_count, 2)
        self.assertEqual(
            state[DATE_STR]["lunch"]["hash"], main.content_hash(meals["lunch"])
        )

    def test_保存に失敗してもサイクルを続けて_Discord_に投稿する(self):
        meals = _meals()
        discord = mock.Mock()
        discord.post_message.return_value = "new"
        state = {}

        with mock.patch.object(
            main, "save_record", side_effect=OSError("disk full")
        ):
            with self.assertLogs("asken2discord", level="ERROR") as logs:
                main.run_cycle(
                    _client(meals),
                    discord,
                    state,
                    num_days=1,
                    data_dir=self.data_dir,
                )

        self.assertIn(f"Failed to save record for {DATE_STR}", logs.output[0])
        self.assertEqual(discord.post_message.call_count, 2)
        self.assertEqual(state[DATE_STR]["lunch"]["message_id"], "new")

    def test_写真取得に失敗した食事だけスキップして後続を続ける(self):
        meals = _meals()
        meals["breakfast"] = MealRecord(
            "breakfast", "朝食", items=[ITEM], photo_urls=["/p/1"]
        )
        client = _client(meals)
        client.download_photo.side_effect = requests.RequestException("boom")
        discord = mock.Mock()
        discord.post_message.return_value = "new"
        state = {}
        next_day = DATE + dt.timedelta(days=1)

        with mock.patch.object(
            main, "track_dates", return_value=[DATE, next_day]
        ):
            with self.assertLogs("asken2discord", level="ERROR") as logs:
                main.run_cycle(
                    client,
                    discord,
                    state,
                    num_days=2,
                    data_dir=self.data_dir,
                )

        self.assertIn(
            f"Failed to download photo for {DATE_STR} breakfast",
            logs.output[0],
        )
        self.assertNotIn("breakfast", state[DATE_STR])
        self.assertEqual(state[DATE_STR]["lunch"]["message_id"], "new")
        self.assertIn(main.SUMMARY_SLOT_KEY, state[DATE_STR])
        self.assertIn("lunch", state[next_day.isoformat()])

    def test_写真保存に失敗しても_Discord_に添付して投稿する(self):
        meals = _meals()
        meals["lunch"] = MealRecord(
            "lunch", "昼食", items=[ITEM], photo_urls=["/p/1"]
        )
        client = _client(meals)
        client.download_photo.return_value = b"jpeg"
        discord = mock.Mock()
        discord.post_message.return_value = "new"
        state = {}

        with mock.patch.object(
            Path, "write_bytes", side_effect=OSError("disk full")
        ):
            with self.assertLogs("asken2discord", level="ERROR") as logs:
                main.run_cycle(
                    client,
                    discord,
                    state,
                    num_days=1,
                    data_dir=self.data_dir,
                )

        self.assertIn(
            f"Failed to save photo for {DATE_STR} lunch", logs.output[0]
        )
        files = discord.post_message.call_args_list[0].args[1]
        self.assertIn(f"{DATE_STR}_lunch_1.jpg", [f[0] for f in files])
        self.assertEqual(state[DATE_STR]["lunch"]["message_id"], "new")

    def test_サマリーは日付ごとに_1_回だけ取得する(self):
        meals = _meals()
        client = _client(meals)

        with mock.patch.object(main, "track_dates", return_value=[DATE, DATE]):
            main.run_cycle(
                client,
                mock.Mock(),
                _posted_state(meals),
                num_days=2,
                data_dir=self.data_dir,
            )

        self.assertEqual(client.fetch_summary.call_count, 2)


if __name__ == "__main__":
    unittest.main()
