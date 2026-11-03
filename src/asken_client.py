"""asken.jp にログインし、食事記録と日次サマリーを取得するクライアント。

`requests` だけを使い、ブラウザ自動化は使わない。
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from types import TracebackType
from urllib.parse import urlparse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BASE_URL = "https://www.asken.jp"
MEAL_TYPES = ["breakfast", "lunch", "dinner", "sweets"]
MEAL_LABEL = {
    "breakfast": "朝食",
    "lunch": "昼食",
    "dinner": "夕食",
    "sweets": "間食",
}

EAT_DATAS_RE = re.compile(r"V2WspMeal\.eatDatas\s*=\s*(\{.*?\});", re.DOTALL)
CSRF_RE = re.compile(r'name="_csrfToken"\s+value="([^"]+)"')
PHOTO_PATH_RE = re.compile(r"/meal_photo/my_photo/[^\"'\s]+")
SUMMARY_COMMENT_RE = re.compile(
    r'<div id="fuki">\s*<h4>.*?</h4>(.*?)</div>', re.DOTALL
)
# 健康度の横にある「(?)」ヘルプアイコンのリンクだけを囲む。内容は持たない。
SUMMARY_HELP_ICON_RE = re.compile(r'<span class="small">.*?</span>', re.DOTALL)
# あすけんは順位付きメニューを 2 個以上連続する全角スペースで区切っている。
# つなげたままにせず、それぞれ別の行に分ける。
SUMMARY_LIST_ITEM_RE = re.compile("　{2,}")
# 1 日の栄養テーブル ("pgraph_eiyo1" ブロック) の行。栄養素ごとに 1 行で、
# 名前・過不足の状態 (over/fit/short)・摂取量・目安範囲を持つ。
NUTRITION_ROW_RE = re.compile(
    r'<li class="title">([^<]+)</li>\s*'
    r'<li class="status ([a-z]+)">[^<]*</li>\s*'
    r'<li class="val fn10">([^<]+)</li>\s*'
    r'<li class="center fn10">([^<]+)</li>'
)

_BR_MARKER = "\x00"
_LIST_BREAK_MARKER = "\x01"


def _clean_summary_comment(body_html: str) -> str:
    """サマリーコメントの内側の HTML を、読みやすいプレーンテキストに変換する。

    `<br>` と順位付き項目の区切りは、空白を畳む前にセンチネル文字へ置き換える。
    これにより、元マークアップのインデント由来の改行は捨て、意図した改行だけを残せる。
    """
    text = re.sub(r"<!--.*?-->", "", body_html, flags=re.DOTALL)
    text = SUMMARY_HELP_ICON_RE.sub("", text)
    text = re.sub(r"<br\s*/?>", _BR_MARKER, text)
    text = SUMMARY_LIST_ITEM_RE.sub(_LIST_BREAK_MARKER, text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace(_BR_MARKER, "\n").replace(_LIST_BREAK_MARKER, "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # 健康度の span のインデントが畳まれ、「は」の前に余分な空白が残る。
    text = re.sub(r"健康度\s+は", "健康度は", text)

    lines = [line.strip() for line in text.splitlines()]
    cleaned: list[str] = []
    prev_blank = True
    for line in lines:
        if line:
            cleaned.append(line)
            prev_blank = False
        elif not prev_blank:
            cleaned.append("")
            prev_blank = True
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\n".join(cleaned)


@dataclass
class MealRecord:
    """1 つの日付・食事区分における、メニュー項目と写真 URL。"""

    meal_type: str
    meal_label: str
    items: list[dict] = field(default_factory=list)
    photo_urls: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """メニュー項目か写真が 1 つでも記録されているか。"""
        return bool(self.items) or bool(self.photo_urls)

    @property
    def total_energy_kcal(self) -> int:
        """メニュー項目のエネルギー (kcal) の合計。数値として解釈できない値は無視する。"""
        total = 0
        for item in self.items:
            try:
                total += int(item.get("energy_kcal") or 0)
            except (ValueError, TypeError):
                pass
        return total


@dataclass(frozen=True)
class NutritionRow:
    """あすけんが算出した、1 栄養素の 1 日の摂取状況。"""

    name: str
    status: str  # "over" | "fit" | "short"
    value: str
    target_range: str


@dataclass(frozen=True)
class DailySummary:
    """あすけんから解析した 1 日分の栄養サマリー。

    コメントの見出しはユーザー名入りの定型挨拶 ("<name>さんのアドバイスをお伝えしますね。")
    で情報を持たないため、本文だけを `comment` として保持する。
    """

    comment: str
    nutrition: list[NutritionRow]


class LoginError(RuntimeError):
    """asken.jp への認証に失敗したときに送出する。"""


class AskenClient:
    """asken.jp 向けの、セッション付きクライアント。

    サイクルをまたいで使い回す前提。1 つのセッションで接続を維持し、
    認証を更新するため `login` を毎サイクル呼び直す。
    """

    def __init__(self, email: str, password: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.email = email
        self.password = password

    def __enter__(self) -> AskenClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """内部の HTTP セッションを閉じる。"""
        self.session.close()

    def login(self) -> None:
        """asken.jp に認証し、失敗したら LoginError を送出する。

        セッションの既存 Cookie がすでに認証済みなら何もしない。その場合 asken.jp は
        `GET /login` に対してログインフォームを返さず、トップページへリダイレクトする。
        ただし、どちらか一方のシグナルだけ (`/login` からの無関係なリダイレクト、
        またはページ内の偶然の "logout" 一致) では本当の失敗を誤判定しうるため、
        リダイレクトと、遷移先ページの「ログアウト」リンクの目印の両方が揃って
        初めて認証済みとみなす。
        """
        r = self.session.get(f"{BASE_URL}/login", timeout=30)
        r.raise_for_status()
        redirected_away_from_login = (
            r.history and urlparse(r.url).path.rstrip("/") != "/login"
        )
        already_logged_in = redirected_away_from_login and (
            "ログアウト" in r.text or "logout" in r.text.lower()
        )
        if already_logged_in:
            return
        m = CSRF_RE.search(r.text)
        if not m:
            raise LoginError("CSRF token not found on login page")
        csrf = m.group(1)

        r2 = self.session.post(
            f"{BASE_URL}/login",
            data={
                "_csrfToken": csrf,
                "CustomerMember[email]": self.email,
                "CustomerMember[passwd_plain]": self.password,
                "CustomerMember[autologin]": "0",
            },
            timeout=30,
        )
        r2.raise_for_status()
        if "ログアウト" not in r2.text and "logout" not in r2.text.lower():
            raise LoginError("Login failed (no logout link found in response)")

    def fetch_meal(self, meal_type: str, date: str) -> MealRecord:
        """1 つの日付・食事区分のメニュー項目と写真を取得する。"""
        r = self.session.get(f"{BASE_URL}/wsp/meal/{meal_type}/{date}", timeout=30)
        r.raise_for_status()

        items = []
        m = EAT_DATAS_RE.search(r.text)
        if m:
            eat_datas = json.loads(m.group(1))
            for item_hash, item in eat_datas.items():
                items.append(
                    {
                        "item_hash": item_hash,
                        "menu_id": item.get("menu_id"),
                        "menu_name": item.get("menu_name"),
                        "quantity": item.get("menu_quantity"),
                        "energy_kcal": item.get("energy"),
                    }
                )

        photo_paths = sorted(set(PHOTO_PATH_RE.findall(r.text)))
        photo_hashes: dict[str, str] = {}
        for p in photo_paths:
            parts = p.rstrip("/").split("/")
            if len(parts) >= 5:
                photo_hash = parts[-2]
                photo_hashes[photo_hash] = "/".join(parts[:-1]) + "/fit_380x380"

        return MealRecord(
            meal_type=meal_type,
            meal_label=MEAL_LABEL[meal_type],
            items=items,
            photo_urls=list(photo_hashes.values()),
        )

    def fetch_summary(self, date: str) -> DailySummary | None:
        """日付の栄養サマリーを取得する。なければ None を返す。

        日付はパスセグメント (`/wsp/advice/{date}`) で渡す必要がある。このエンドポイントは
        クエリパラメータ `diary_date` を黙って無視し、常に当日のデータを返すため。
        """
        r = self.session.get(f"{BASE_URL}/wsp/advice/{date}", timeout=30)
        r.raise_for_status()
        m = SUMMARY_COMMENT_RE.search(r.text)
        if not m:
            return None
        nutrition = [
            NutritionRow(name=name, status=status, value=value, target_range=target_range)
            for name, status, value, target_range in NUTRITION_ROW_RE.findall(r.text)
        ]
        return DailySummary(comment=_clean_summary_comment(m.group(1)), nutrition=nutrition)

    def download_photo(self, url_path: str) -> bytes:
        """食事の写真をダウンロードし、生のバイト列を返す。"""
        r = self.session.get(f"{BASE_URL}{url_path}", timeout=30)
        r.raise_for_status()
        return r.content
