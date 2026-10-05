"""Provedor IMDb (filmes e séries) — lê o `ratings.csv` (ou `watchlist.csv`).

O IMDb traz o próprio id (`Const`, ex.: `tt0468569`), resolvido para o TMDB via
`/find?external_source=imdb_id` no matcher. As notas usam a escala 1–10.
"""

import csv
import io
from datetime import date, datetime, time

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import (
    ImportEntry,
    ImportLogEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.importer.providers.base import ImportProvider

_SERIES_TYPES = {"tvseries", "tvminiseries"}
_MOVIE_TYPES = {"movie", "tvmovie", "short", "tvshort", "tvspecial", "video"}


def _to_date(value: str | None) -> date | None:
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


def _to_rating(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        rating = float(raw)  # IMDb usa 1–10
    except ValueError:
        return None
    return rating if rating > 0 else None


def _to_int(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


class ImdbProvider(ImportProvider):
    id = "imdb"
    label = "IMDb"
    media_types = [MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES]
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
            raise ValueError("CSV do IMDb vazio.")

        headers = set(rows[0].keys())
        if "const" not in headers:
            raise ValueError(
                "CSV do IMDb não reconhecido. Exporte em Your Ratings → Export "
                "(ratings.csv ou watchlist.csv)."
            )

        is_ratings = "your rating" in headers
        is_watchlist = "created" in headers
        if not is_ratings and not is_watchlist:
            raise ValueError(
                "CSV do IMDb sem nota nem data de watchlist. Envie o ratings.csv "
                "(ou o watchlist.csv)."
            )

        entries = [self._to_entry(row, is_ratings, is_watchlist) for row in rows]
        entries = [entry for entry in entries if entry is not None]

        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=sum(1 for entry in entries if entry.logs),
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        has_movies = any(entry.media_type == MediaTypeEnum.MOVIES for entry in entries)
        return ImportPreviewResponse(
            media_type=MediaTypeEnum.MOVIES if has_movies else MediaTypeEnum.SERIES,
            entries=entries,
            counts=counts,
        )

    def _to_entry(self, row: dict[str, str], is_ratings: bool, is_watchlist: bool) -> ImportEntry | None:
        imdb_id = row.get("const", "")
        if not imdb_id:
            return None

        media_type = self._media_type(row.get("title type"))
        if media_type is None:
            return None
        title = row.get("title") or ""
        year = _to_int(row.get("year"))
        rating = _to_rating(row.get("your rating")) if is_ratings else None

        in_backlog = is_watchlist and not is_ratings
        day = _to_date(row.get("date rated")) or _to_date(row.get("created")) or date.today()

        logs: list[ImportLogEntry] = []
        if not in_backlog:
            logs.append(
                ImportLogEntry(
                    date=day,
                    start_date=_to_datetime(day),
                    end_date=_to_datetime(day),
                    status=MediaStatusEnum.FINISHED,
                    rating=rating,
                    review=None,
                )
            )

        return ImportEntry(
            key=f"imdb-{imdb_id}",
            media_type=media_type,
            title=title,
            year=year,
            source="imdb",
            external_refs={"imdbId": imdb_id},
            status=None if in_backlog else MediaStatusEnum.FINISHED,
            rating=rating,
            review=None,
            logs=logs,
            in_backlog=in_backlog,
            backlog_date=day if in_backlog else None,
        )

    @staticmethod
    def _media_type(title_type: str | None) -> MediaTypeEnum | None:
        value = (title_type or "").strip().lower()
        if value in _SERIES_TYPES:
            return MediaTypeEnum.SERIES
        if value in _MOVIE_TYPES:
            return MediaTypeEnum.MOVIES
        # Episódios, jogos e outros tipos são ignorados.
        return None
