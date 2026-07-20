"""Persist the state (content hash and message id) of posted Discord messages.

The state file lives on a mounted volume so that, across container restarts, the
process can tell whether to edit an existing message or post a new one.
"""
from __future__ import annotations

import json
from pathlib import Path


def load_state(path: Path) -> dict:
    """Load the state file, returning an empty dict if it is missing or corrupt."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict) -> None:
    """Write the state atomically via a temporary file and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_slot(state: dict, date_str: str, meal_type: str) -> dict | None:
    """Return the stored slot for a (date, meal), or None if not yet posted."""
    return state.get(date_str, {}).get(meal_type)


def set_slot(
    state: dict, date_str: str, meal_type: str, *, content_hash: str, message_id: str
) -> None:
    """Record the content hash and message id for a (date, meal) slot."""
    state.setdefault(date_str, {})[meal_type] = {
        "hash": content_hash,
        "message_id": message_id,
    }
