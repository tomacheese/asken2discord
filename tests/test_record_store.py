"""record_store の組み立て・保存のユニットテスト。"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from asken_client import (
    MEAL_LABEL,
    MEAL_TYPES,
    DailySummary,
    MealRecord,
    NutritionRow,
)
from record_store import build_record, save_record

NOW = dt.datetime(2026, 11, 16, 9, 30, tzinfo=ZoneInfo("Asia/Tokyo"))


def _meal(meal_type, items=(), photo_urls=()):
    return MealRecord(
        meal_type=meal_type,
        meal_label=MEAL_LABEL[meal_type],
        items=list(items),
        photo_urls=list(photo_urls),
    )


def _item(name, kcal, item_hash="h"):
    return {
        "item_hash": item_hash,
        "menu_id": "1",
        "menu_name": name,
        "quantity": "1",
        "energy_kcal": kcal,
    }


class BuildRecordTest(unittest.TestCase):
    def test_全区分に記録がある場合は区分ごとと日合計を保存する(self):
        meals = {
            "breakfast": _meal("breakfast", [_item("a", "300")]),
            "lunch": _meal("lunch", [_item("b", "500"), _item("c", 200)]),
            "dinner": _meal("dinner", [_item("d", "700")]),
            "sweets": _meal("sweets", [_item("e", "130")]),
        }
        record = build_record("2026-11-15", meals, None, NOW)

        self.assertEqual(record["date"], "2026-11-15")
        self.assertEqual(record["updated_at"], "2026-11-16T09:30:00+09:00")
        self.assertEqual(list(record["meals"]), MEAL_TYPES)
        self.assertEqual(record["meals"]["lunch"]["label"], "昼食")
        self.assertEqual(
            record["meals"]["lunch"]["items"], meals["lunch"].items
        )
        self.assertEqual(record["meals"]["lunch"]["total_energy_kcal"], 700)
        self.assertEqual(record["total_energy_kcal"], 300 + 700 + 700 + 130)

    def test_記録のない区分は空として含まれる(self):
        meals = {t: _meal(t) for t in MEAL_TYPES}
        meals["dinner"] = _meal("dinner", [_item("d", "700")])
        record = build_record("2026-11-15", meals, None, NOW)

        self.assertEqual(
            record["meals"]["breakfast"],
            {"label": "朝食", "items": [], "total_energy_kcal": 0, "photos": []},
        )
        self.assertEqual(record["total_energy_kcal"], 700)

    def test_meals_に無い区分も空として含まれる(self):
        record = build_record("2026-11-15", {}, None, NOW)

        self.assertEqual(list(record["meals"]), MEAL_TYPES)
        self.assertEqual(record["meals"]["sweets"]["label"], "間食")
        self.assertEqual(record["total_energy_kcal"], 0)

    def test_写真は命名規則のファイル名で記録する(self):
        meals = {"lunch": _meal("lunch", photo_urls=["/a", "/b"])}
        record = build_record("2026-11-15", meals, None, NOW)

        self.assertEqual(
            record["meals"]["lunch"]["photos"], ["lunch_1.jpg", "lunch_2.jpg"]
        )

    def test_サマリーがなければ_null(self):
        record = build_record("2026-11-15", {}, None, NOW)

        self.assertIsNone(record["summary"])

    def test_サマリーはコメントと栄養素を保存する(self):
        summary = DailySummary(
            comment="こんにちは",
            nutrition=[NutritionRow("たんぱく質", "fit", "60g", "50〜70g")],
        )
        record = build_record("2026-11-15", {}, summary, NOW)

        self.assertEqual(
            record["summary"],
            {
                "comment": "こんにちは",
                "nutrition": [
                    {
                        "name": "たんぱく質",
                        "status": "fit",
                        "value": "60g",
                        "target_range": "50〜70g",
                    }
                ],
            },
        )

    def test_json_に変換できる(self):
        meals = {"breakfast": _meal("breakfast", [_item("a", None)])}
        record = build_record("2026-11-15", meals, None, NOW)

        self.assertEqual(json.loads(json.dumps(record)), record)


class SaveRecordTest(unittest.TestCase):
    def test_日付ディレクトリを作って_record_json_に書く(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {"date": "2026-11-15", "meals": {}}
            save_record(Path(tmp), "2026-11-15", record)

            path = Path(tmp) / "2026-11-15" / "record.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, record)

    def test_上書きで更新され一時ファイルが残らない(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_record(Path(tmp), "2026-11-15", {"v": 1})
            save_record(Path(tmp), "2026-11-15", {"v": 2})

            day_dir = Path(tmp) / "2026-11-15"
            saved = json.loads(
                (day_dir / "record.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved, {"v": 2})
            self.assertEqual(
                [p.name for p in day_dir.iterdir()], ["record.json"]
            )


if __name__ == "__main__":
    unittest.main()
