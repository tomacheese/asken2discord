"""食事記録と日次サマリーを、日付ごとの構造化データ (record.json) として保存する。

Discord への投稿状況とは独立した、分析用のローカルデータ。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from pathlib import Path

from asken_client import MEAL_LABEL, MEAL_TYPES, DailySummary, MealRecord
import state as state_store


def build_record(
    date_str: str,
    meals: dict[str, MealRecord],
    summary: DailySummary | None,
    now: dt.datetime,
) -> dict:
    """1 日分の record を組み立てる。

    `meals` に無い食事区分や記録のない区分も、空の項目として必ず含める。
    `photos` は保存先ファイル名の命名規則から作り、ファイルの実在は確認しない。
    """
    meal_records = {}
    for meal_type in MEAL_TYPES:
        meal = meals.get(meal_type)
        items = meal.items if meal else []
        photo_count = len(meal.photo_urls) if meal else 0
        photos = [f"{meal_type}_{i}.jpg" for i in range(1, photo_count + 1)]
        meal_records[meal_type] = {
            "label": MEAL_LABEL[meal_type],
            "items": items,
            "total_energy_kcal": meal.total_energy_kcal if meal else 0,
            "photos": photos,
        }

    return {
        "date": date_str,
        "updated_at": now.isoformat(),
        "total_energy_kcal": sum(
            m["total_energy_kcal"] for m in meal_records.values()
        ),
        "meals": meal_records,
        "summary": (
            {
                "comment": summary.comment,
                "nutrition": [asdict(row) for row in summary.nutrition],
            }
            if summary
            else None
        ),
    }


def save_record(data_dir: Path, date_str: str, record: dict) -> None:
    """`data_dir/<日付>/record.json` に record を原子的に書き込む。"""
    state_store.save_state(data_dir / date_str / "record.json", record)
