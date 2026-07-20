"""Build the Discord message body for a single (date, meal) record.

Messages are plain text plus image attachments; embeds are deliberately not used.
Only real data (menu, calories, photos, advice) is rendered, so self-evident
fields such as a meal-type-only description are omitted.
"""
from __future__ import annotations

import datetime as dt

from asken_client import Advice, MealRecord


def _format_title(date_str: str, meal_label: str) -> str:
    """Format the message heading, e.g. "07/19 朝食"."""
    d = dt.date.fromisoformat(date_str)
    return f"{d.month:02d}/{d.day:02d} {meal_label}"


def _format_items_lines(meal: MealRecord) -> list[str]:
    """Format each menu item as a "- name xN kcal" line."""
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
    advice: Advice | None,
    photo_files: list[tuple[str, bytes]],
) -> tuple[str, list[tuple[str, bytes, str]]]:
    """Build the Discord webhook (content, files) for one (date, meal) record.

    files is [(filename, content_bytes, content_type), ...], attached as plain
    image attachments.
    """
    parts = [_format_title(date_str, meal.meal_label)]
    parts.extend(_format_items_lines(meal))

    if meal.items:
        parts.append("")
        parts.append(f"計{meal.total_energy_kcal}kcal")

    if advice:
        parts.append("")
        parts.append(advice.title)
        parts.append(advice.body)

    content = "\n".join(parts)

    files: list[tuple[str, bytes, str]] = [
        (name, data, "image/jpeg") for name, data in photo_files
    ]

    return content, files
