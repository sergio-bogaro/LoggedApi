"""Adaptador do Kitsu (anime e mangá).

A biblioteca pública é lida sem autenticação. O Kitsu usa ids próprios; o
matcher resolve para o AniList pelo título quando não há mapeamento direto.
"""

from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import LibraryConnection
from services.library_sync.providers.base import (
    LibraryProvider,
    SourceItem,
    SourceSnapshot,
    SourceStatus,
)
from services.library_sync.providers.common import (
    client,
    image_url,
    to_date,
    to_float,
    year_of,
)

_API = "https://kitsu.io/api/edge"
_PAGE = 500


class KitsuProvider(LibraryProvider):
    id = "kitsu"
    label = "Kitsu"
    media_types = [MediaTypeEnum.ANIME, MediaTypeEnum.MANGA]
    auth_kind = "username"

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("current", "Assistindo/Lendo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("completed", "Completo", "finished", MediaStatusEnum.FINISHED),
            SourceStatus("onHold", "Em pausa", "on_hold", MediaStatusEnum.ON_HOLD),
            SourceStatus("dropped", "Dropado", "dropped", MediaStatusEnum.DROPPED),
            SourceStatus("planned", "Planejado", "backlog", None),
        ]

    async def _user_id(self, username: str) -> str:
        async with client() as http:
            response = await http.get(
                f"{_API}/users",
                params={"filter[name]": username, "page[limit]": 1},
            )
        response.raise_for_status()
        data = response.json().get("data") or []
        if not data:
            raise ValueError("Username do Kitsu não encontrado.")
        return str(data[0]["id"])

    async def test(self, connection: LibraryConnection) -> str:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do Kitsu.")
        await self._user_id(username)
        return username

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do Kitsu.")
        user_id = await self._user_id(username)

        items: dict[str, SourceItem] = {}
        async with client() as http:
            url = f"{_API}/users/{user_id}/library-entries"
            params = {
                "page[limit]": _PAGE,
                "page[offset]": 0,
                "include": "anime,manga",
            }
            while url:
                response = await http.get(url, params=params)
                response.raise_for_status()
                body = response.json()
                included = {
                    (row.get("type"), str(row.get("id"))): row
                    for row in body.get("included") or []
                }
                for entry in body.get("data") or []:
                    self._add(items, entry, included)
                next_url = ((body.get("links") or {}).get("next") or "").strip()
                if not next_url:
                    break
                url = next_url
                params = None
        return SourceSnapshot(items=list(items.values()), statuses=self.statuses())

    def _add(
        self,
        items: dict[str, SourceItem],
        entry: dict,
        included: dict[tuple[str, str], dict],
    ) -> None:
        attrs = entry.get("attributes") or {}
        relationships = entry.get("relationships") or {}
        media_type = None
        media_id = None
        for rel_type, kind in (("anime", MediaTypeEnum.ANIME), ("manga", MediaTypeEnum.MANGA)):
            rel = (relationships.get(rel_type) or {}).get("data")
            if isinstance(rel, dict) and rel.get("id"):
                media_type = kind
                media_id = str(rel.get("id"))
                break
        if media_type is None or media_id is None:
            return
        media = included.get(("anime" if media_type == MediaTypeEnum.ANIME else "manga", media_id), {})
        media_attrs = media.get("attributes") or {}
        titles = media_attrs.get("titles") or {}
        title = (
            media_attrs.get("canonicalTitle")
            or titles.get("en")
            or titles.get("en_jp")
            or titles.get("ja_jp")
            or f"Kitsu {media_id}"
        )
        start = to_date(media_attrs.get("startDate"))
        rating = None
        rating20 = to_float(attrs.get("ratingTwenty"))
        if rating20 and rating20 > 0:
            rating = round(rating20 / 2.0, 1)
        source_id = f"kitsu:{entry.get('id')}"
        items[source_id] = SourceItem(
            source_id=source_id,
            media_type=media_type,
            title=title,
            status=str(attrs.get("status") or "planned"),
            year=year_of(start) if start else None,
            rating=rating,
            review=(attrs.get("notes") or None),
            overview=media_attrs.get("synopsis") or None,
            cover_url=image_url(media_attrs.get("posterImage")),
            release_date=start,
            external_refs={"kitsuId": media_id, "kitsuEntryId": str(entry.get("id"))},
            source_url=f"https://kitsu.app/{'anime' if media_type == MediaTypeEnum.ANIME else 'manga'}/{media_id}",
        )
