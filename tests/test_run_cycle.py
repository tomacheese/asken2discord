"""run_cycle が record.json を Discord 処理から独立して保存することのテスト。"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from asken_client import MEAL_LABEL, MEAL_TYPES, DailySummary, MealRecord

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

        self.assertIn(
            f"Failed to save record for {DATE_STR}", logs.output[0]
        )
        self.assertEqual(discord.post_message.call_count, 2)
        self.assertEqual(state[DATE_STR]["lunch"]["message_id"], "new")

    def test_サマリーは日付ごとに_1_回だけ取得する(self):
        meals = _meals()
        client = _client(meals)

        with mock.patch.object(
            main, "track_dates", return_value=[DATE, DATE]
        ):
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
