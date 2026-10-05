"""Provedor MyAnimeList (anime/mangá) — lê o XML `.xml.gz` do export.

O MAL usa ids próprios; eles são resolvidos para o id do AniList no matcher
(AniList expõe o `idMal`). A nota já está na escala 0–10.
"""

import gzip
import xml.etree.ElementTree as ET
from datetime import date, datetime, time

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import (
    ImportEntry,
    ImportLogEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.importer.providers.base import ImportProvider

# 1 watching/reading, 2 completed, 3 on hold, 4 dropped, 6 plan to watch/read.
_STATUS_MAP: dict[str, MediaStatusEnum | None] = {
    "1": MediaStatusEnum.IN_PROGRESS,
    "2": MediaStatusEnum.FINISHED,
    "3": MediaStatusEnum.ON_HOLD,
    "4": MediaStatusEnum.DROPPED,
    "6": None,  # entra no backlog
}


def _text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw or raw.startswith("0000"):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _to_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min)


class MalProvider(ImportProvider):
    id = "mal"
    label = "MyAnimeList"
    media_types = [MediaTypeEnum.ANIME, MediaTypeEnum.MANGA]
    input_type = "file"
    accepts = ".xml,.gz"

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        if not raw:
            raise ValueError("O arquivo enviado está vazio.")

        data = self._maybe_gunzip(raw, filename)
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ValueError("Não foi possível ler o XML do MyAnimeList.") from exc

        anime_entries = root.findall("anime/entry")
        manga_entries = root.findall("manga/entry")

        if anime_entries:
            media_type = MediaTypeEnum.ANIME
            entries = [self._to_entry(element, media_type) for element in anime_entries]
        elif manga_entries:
            media_type = MediaTypeEnum.MANGA
            entries = [self._to_entry(element, media_type) for element in manga_entries]
        else:
            raise ValueError(
                "Nenhuma lista de anime ou mangá foi encontrada. Exporte a lista no "
                "MyAnimeList (myanimelist.net/panel.php?go=export) e envie o .xml.gz."
            )

        entries = [entry for entry in entries if entry.external_refs.get("malId")]
        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=sum(1 for entry in entries if entry.logs),
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(media_type=media_type, entries=entries, counts=counts)

    def _maybe_gunzip(self, raw: bytes, filename: str) -> bytes:
        if raw[:2] == b"\x1f\x8b" or (filename or "").lower().endswith(".gz"):
            try:
                return gzip.decompress(raw)
            except OSError as exc:
                raise ValueError("O arquivo .gz não pôde ser descompactado.") from exc
        return raw

    def _to_entry(self, element: ET.Element, media_type: MediaTypeEnum) -> ImportEntry:
        if media_type == MediaTypeEnum.ANIME:
            mal_id = _to_int(_text(element, "series_animedb_id"))
            title = _text(element, "series_title")
        else:
            mal_id = _to_int(_text(element, "manga_mangadb_id"))
            title = _text(element, "manga_title")

        score = _to_int(_text(element, "my_score"))
        rating = float(score) if score and score > 0 else None
        review = _text(element, "my_comments") or None

        status_raw = _text(element, "my_status")
        status = _STATUS_MAP.get(status_raw, MediaStatusEnum.IN_PROGRESS)
        in_backlog = status_raw == "6"

        started = _to_date(_text(element, "my_start_date"))
        finished = _to_date(_text(element, "my_finish_date"))

        logs: list[ImportLogEntry] = []
        if not in_backlog:
            day = finished or started or date.today()
            logs.append(
                ImportLogEntry(
                    date=day,
                    start_date=_to_datetime(started),
                    end_date=_to_datetime(finished),
                    status=status,
                    rating=rating,
                    review=review,
                )
            )

        return ImportEntry(
            key=f"mal-{mal_id}",
            media_type=media_type,
            title=title or str(mal_id or ""),
            year=None,
            source="mal",
            external_refs={"malId": str(mal_id)} if mal_id is not None else {},
            status=status,
            rating=rating,
            review=review,
            logs=logs,
            in_backlog=in_backlog,
            backlog_date=None,
        )
