"""Provedor AniList (anime e mangá) — lê as listas públicas por username.

O `external_id` do Logged para anime/mangá é o id do AniList, então o match é
direto (sem busca por nome). A nota é normalizada conforme o `scoreFormat` do
perfil (POINT_100/10/10_DECIMAL/5/3/1).
"""

from datetime import date, datetime, time

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import (
    ImportEntry,
    ImportLogEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.anilist_service import service as anilist_service
from services.importer.providers.base import ImportProvider

_STATUS_MAP: dict[str, MediaStatusEnum | None] = {
    "CURRENT": MediaStatusEnum.IN_PROGRESS,
    "REPEATING": MediaStatusEnum.IN_PROGRESS,
    "COMPLETED": MediaStatusEnum.FINISHED,
    "PAUSED": MediaStatusEnum.ON_HOLD,
    "DROPPED": MediaStatusEnum.DROPPED,
    "PLANNING": None,  # entra no backlog
}


def _fuzzy_date(value: dict | None) -> date | None:
    if not value or not value.get("year"):
        return None
    try:
        return date(int(value["year"]), int(value.get("month") or 1), int(value.get("day") or 1))
    except (TypeError, ValueError):
        return None


def _to_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min)


def _normalize_score(score: int | float | None, score_format: str) -> float | None:
    if not score:
        return None
    value = float(score)
    if score_format == "POINT_100":
        return round(value / 10, 1)
    if score_format == "POINT_10_DECIMAL":
        return round(value, 1)
    if score_format == "POINT_10":
        return round(value, 1)
    if score_format == "POINT_5":
        return round(value * 2, 1)
    if score_format == "POINT_3":
        return round(value / 3 * 10, 1)
    if score_format == "POINT_1":
        return 10.0 if value >= 1 else None
    return round(value / 10, 1)


class AniListProvider(ImportProvider):
    id = "anilist"
    label = "AniList"
    media_types = [MediaTypeEnum.ANIME, MediaTypeEnum.MANGA]
    input_type = "username"

    async def parse_username(
        self, username: str, media_type: MediaTypeEnum | None
    ) -> ImportPreviewResponse:
        if not username or not username.strip():
            raise ValueError("Informe o username do AniList.")

        resolved_type = media_type if media_type in self.media_types else MediaTypeEnum.ANIME
        anilist_type = "ANIME" if resolved_type == MediaTypeEnum.ANIME else "MANGA"

        collection = await anilist_service.get_user_list(username.strip(), anilist_type)
        score_format = (
            ((collection.get("user") or {}).get("mediaListOptions") or {}).get("scoreFormat")
            or "POINT_100"
        )

        # A mesma mídia pode aparecer em listas customizadas; a primeira ocorrência
        # (listas de status padrão) vence.
        entries: dict[int, ImportEntry] = {}
        for media_list in collection.get("lists") or []:
            if media_list.get("isCustomList"):
                continue
            for row in media_list.get("entries") or []:
                media = row.get("media") or {}
                media_id = media.get("id")
                if media_id is None or media_id in entries:
                    continue
                entries[media_id] = self._to_entry(row, media, resolved_type, score_format)

        items = list(entries.values())
        counts = ImportPreviewCounts(
            total=len(items),
            with_logs=sum(1 for entry in items if entry.logs),
            rated=sum(1 for entry in items if entry.rating is not None),
            reviewed=sum(1 for entry in items if entry.review),
            backlog=sum(1 for entry in items if entry.in_backlog),
        )
        return ImportPreviewResponse(media_type=resolved_type, entries=items, counts=counts)

    def _to_entry(
        self,
        row: dict,
        media: dict,
        media_type: MediaTypeEnum,
        score_format: str,
    ) -> ImportEntry:
        media_id = int(media["id"])
        status_raw = (row.get("status") or "").upper()
        status = _STATUS_MAP.get(status_raw, MediaStatusEnum.IN_PROGRESS)
        in_backlog = status_raw == "PLANNING"

        score = _normalize_score(row.get("score"), score_format)
        notes = (row.get("notes") or "").strip() or None

        started = _fuzzy_date(row.get("startedAt"))
        completed = _fuzzy_date(row.get("completedAt"))

        logs: list[ImportLogEntry] = []
        if not in_backlog:
            day = completed or started or date.today()
            logs.append(
                ImportLogEntry(
                    date=day,
                    start_date=_to_datetime(started),
                    end_date=_to_datetime(completed),
                    status=status,
                    rating=score,
                    review=notes,
                )
            )

        title = media.get("title") or {}
        name = title.get("english") or title.get("romaji") or title.get("native") or ""
        start_year = (media.get("startDate") or {}).get("year")
        cover = media.get("coverImage") or {}

        return ImportEntry(
            key=f"anilist-{media_id}",
            media_type=media_type,
            title=name,
            year=start_year,
            source="anilist",
            external_refs={"anilistId": str(media_id)},
            status=status,
            rating=score,
            review=notes,
            logs=logs,
            in_backlog=in_backlog,
            backlog_date=None,
        )
