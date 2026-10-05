"""Provedor Goodreads (livros) — lê o `library_export.csv`.

O CSV traz ISBN/ISBN13, nota (1–5), a estante (`Exclusive Shelf`), datas e a
resenha. O ISBN é resolvido para a obra da OpenLibrary no matcher. A nota é
convertida para a escala 0–10.
"""

import csv
import html
import io
import re
from datetime import date, datetime, time

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import (
    ImportEntry,
    ImportLogEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.importer.providers.base import ImportProvider

# `Exclusive Shelf` → status do Logged (None = backlog).
_SHELF_MAP: dict[str, MediaStatusEnum | None] = {
    "read": MediaStatusEnum.FINISHED,
    "currently-reading": MediaStatusEnum.IN_PROGRESS,
    "to-read": None,
    "did-not-finish": MediaStatusEnum.DROPPED,
}


def _clean_isbn(value: str | None) -> str | None:
    raw = (value or "").strip().replace("=", "").replace('"', "").strip()
    return raw or None


def _to_rating(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        rating = float(raw)
    except ValueError:
        return None
    return round(rating * 2, 1) if rating > 0 else None


def _to_date(value: str | None) -> date | None:
    raw = (value or "").strip().replace("/", "-")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _to_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min)


def _clean_review(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).strip()
    return text or None


class GoodreadsProvider(ImportProvider):
    id = "goodreads"
    label = "Goodreads"
    media_types = [MediaTypeEnum.BOOK]
    input_type = "file"
    accepts = ".csv"

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        if not raw:
            raise ValueError("O arquivo enviado está vazio.")

        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {(key or "").strip(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]
        if not rows or "Title" not in (rows[0].keys() if rows else []):
            raise ValueError(
                "CSV do Goodreads não reconhecido. Exporte em My Books → Import and "
                "export → Export Library."
            )

        entries = [self._to_entry(row) for row in rows]
        entries = [entry for entry in entries if entry.title]
        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=sum(1 for entry in entries if entry.logs),
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(media_type=MediaTypeEnum.BOOK, entries=entries, counts=counts)

    def _to_entry(self, row: dict[str, str]) -> ImportEntry:
        title = row.get("Title", "")
        author = row.get("Author", "")
        isbn13 = _clean_isbn(row.get("ISBN13"))
        isbn10 = _clean_isbn(row.get("ISBN"))

        rating = _to_rating(row.get("My Rating"))
        review = _clean_review(row.get("My Review"))

        shelf = (row.get("Exclusive Shelf") or "").strip().lower()
        status = _SHELF_MAP.get(shelf, MediaStatusEnum.FINISHED)
        in_backlog = shelf == "to-read"

        date_read = _to_date(row.get("Date Read"))
        date_added = _to_date(row.get("Date Added"))

        logs: list[ImportLogEntry] = []
        if not in_backlog:
            day = date_read or date_added or date.today()
            logs.append(
                ImportLogEntry(
                    date=day,
                    start_date=_to_datetime(date_added),
                    end_date=_to_datetime(date_read),
                    status=status,
                    rating=rating,
                    review=review,
                )
            )

        refs: dict[str, str] = {}
        if isbn13:
            refs["isbn13"] = isbn13
        elif isbn10:
            refs["isbn10"] = isbn10

        book_id = row.get("Book Id") or isbn13 or isbn10 or title

        return ImportEntry(
            key=f"goodreads-{book_id}",
            media_type=MediaTypeEnum.BOOK,
            title=title,
            year=None,
            overview=f"Author: {author}" if author else None,
            source="goodreads",
            external_refs=refs,
            status=status,
            rating=rating,
            review=review,
            logs=logs,
            in_backlog=in_backlog,
            backlog_date=date_added if in_backlog else None,
        )
