"""投稿済み Discord メッセージの状態 (内容ハッシュとメッセージ ID) を永続化する。

状態ファイルはマウントしたボリューム上に置く。これにより、コンテナを再起動しても
既存メッセージを編集するか新規投稿するかを判断できる。
"""
from __future__ import annotations

import json
from pathlib import Path


def load_state(path: Path) -> dict:
    """状態ファイルを読み込む。ファイルがない、または壊れている場合は空の dict を返す。"""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict) -> None:
    """一時ファイルに書いてからリネームすることで、状態を原子的に書き込む。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_slot(state: dict, date_str: str, meal_type: str) -> dict | None:
    """(日付, 食事区分) の保存済みスロットを返す。未投稿なら None。"""
    return state.get(date_str, {}).get(meal_type)


def set_slot(
    state: dict, date_str: str, meal_type: str, *, content_hash: str, message_id: str
) -> None:
    """(日付, 食事区分) のスロットに内容ハッシュとメッセージ ID を記録する。"""
    state.setdefault(date_str, {})[meal_type] = {
        "hash": content_hash,
        "message_id": message_id,
    }
