"""Client for logging in to asken.jp and fetching meal records and daily summaries.

Uses only `requests`; no browser automation is involved.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from types import TracebackType

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
# Wraps only the "(?)" help-icon link next to the health score; carries no content.
SUMMARY_HELP_ICON_RE = re.compile(r'<span class="small">.*?</span>', re.DOTALL)
# asken separates ranked menu items with runs of 2+ ideographic spaces; break them
# onto their own lines instead of leaving them run together.
SUMMARY_LIST_ITEM_RE = re.compile("　{2,}")
# Rows of the day's nutrition table (the "pgraph_eiyo1" block), one per nutrient:
# name, over/fit/short status, actual intake, and the target range.
NUTRITION_ROW_RE = re.compile(
    r'<li class="title">([^<]+)</li>\s*'
    r'<li class="status ([a-z]+)">[^<]*</li>\s*'
    r'<li class="val fn10">([^<]+)</li>\s*'
    r'<li class="center fn10">([^<]+)</li>'
)

_BR_MARKER = "\x00"
_LIST_BREAK_MARKER = "\x01"


def _clean_summary_comment(body_html: str) -> str:
    """Turn the summary comment's inner HTML into readable plain text.

    `<br>` and ranked-item separators are swapped for sentinel characters
    before whitespace collapsing, so the source markup's incidental indentation
    newlines are discarded while intentional line breaks survive.
    """
    text = re.sub(r"<!--.*?-->", "", body_html, flags=re.DOTALL)
    text = SUMMARY_HELP_ICON_RE.sub("", text)
    text = re.sub(r"<br\s*/?>", _BR_MARKER, text)
    text = SUMMARY_LIST_ITEM_RE.sub(_LIST_BREAK_MARKER, text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace(_BR_MARKER, "\n").replace(_LIST_BREAK_MARKER, "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # The health-score span's indentation collapses into a stray space before "は".
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
    """A single meal's menu items and photo URLs for one date and meal type."""

    meal_type: str
    meal_label: str
    items: list[dict] = field(default_factory=list)
    photo_urls: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """Whether the meal has any recorded menu items or photos."""
        return bool(self.items) or bool(self.photo_urls)

    @property
    def total_energy_kcal(self) -> int:
        """Sum of the menu items' energy in kcal, ignoring unparseable values."""
        total = 0
        for item in self.items:
            try:
                total += int(item.get("energy_kcal") or 0)
            except (ValueError, TypeError):
                pass
        return total


@dataclass(frozen=True)
class NutritionRow:
    """One nutrient's daily intake, as computed by asken."""

    name: str
    status: str  # "over" | "fit" | "short"
    value: str
    target_range: str


@dataclass(frozen=True)
class DailySummary:
    """A day's nutrition summary parsed from asken.

    The comment's heading is a generic per-user greeting with no real
    information ("<name>さんのアドバイスをお伝えしますね。"), so only the body
    is kept as `comment`.
    """

    comment: str
    nutrition: list[NutritionRow]


def _looks_logged_in(body_html: str) -> bool:
    """Whether a response body shows a "ログアウト"/"logout" link.

    asken.jp renders this link only for an authenticated session, so its
    presence distinguishes "already logged in" from a genuine login failure.
    """
    return "ログアウト" in body_html or "logout" in body_html.lower()


class LoginError(RuntimeError):
    """Raised when authentication to asken.jp does not succeed."""


class AskenClient:
    """Session-backed client for asken.jp.

    Intended to be reused across cycles: a single session keeps connections
    alive, and `login` is called again each cycle to refresh authentication.
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
        """Close the underlying HTTP session."""
        self.session.close()

    def login(self) -> None:
        """Authenticate against asken.jp, raising LoginError on failure.

        A no-op if the session's existing cookies are already authenticated:
        asken.jp then redirects `GET /login` to the top page instead of
        returning the login form, so `_csrfToken` is absent for a reason
        other than failure.
        """
        r = self.session.get(f"{BASE_URL}/login", timeout=30)
        r.raise_for_status()
        m = CSRF_RE.search(r.text)
        if not m:
            if _looks_logged_in(r.text):
                return
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
        if not _looks_logged_in(r2.text):
            raise LoginError("Login failed (no logout link found in response)")

    def fetch_meal(self, meal_type: str, date: str) -> MealRecord:
        """Fetch the menu items and photos for one meal type on one date."""
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
        """Fetch the daily nutrition summary for a date, or None if there is none.

        The date must be passed as a path segment (`/wsp/advice/{date}`); the
        endpoint silently ignores a `diary_date` query parameter and always
        returns today's data instead.
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
        """Download a meal photo and return its raw bytes."""
        r = self.session.get(f"{BASE_URL}{url_path}", timeout=30)
        r.raise_for_status()
        return r.content
