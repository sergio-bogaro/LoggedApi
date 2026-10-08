"""Provedor Infinite Backlog (jogos) — lê o CSV do export da coleção.

O layout não é documentado, então o parser identifica as colunas pelo cabeçalho
(com apelidos) e é tolerante a variações. A escala da nota é detectada pelo
maior valor do arquivo: ≤5 trata como 0–5 (×2), senão como 0–10.
"""

import csv
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
from services.importer.providers.games_common import is_backlog_status, map_game_status

_TITLE_KEYS = ["title", "name", "game", "game title"]
_RATING_KEYS = ["rating", "score", "my rating", "your rating"]
_STATUS_KEYS = ["status", "play status", "state", "shelf", "list"]
_REVIEW_KEYS = ["review", "notes", "note", "comment", "review text"]
_YEAR_KEYS = ["release year", "year", "release date", "released"]
_DATE_KEYS = [
    "date finished",
    "date completed",
    "date played",
    "completed",
    "finished",
    "date added",
    "added",
    "date",
]


def _first(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def _to_date(value: str) -> date | None:
    raw = (value or "").strip()
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


def _extract_year(value: str) -> int | None:
    match = re.search(r"\d{4}", value or "")
    return int(match.group()) if match else None


def _to_float(value: str) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class InfiniteBacklogProvider(ImportProvider):
    id = "infinitebacklog"
    label = "Infinite Backlog"
    media_types = [MediaTypeEnum.GAME]
    input_type = "file"
    accepts = ".csv"

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        if not raw:
            raise ValueError("O arquivo enviado está vazio.")

        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]
        if not rows:
            raise ValueError("CSV do Infinite Backlog vazio.")

        ratings = [_to_float(_first(row, _RATING_KEYS)) for row in rows]
        ratings = [rating for rating in ratings if rating is not None]
        scale = 10 if ratings and max(ratings) > 5 else 5

        entries = []
        for row in rows:
            entry = self._to_entry(row, scale)
            if entry is not None:
                entries.append(entry)

        if not entries:
            raise ValueError("Nenhum jogo reconhecido no CSV do Infinite Backlog.")

        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=sum(1 for entry in entries if entry.logs),
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(media_type=MediaTypeEnum.GAME, entries=entries, counts=counts)

    def _to_entry(self, row: dict[str, str], scale: int) -> ImportEntry | None:
        title = _first(row, _TITLE_KEYS)
        if not title:
            return None

        status_raw = _first(row, _STATUS_KEYS)
        status = map_game_status(status_raw)
        in_backlog = is_backlog_status(status_raw)
        if status is None and not in_backlog:
            # Sem status reconhecido: assume que foi jogado.
            status = MediaStatusEnum.FINISHED

        raw_rating = _to_float(_first(row, _RATING_KEYS))
        rating = None
        if raw_rating and raw_rating > 0:
            rating = round(raw_rating, 1) if scale == 10 else round(raw_rating * 2, 1)

        review = _first(row, _REVIEW_KEYS) or None
        year = _extract_year(_first(row, _YEAR_KEYS))
        day = _to_date(_first(row, _DATE_KEYS))

        logs: list[ImportLogEntry] = []
        if not in_backlog:
            when = day or date.today()
            end = _to_datetime(when) if status == MediaStatusEnum.FINISHED else None
            logs.append(
                ImportLogEntry(
                    date=when,
                    start_date=_to_datetime(when),
                    end_date=end,
                    status=status,
                    rating=rating,
                    review=review,
                )
            )

        return ImportEntry(
            key=f"infinitebacklog-{title}",
            media_type=MediaTypeEnum.GAME,
            title=title,
            year=year,
            source="infinitebacklog",
            external_refs={},
            status=status,
            rating=rating,
            review=review,
            logs=logs,
            in_backlog=in_backlog,
            backlog_date=day if in_backlog else None,
        )
