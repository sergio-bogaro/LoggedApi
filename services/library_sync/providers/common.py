"""Helpers compartilhados pelos adaptadores de biblioteca."""

import html
import re
from datetime import date, datetime, time, timezone
from typing import Any

import httpx

from config import settings

USER_AGENT = "Logged/1.0 (+https://github.com/sergio-bogaro/logged)"
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def client(timeout: float | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout or settings.library_sync_request_timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        day = to_date(raw)
        return datetime.combine(day, time.min) if day else None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def year_of(value: Any) -> int | None:
    if isinstance(value, (date, datetime)):
        return value.year
    raw = str(value or "")
    match = re.search(r"\d{4}", raw)
    return int(match.group()) if match else None


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def strip_html(value: str | None) -> str | None:
    if not value:
        return None
    text = _IMG_RE.sub("", value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def rating_0_10(value: Any, max_scale: float) -> float | None:
    rating = to_float(value)
    if rating is None or rating <= 0:
        return None
    return round(min(max(rating / max_scale * 10.0, 0.0), 10.0), 1)


def image_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("url", "large", "medium", "extraLarge", "original"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None
