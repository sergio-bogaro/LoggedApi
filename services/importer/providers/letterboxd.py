"""Provedor Letterboxd (filmes) — ZIP/CSV do export de dados.

O export não traz o id do TMDB, então cada filme é casado por título + ano no
matcher. As notas (0,5–5) são convertidas para a escala 0–10 do Logged.
"""

import csv
import io
import re
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

_KNOWN_FILES = {"watched.csv", "diary.csv", "ratings.csv", "reviews.csv", "watchlist.csv"}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _slug_from_uri(uri: str | None) -> str:
    parts = [part for part in (uri or "").split("/") if part]
    if "film" in parts:
        index = parts.index("film")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1] if parts else ""


def _to_int(value: str | None) -> int | None:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return None


def _to_date(value: str | None) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _to_rating(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return round(float(raw) * 2, 2)
    except ValueError:
        return None


def _to_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min)


@dataclass
class _Watch:
    rating: float | None = None
    review: str | None = None


@dataclass
class _FilmAccumulator:
    key: str
    name: str
    year: int | None = None
    uri: str | None = None
    watches: dict[date, _Watch] = field(default_factory=dict)
    watched_file_dates: list[date] = field(default_factory=list)
    ratings_csv: float | None = None
    rating_date: date | None = None
    in_watchlist: bool = False
    watchlist_date: date | None = None


class LetterboxdProvider(ImportProvider):
    id = "letterboxd"
    label = "Letterboxd"
    media_types = [MediaTypeEnum.MOVIES]
    input_type = "file"
    accepts = ".zip,.csv"

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        if not raw:
            raise ValueError("O arquivo enviado está vazio.")

        files = self._extract_files(filename, raw)
        if not files:
            raise ValueError(
                "Nenhum arquivo reconhecido do Letterboxd foi encontrado. "
                "Envie o ZIP exportado pelo Letterboxd (watched.csv, diary.csv, "
                "ratings.csv, reviews.csv, watchlist.csv)."
            )

        accumulators: dict[str, _FilmAccumulator] = {}
        if "watched.csv" in files:
            self._parse_watched(files["watched.csv"], accumulators)
        if "diary.csv" in files:
            self._parse_diary(files["diary.csv"], accumulators)
        if "reviews.csv" in files:
            self._parse_reviews(files["reviews.csv"], accumulators)
        if "ratings.csv" in files:
            self._parse_ratings(files["ratings.csv"], accumulators)
        if "watchlist.csv" in files:
            self._parse_watchlist(files["watchlist.csv"], accumulators)

        entries = [self._finalize(acc) for acc in accumulators.values()]
        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=sum(1 for entry in entries if entry.logs),
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(
            media_type=MediaTypeEnum.MOVIES, entries=entries, counts=counts
        )

    # ── leitura dos CSVs ──

    def _extract_files(self, filename: str, raw: bytes) -> dict[str, bytes]:
        name = (filename or "").rsplit("/", 1)[-1].lower()
        if name.endswith(".zip") or raw[:2] == b"PK":
            result: dict[str, bytes] = {}
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for info in archive.namelist():
                    base = info.rsplit("/", 1)[-1].lower()
                    if base in _KNOWN_FILES:
                        result[base] = archive.read(info)
            return result

        base = name or "watched.csv"
        if base in _KNOWN_FILES:
            return {base: raw}
        return {self._infer_single_file(raw): raw}

    def _infer_single_file(self, raw: bytes) -> str:
        lines = raw.decode("utf-8-sig", errors="replace").splitlines()
        header = lines[0].lower() if lines else ""
        if "watched date" in header:
            return "reviews.csv" if "review" in header else "diary.csv"
        if "rating" in header:
            return "ratings.csv"
        return "watched.csv"

    def _read_rows(self, raw: bytes) -> list[dict[str, str]]:
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        return [
            {(key or "").strip(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]

    def _ensure(
        self,
        accumulators: dict[str, _FilmAccumulator],
        name: str,
        year: int | None,
        uri: str | None,
    ) -> _FilmAccumulator | None:
        if not name:
            return None
        slug = _slug_from_uri(uri)
        base = slug or _normalize(name).replace(" ", "-")
        key = f"{base}|{year or ''}"
        acc = accumulators.get(key)
        if acc is None:
            acc = _FilmAccumulator(key=key, name=name, year=year, uri=uri or None)
            accumulators[key] = acc
        elif not acc.uri and uri:
            acc.uri = uri
        return acc

    @staticmethod
    def _watch(acc: _FilmAccumulator, day: date | None) -> _Watch | None:
        if day is None:
            return None
        if day not in acc.watches:
            acc.watches[day] = _Watch()
        return acc.watches[day]

    def _parse_watched(self, raw: bytes, accumulators: dict[str, _FilmAccumulator]) -> None:
        for row in self._read_rows(raw):
            acc = self._ensure(
                accumulators, row.get("Name", ""), _to_int(row.get("Year")), row.get("Letterboxd URI")
            )
            day = _to_date(row.get("Date"))
            if acc is not None and day is not None and day not in acc.watched_file_dates:
                acc.watched_file_dates.append(day)

    def _parse_diary(self, raw: bytes, accumulators: dict[str, _FilmAccumulator]) -> None:
        for row in self._read_rows(raw):
            acc = self._ensure(
                accumulators, row.get("Name", ""), _to_int(row.get("Year")), row.get("Letterboxd URI")
            )
            if acc is None:
                continue
            day = _to_date(row.get("Watched Date")) or _to_date(row.get("Date"))
            watch = self._watch(acc, day)
            rating = _to_rating(row.get("Rating"))
            if watch is not None and rating is not None:
                watch.rating = rating

    def _parse_reviews(self, raw: bytes, accumulators: dict[str, _FilmAccumulator]) -> None:
        for row in self._read_rows(raw):
            acc = self._ensure(
                accumulators, row.get("Name", ""), _to_int(row.get("Year")), row.get("Letterboxd URI")
            )
            if acc is None:
                continue
            day = _to_date(row.get("Watched Date")) or _to_date(row.get("Date"))
            watch = self._watch(acc, day)
            rating = _to_rating(row.get("Rating"))
            review = (row.get("Review") or "").strip() or None
            if watch is not None:
                if rating is not None:
                    watch.rating = rating
                if review:
                    watch.review = review

    def _parse_ratings(self, raw: bytes, accumulators: dict[str, _FilmAccumulator]) -> None:
        for row in self._read_rows(raw):
            acc = self._ensure(
                accumulators, row.get("Name", ""), _to_int(row.get("Year")), row.get("Letterboxd URI")
            )
            if acc is None:
                continue
            acc.ratings_csv = _to_rating(row.get("Rating"))
            acc.rating_date = _to_date(row.get("Date"))

    def _parse_watchlist(self, raw: bytes, accumulators: dict[str, _FilmAccumulator]) -> None:
        for row in self._read_rows(raw):
            acc = self._ensure(
                accumulators, row.get("Name", ""), _to_int(row.get("Year")), row.get("Letterboxd URI")
            )
            if acc is None:
                continue
            acc.in_watchlist = True
            acc.watchlist_date = _to_date(row.get("Date")) or acc.watchlist_date

    def _finalize(self, acc: _FilmAccumulator) -> ImportEntry:
        if acc.watches:
            log_dates = sorted(acc.watches)
        elif acc.watched_file_dates:
            log_dates = sorted(set(acc.watched_file_dates))
        elif acc.ratings_csv is not None and acc.rating_date is not None:
            log_dates = [acc.rating_date]
        else:
            log_dates = []

        logs: list[ImportLogEntry] = []
        for day in log_dates:
            watch = acc.watches.get(day)
            logs.append(
                ImportLogEntry(
                    date=day,
                    start_date=_to_datetime(day),
                    end_date=_to_datetime(day),
                    status=MediaStatusEnum.FINISHED,
                    rating=watch.rating if watch else None,
                    review=watch.review if watch else None,
                )
            )

        rating = acc.ratings_csv
        if rating is None:
            ratings = [watch.rating for _, watch in sorted(acc.watches.items()) if watch.rating is not None]
            rating = ratings[-1] if ratings else None

        reviews = [(day, watch.review) for day, watch in sorted(acc.watches.items()) if watch.review]
        review = reviews[-1][1] if reviews else None

        if len(logs) == 1:
            if logs[0].rating is None:
                logs[0].rating = rating
            if logs[0].review is None:
                logs[0].review = review
        elif logs and logs[-1].rating is None and rating is not None:
            logs[-1].rating = rating

        return ImportEntry(
            key=acc.key,
            media_type=MediaTypeEnum.MOVIES,
            title=acc.name,
            year=acc.year,
            source="letterboxd",
            external_refs={"letterboxdUri": acc.uri} if acc.uri else {},
            status=MediaStatusEnum.FINISHED if logs else None,
            rating=rating,
            review=review,
            logs=logs,
            in_backlog=acc.in_watchlist,
            backlog_date=acc.watchlist_date,
        )
