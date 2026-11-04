"""(日付, 食事区分) の記録と日次サマリーから Discord メッセージ本文を組み立てる。

食事メッセージはプレーンテキストと画像添付で構成する。
メニュー・カロリー・写真といった実データだけを出力し、食事区分だけの説明のような自明な項目は省く。
あすけんの日次サマリーは 1 食ではなく 1 日全体を対象とするため、
食事メッセージに添付せず、独立した embed メッセージとして出力する。
"""
from __future__ import annotations

import datetime as dt

from asken_client import DailySummary, MealRecord

_STATUS_EMOJI = {"over": "🔺", "fit": "✅", "short": "🔻"}


def _format_title(date_str: str, meal_label: str) -> str:
    """メッセージの見出しを整形する (例: "07/19 朝食")。"""
    d = dt.date.fromisoformat(date_str)
    return f"{d.month:02d}/{d.day:02d} {meal_label}"


def _format_items_lines(meal: MealRecord) -> list[str]:
    """各メニューを "- 名前 xN kcal" 形式の行に整形する。"""
    if not meal.items:
        return ["(写真のみ記録)"]
    lines = []
    for item in meal.items:
        name = item.get("menu_name") or "?"
        qty = item.get("quantity") or "1"
        kcal = item.get("energy_kcal")
        kcal_part = f" {kcal}kcal" if kcal is not None else ""
        lines.append(f"- {name} x{qty}{kcal_part}")
    return lines


def build_meal_message(
    *,
    date_str: str,
    meal: MealRecord,
    photo_files: list[tuple[str, bytes]],
) -> tuple[str, list[tuple[str, bytes, str]]]:
    """1 件の (日付, 食事区分) 記録から Discord Webhook 用の (content, files) を組み立てる。

    files は [(ファイル名, バイト列, content_type), ...] で、単なる画像添付として付ける。
    """
    parts = [_format_title(date_str, meal.meal_label)]
    parts.extend(_format_items_lines(meal))

    if meal.items:
        parts.append("")
        parts.append(f"計{meal.total_energy_kcal}kcal")

    content = "\n".join(parts)

    files: list[tuple[str, bytes, str]] = [
        (name, data, "image/jpeg") for name, data in photo_files
    ]

    return content, files


def build_summary_embed(*, date_str: str, summary: DailySummary) -> dict:
    """1 日分の栄養サマリーの Discord embed を組み立てる。

    プレーンテキストではなく embed を使うと、コメントと栄養素の数値が視覚的に
    区切られたコンテナに入るため、読みやすさのためにコードブロック整形を足す必要がない。
    """
    fields = [
        {
            "name": f"{row.name} {_STATUS_EMOJI.get(row.status, '')}".strip(),
            "value": f"{row.value}(目安 {row.target_range})",
            "inline": True,
        }
        for row in summary.nutrition
    ]
    return {
        "title": _format_title(date_str, "サマリー"),
        "description": summary.comment,
        "fields": fields,
    }
