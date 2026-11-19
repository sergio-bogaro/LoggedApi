"""Adaptador do MyAnimeList (anime e mangá).

Lê as listas públicas com `X-MAL-CLIENT-ID` (sem OAuth de usuário) e resolve o
id do MAL para o id do AniList, que é o `external_id` do Logged.
"""

from datetime import date

from config import settings
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
    client,
    image_url,
    to_date,
    to_float,
    year_of,
)
from domain_errors import DomainError

_API = "https://api.myanimelist.net/v2"
_FIELDS = "list_status,alternative_titles,main_picture,synopsis,start_date,media_type"
_PAGE = 1000


class MalProvider(LibraryProvider):
    id = "mal"
    label = "MyAnimeList"
    media_types = [MediaTypeEnum.ANIME, MediaTypeEnum.MANGA]
    auth_kind = "username"
    trusted_external_id = True
    creates_logs = True

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("watching", "Assistindo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("reading", "Lendo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("completed", "Completo", "finished", MediaStatusEnum.FINISHED),
            SourceStatus("on_hold", "Em pausa", "on_hold", MediaStatusEnum.ON_HOLD),
            SourceStatus("dropped", "Dropado", "dropped", MediaStatusEnum.DROPPED),
            SourceStatus("plan_to_watch", "Planejado (anime)", "backlog", None),
            SourceStatus("plan_to_read", "Planejado (mangá)", "backlog", None),
        ]

    def _client_id(self, connection: LibraryConnection) -> str:
        value = str(self.auth(connection).get("client_id") or "").strip()
        return value or (settings.mal_client_id or "").strip()

    async def test(self, connection: LibraryConnection) -> str:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise DomainError("Informe o username do MyAnimeList.")
        if not self._client_id(connection):
            raise DomainError(
                "Configure o Client ID do MyAnimeList (MAL_CLIENT_ID) para ler listas públicas."
            )
        async with client() as http:
            response = await http.get(
                f"{_API}/users/{username}/animelist",
                params={"fields": "list_status", "limit": 1},
                headers={"X-MAL-CLIENT-ID": self._client_id(connection)},
            )
        if response.status_code == 404:
            raise DomainError("Username do MyAnimeList não encontrado.")
        if response.status_code in (401, 403):
            raise DomainError("Client ID do MyAnimeList inválido ou sem permissão.")
        response.raise_for_status()
        return username

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        username = str(self.auth(connection).get("username") or "").strip()
        client_id = self._client_id(connection)
        if not username:
            raise DomainError("Informe o username do MyAnimeList.")
        if not client_id:
            raise DomainError(
                "Configure o Client ID do MyAnimeList (MAL_CLIENT_ID) para ler listas públicas."
            )

        items: dict[str, SourceItem] = {}
        async with client() as http:
            for media_type, endpoint in (
                (MediaTypeEnum.ANIME, "animelist"),
                (MediaTypeEnum.MANGA, "mangalist"),
            ):
                offset = 0
                while True:
                    response = await http.get(
                        f"{_API}/users/{username}/{endpoint}",
                        params={
                            "fields": _FIELDS,
                            "limit": _PAGE,
                            "offset": offset,
                        },
                        headers={"X-MAL-CLIENT-ID": client_id},
                    )
                    if response.status_code == 404:
                        raise DomainError("Username do MyAnimeList não encontrado.")
                    if response.status_code in (401, 403):
                        raise DomainError(
                            "Client ID do MyAnimeList inválido ou sem permissão."
                        )
                    response.raise_for_status()
                    body = response.json()
                    rows = body.get("data") or []
                    for row in rows:
                        self._add(items, row, media_type)
                    if len(rows) < _PAGE:
                        break
                    offset += _PAGE

        await self._resolve_external_ids(items)
        return SourceSnapshot(items=list(items.values()), statuses=self.statuses())

    def _add(self, items: dict[str, SourceItem], row: dict, media_type: MediaTypeEnum) -> None:
        node = row.get("node") or {}
        status_block = row.get("list_status") or {}
        mal_id = node.get("id")
        if mal_id is None:
            return
        title_block = node.get("alternative_titles") or {}
        title = (
            node.get("title")
            or title_block.get("en")
            or title_block.get("ja")
            or str(mal_id)
        )
        start = to_date(node.get("start_date"))
        score = to_float(status_block.get("score"))
        rating = round(score, 1) if score and score > 0 else None
        status = str(status_block.get("status") or "plan_to_watch")
        started = to_date(status_block.get("start_date"))
        finished = to_date(status_block.get("finish_date"))
        watched = (
            None
            if status in ("plan_to_watch", "plan_to_read")
            else (finished or started or date.today())
        )
        source_id = f"mal:{mal_id}"
        items[source_id] = SourceItem(
            source_id=source_id,
            media_type=media_type,
            title=title,
            status=status,
            year=year_of(start) if start else None,
            rating=rating,
            review=(status_block.get("comments") or None),
            overview=node.get("synopsis") or None,
            cover_url=image_url(node.get("main_picture")),
            release_date=start,
            external_id=None,
            external_refs={"malId": str(mal_id)},
            source_url=f"https://myanimelist.net/{'anime' if media_type == MediaTypeEnum.ANIME else 'manga'}/{mal_id}",
            watched_at=watched,
        )

    async def _resolve_external_ids(self, items: dict[str, SourceItem]) -> None:
        for media_type in (MediaTypeEnum.ANIME, MediaTypeEnum.MANGA):
            subset = [item for item in items.values() if item.media_type == media_type]
            if not subset:
                continue
            mal_ids = [
                int(item.external_refs["malId"])
                for item in subset
                if item.external_refs.get("malId", "").isdigit()
            ]
            if not mal_ids:
                continue
            anilist_type = "ANIME" if media_type == MediaTypeEnum.ANIME else "MANGA"
            try:
                media_rows = await anilist_service.get_media_by_mal_ids(
                    mal_ids, anilist_type
                )
            except ValueError:
                continue
            by_mal = {
                str(row.get("idMal")): str(row.get("id"))
                for row in media_rows
                if row.get("idMal") and row.get("id")
            }
            for item in subset:
                anilist_id = by_mal.get(item.external_refs.get("malId", ""))
                if anilist_id:
                    item.external_id = anilist_id
                    item.external_refs["anilistId"] = anilist_id
