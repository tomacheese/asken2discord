"""Client for logging in to asken.jp and fetching meal records and advice.

Uses only `requests`; no browser automation is involved.
"""
from __future__ import annotations

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
ADVICE_TITLE_RE = re.compile(
    r'<div id="premium_fuki_comment">\s*<h4>\s*(.*?)\s*</h4>(.*?)</div>', re.DOTALL
)


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
class Advice:
    """A daily advice comment parsed from asken."""

    title: str
    body: str


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
        """Authenticate against asken.jp, raising LoginError on failure."""
        r = self.session.get(f"{BASE_URL}/login", timeout=30)
        r.raise_for_status()
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

    def fetch_advice(self, date: str) -> Advice | None:
        """Fetch the daily advice for a date, or None if there is none."""
        r = self.session.get(
            f"{BASE_URL}/wsp/advice", params={"diary_date": date}, timeout=30
        )
        r.raise_for_status()
        m = ADVICE_TITLE_RE.search(r.text)
        if not m:
            return None
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        body_html = m.group(2)
        body_text = re.sub(r"<br\s*/?>", "\n", body_html)
        body_text = re.sub(r"<[^>]+>", "", body_text)
        body_text = "\n".join(
            line.strip() for line in body_text.splitlines() if line.strip()
        )
        return Advice(title=title, body=body_text)

    def download_photo(self, url_path: str) -> bytes:
        """Download a meal photo and return its raw bytes."""
        r = self.session.get(f"{BASE_URL}{url_path}", timeout=30)
        r.raise_for_status()
        return r.content
