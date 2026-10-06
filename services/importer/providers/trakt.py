"""Provedor Trakt (filmes e séries) — lê o ZIP exportado em Settings → Data.

O parser é tolerante a variações de nome/estrutura: identifica os arquivos por
palavra-chave (`watched`/`ratings`/`watchlist` + `movie`/`show`) e aceita tanto
uma lista no topo quanto um objeto com a lista dentro. Os ids do TMDB/IMDb vêm
no próprio export, então o match é direto.
"""

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import (
    ImportEntry,
    ImportLogEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.importer.providers.base import ImportProvider


def _to_date(value: object) -> date | None:
    raw = str(value or "").strip()
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


def _to_rating(value: object) -> float | None:
    try:
        rating = float(value)  # Trakt usa 1–10
    except (TypeError, ValueError):
        return None
    return rating if rating > 0 else None


def _load_list(data: object) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("movies", "shows", "episodes", "seasons", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


@dataclass
class _Acc:
    title: str
    year: int | None = None
    tmdb: str | None = None
    imdb: str | None = None
    slug: str | None = None
    trakt: str | None = None
    watched_dates: list[date] = field(default_factory=list)
    rating: float | None = None
    watchlist_date: date | None = None


class TraktProvider(ImportProvider):
    id = "trakt"
    label = "Trakt"
    media_types = [MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES]
    input_type = "file"
    accepts = ".zip"

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        if not raw:
            raise ValueError("O arquivo enviado está vazio.")
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise ValueError(
                "Envie o ZIP exportado pelo Trakt (Settings → Data → Export now)."
            ) from exc

        movies: dict[str, _Acc] = {}
        shows: dict[str, _Acc] = {}
        with archive:
            for info in archive.namelist():
                base = info.rsplit("/", 1)[-1].lower()
                if not base.endswith(".json"):
                    continue
                if "movie" in base:
                    target, kind = movies, "movie"
                elif "show" in base:
                    target, kind = shows, "show"
                else:
                    continue

                if "watched" in base:
                    field_name = "watched"
                elif "rating" in base:
                    field_name = "rating"
                elif "watchlist" in base:
                    field_name = "watchlist"
                else:
                    continue

                try:
                    data = json.loads(archive.read(info).decode("utf-8-sig", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                for entry in _load_list(data):
                    if not isinstance(entry, dict):
                        continue
                    obj = entry.get(kind)
                    if isinstance(obj, dict):
                        self._accumulate(target, obj, entry, field_name)

        if not movies and not shows:
            raise ValueError("Nenhum filme ou série reconhecido no export do Trakt.")

        entries = self._finalize_all(movies, MediaTypeEnum.MOVIES) + self._finalize_all(
            shows, MediaTypeEnum.SERIES
        )
        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=sum(1 for entry in entries if entry.logs),
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(
            media_type=MediaTypeEnum.MOVIES if movies else MediaTypeEnum.SERIES,
            entries=entries,
            counts=counts,
        )

    def _accumulate(
        self, target: dict[str, _Acc], obj: dict, entry: dict, field_name: str
    ) -> None:
        ids = obj.get("ids") or {}
        tmdb = str(ids["tmdb"]) if ids.get("tmdb") else None
        imdb = ids.get("imdb") or None
        slug = ids.get("slug") or None
        trakt = str(ids["trakt"]) if ids.get("trakt") else None
        key = tmdb or imdb or slug or trakt
        if not key:
            return

        acc = target.get(str(key))
        if acc is None:
            acc = _Acc(
                title=obj.get("title") or "",
                year=obj.get("year"),
                tmdb=tmdb,
                imdb=imdb,
                slug=slug,
                trakt=trakt,
            )
            target[str(key)] = acc
        else:
            acc.slug = acc.slug or slug
            acc.trakt = acc.trakt or trakt

        if field_name == "watched":
            day = _to_date(entry.get("last_watched_at"))
            if day is not None and day not in acc.watched_dates:
                acc.watched_dates.append(day)
        elif field_name == "rating":
            rating = _to_rating(entry.get("rating"))
            if rating is not None:
                acc.rating = rating
        elif field_name == "watchlist":
            acc.watchlist_date = acc.watchlist_date or _to_date(entry.get("listed_at"))

    def _finalize_all(
        self, target: dict[str, _Acc], media_type: MediaTypeEnum
    ) -> list[ImportEntry]:
        return [self._finalize(acc, media_type) for acc in target.values()]

    def _finalize(self, acc: _Acc, media_type: MediaTypeEnum) -> ImportEntry:
        in_backlog = acc.watchlist_date is not None and not acc.watched_dates
        if in_backlog:
            status: MediaStatusEnum | None = None
        elif media_type == MediaTypeEnum.MOVIES:
            status = MediaStatusEnum.FINISHED
        else:
            # Trakt não diz se a série terminou; marca como "assistindo".
            status = MediaStatusEnum.IN_PROGRESS

        logs: list[ImportLogEntry] = []
        if acc.watched_dates:
            for day in sorted(set(acc.watched_dates)):
                if media_type == MediaTypeEnum.MOVIES:
                    start, end = _to_datetime(day), _to_datetime(day)
                else:
                    start, end = _to_datetime(day), None
                logs.append(
                    ImportLogEntry(
                        date=day,
                        start_date=start,
                        end_date=end,
                        status=status,
                        rating=acc.rating,
                        review=None,
                    )
                )

        refs: dict[str, str] = {}
        if acc.slug:
            refs["traktSlug"] = acc.slug
        if acc.trakt:
            refs["traktId"] = acc.trakt
        if acc.tmdb:
            refs["tmdbId"] = acc.tmdb
        if acc.imdb:
            refs["imdbId"] = acc.imdb

        return ImportEntry(
            key=f"trakt-{media_type.value}-{acc.tmdb or acc.imdb or acc.title}",
            media_type=media_type,
            title=acc.title,
            year=acc.year,
            source="trakt",
            external_refs=refs,
            status=status,
            rating=acc.rating,
            review=None,
            logs=logs,
            in_backlog=in_backlog,
            backlog_date=acc.watchlist_date,
        )
