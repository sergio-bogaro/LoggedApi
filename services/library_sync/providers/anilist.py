"""Adaptador do AniList (anime e mangá).

A lista pública é lida sem autenticação via `MediaListCollection`. O id do
AniList já é o `external_id` usado pelo Logged para anime/mangá.
"""

from datetime import date

from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import LibraryConnection
from services.anilist_service import service as anilist_service
from services.library_sync.providers.base import (
    LibraryProvider,
    SourceItem,
    SourceSnapshot,
    SourceStatus,
)
from services.library_sync.providers.common import (
    image_url,
    to_date,
    to_datetime,
    to_float,
    year_of,
)

_QUERY = """
query ($userName: String, $type: MediaType) {
  MediaListCollection(userName: $userName, type: $type) {
    user { id name }
    lists {
      name
      status
      isCustomList
      entries {
        id
        status
        score(format: POINT_10_DECIMAL)
        notes
        updatedAt
        startedAt { year month day }
        completedAt { year month day }
        media {
          id
          title { romaji english native }
          coverImage { extraLarge large medium }
          description
          startDate { year month day }
        }
      }
    }
  }
}
"""


class AniListProvider(LibraryProvider):
    id = "anilist"
    label = "AniList"
    media_types = [MediaTypeEnum.ANIME, MediaTypeEnum.MANGA]
    auth_kind = "username"
    trusted_external_id = True
    creates_logs = True

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("CURRENT", "Assistindo/Lendo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("REPEATING", "Reassistindo/Relendo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("COMPLETED", "Completo", "finished", MediaStatusEnum.FINISHED),
            SourceStatus("PAUSED", "Em pausa", "on_hold", MediaStatusEnum.ON_HOLD),
            SourceStatus("DROPPED", "Dropado", "dropped", MediaStatusEnum.DROPPED),
            SourceStatus("PLANNING", "Planejado", "backlog", None),
        ]

    async def test(self, connection: LibraryConnection) -> str:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do AniList.")
        user = await anilist_service.get_user(username)
        if not user:
            raise ValueError("Username do AniList não encontrado.")
        return str(user.get("name") or username)

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do AniList.")

        items: dict[int, SourceItem] = {}
        for media_type in self.media_types:
            anilist_type = "ANIME" if media_type == MediaTypeEnum.ANIME else "MANGA"
            data = await anilist_service.query(
                _QUERY, {"userName": username, "type": anilist_type}
            )
            collection = data.get("MediaListCollection")
            if not collection:
                raise ValueError(
                    "Lista do AniList não encontrada. Verifique o username e se o "
                    "perfil é público."
                )
            for group in collection.get("lists") or []:
                if group.get("isCustomList"):
                    continue
                for entry in group.get("entries") or []:
                    media = entry.get("media") or {}
                    media_id = media.get("id")
                    if media_id is None or int(media_id) in items:
                        continue
                    title_block = media.get("title") or {}
                    title = (
                        title_block.get("english")
                        or title_block.get("romaji")
                        or title_block.get("native")
                        or str(media_id)
                    )
                    start = to_date(_fuzzy(media.get("startDate")))
                    status = str(entry.get("status") or "CURRENT")
                    started = to_date(_fuzzy(entry.get("startedAt")))
                    completed = to_date(_fuzzy(entry.get("completedAt")))
                    # Mesma regra do importador: itens fora do backlog ganham
                    # um log com a data de conclusão/início (ou hoje).
                    watched = (
                        None
                        if status == "PLANNING"
                        else (completed or started or date.today())
                    )
                    items[int(media_id)] = SourceItem(
                        source_id=str(media_id),
                        media_type=media_type,
                        title=title,
                        status=status,
                        year=year_of(start) if start else None,
                        rating=_normalize_score(entry.get("score")),
                        review=(entry.get("notes") or None),
                        overview=media.get("description") or None,
                        cover_url=image_url(media.get("coverImage")),
                        release_date=start,
                        external_id=str(media_id),
                        external_refs={"anilistId": str(media_id)},
                        source_url=f"https://anilist.co/{'anime' if media_type == MediaTypeEnum.ANIME else 'manga'}/{media_id}",
                        updated_at=to_datetime(entry.get("updatedAt")),
                        watched_at=watched,
                    )
        return SourceSnapshot(items=list(items.values()), statuses=self.statuses())


def _fuzzy(value) -> date | None:
    if not isinstance(value, dict) or not value.get("year"):
        return None
    try:
        return date(int(value["year"]), int(value.get("month") or 1), int(value.get("day") or 1))
    except (TypeError, ValueError):
        return None


def _normalize_score(value) -> float | None:
    score = to_float(value)
    if score is None or score <= 0:
        return None
    return round(min(score, 10.0), 1)
