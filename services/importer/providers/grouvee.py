"""Provedor Grouvee (jogos) — lê o CSV oficial do export.

Export em Grouvee → Settings → "Export your collection to a CSV file" (o link
chega por e-mail). Colunas (posicionais, confirmadas no parser da comunidade):

`grouvee_id, name, shelves(JSON), platforms, rating, review, dates, statuses,
genres, franchises, developers, publishers, release_date, url, giantbomb_id`

A nota é 1–5 → ×2 (0–10). O match é feito no IGDB por título + ano.
"""

import csv
import io
import json
from datetime import date, datetime, time

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import (
    ImportEntry,
    ImportLogEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.importer.providers.base import ImportProvider

_SHELF_PRIORITY = ["Played", "Playing", "Wishlist", "Backlog"]


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


def _to_rating(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        rating = float(raw)  # Grouvee usa 1–5
    except ValueError:
        return None
    return round(rating * 2, 1) if rating > 0 else None


def _extract_year(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return int(raw[:4])
    except ValueError:
        return None


class GrouveeProvider(ImportProvider):
    id = "grouvee"
    label = "Grouvee"
    media_types = [MediaTypeEnum.GAME]
    input_type = "file"
    accepts = ".csv"

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        if not raw:
            raise ValueError("O arquivo enviado está vazio.")

        text = raw.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            raise ValueError("CSV do Grouvee vazio.")

        # A primeira linha é o cabeçalho; ignora se não começar com o id numérico.
        data_rows = rows[1:] if not (rows[0] and rows[0][0].strip().isdigit()) else rows

        entries = []
        for row in data_rows:
            entry = self._to_entry(row)
            if entry is not None:
                entries.append(entry)

        if not entries:
            raise ValueError("Nenhum jogo reconhecido no CSV do Grouvee.")

        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=sum(1 for entry in entries if entry.logs),
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(media_type=MediaTypeEnum.GAME, entries=entries, counts=counts)

    def _to_entry(self, row: list[str]) -> ImportEntry | None:
        if len(row) < 15:
            return None
        name = (row[1] or "").strip()
        if not name:
            return None

        shelves = self._parse_shelves(row[2])
        rating = _to_rating(row[4])
        review = (row[5] or "").strip() or None
        year = _extract_year(row[12])

        status: MediaStatusEnum | None = None
        in_backlog = False
        log_date: date | None = None
        backlog_date: date | None = None

        for shelf in _SHELF_PRIORITY:
            if shelf not in shelves:
                continue
            when = shelves[shelf]
            if shelf == "Played":
                status = MediaStatusEnum.FINISHED
                log_date = when
            elif shelf == "Playing":
                status = MediaStatusEnum.IN_PROGRESS
                log_date = when
            else:  # Wishlist / Backlog
                in_backlog = True
                backlog_date = when
            break

        logs: list[ImportLogEntry] = []
        if status is not None:
            day = log_date or date.today()
            end = _to_datetime(day) if status == MediaStatusEnum.FINISHED else None
            logs.append(
                ImportLogEntry(
                    date=day,
                    start_date=_to_datetime(day),
                    end_date=end,
                    status=status,
                    rating=rating,
                    review=review,
                )
            )

        return ImportEntry(
            key=f"grouvee-{row[0].strip() or name}",
            media_type=MediaTypeEnum.GAME,
            title=name,
            year=year,
            source="grouvee",
            external_refs={},
            status=status,
            rating=rating,
            review=review,
            logs=logs,
            in_backlog=in_backlog,
            backlog_date=backlog_date,
        )

    @staticmethod
    def _parse_shelves(raw: str) -> dict[str, date | None]:
        result: dict[str, date | None] = {}
        if not raw:
            return result
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return result
        if not isinstance(data, dict):
            return result
        for name, info in data.items():
            when = None
            if isinstance(info, dict):
                when = _to_date(info.get("date_added"))
            result[str(name)] = when
        return result
