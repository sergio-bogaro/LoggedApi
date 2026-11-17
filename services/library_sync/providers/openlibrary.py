"""Adaptador do Open Library (livros).

Lê o reading log público de um usuário. O work id do Open Library é o
`external_id` usado pelo Logged para livros.
"""

import asyncio

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
    year_of,
)

_API = "https://openlibrary.org"
_PAGE = 100
_SHELVES = [
    ("want-to-read", "Quero ler", "backlog", None, 1),
    ("currently-reading", "Lendo", "in_progress", MediaStatusEnum.IN_PROGRESS, 3),
    ("already-read", "Já li", "finished", MediaStatusEnum.FINISHED, 4),
    ("stopped-reading", "Abandonei", "dropped", MediaStatusEnum.DROPPED, 2),
]


class OpenLibraryProvider(LibraryProvider):
    id = "openlibrary"
    label = "Open Library"
    media_types = [MediaTypeEnum.BOOK]
    auth_kind = "username"

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("currently-reading", "Lendo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("already-read", "Já li", "finished", MediaStatusEnum.FINISHED),
            SourceStatus("stopped-reading", "Abandonei", "dropped", MediaStatusEnum.DROPPED),
            SourceStatus("want-to-read", "Quero ler", "backlog", None),
        ]

    async def test(self, connection: LibraryConnection) -> str:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do Open Library.")
        async with client() as http:
            response = await http.get(
                f"{_API}/people/{username}/books/want-to-read.json",
                params={"limit": 1},
            )
        if response.status_code == 404:
            raise ValueError("Reading log público do Open Library não encontrado.")
        response.raise_for_status()
        return username

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do Open Library.")

        items: dict[str, SourceItem] = {}
        priorities: dict[str, int] = {}
        async with client() as http:
            for shelf, label, action, _status, priority in _SHELVES:
                page = 1
                while True:
                    response = None
                    for attempt in range(3):
                        response = await http.get(
                            f"{_API}/people/{username}/books/{shelf}.json",
                            params={"limit": _PAGE, "page": page},
                        )
                        if response.status_code < 500 or attempt == 2:
                            break
                        await asyncio.sleep(1.5 * (attempt + 1))
                    if response.status_code == 404:
                        # Perfil privado ou sem essa estante.
                        if shelf == "want-to-read":
                            raise ValueError(
                                "Reading log público do Open Library não encontrado. "
                                "Verifique o username e a visibilidade do perfil."
                            )
                        break
                    response.raise_for_status()
                    body = response.json()
                    rows = body.get("reading_log_entries") or []
                    for row in rows:
                        self._add(items, priorities, row, shelf, label, action, priority)
                    if len(rows) < _PAGE:
                        break
                    page += 1
                    await asyncio.sleep(0.35)
        return SourceSnapshot(items=list(items.values()), statuses=self.statuses())

    def _add(
        self,
        items: dict[str, SourceItem],
        priorities: dict[str, int],
        row: dict,
        shelf: str,
        label: str,
        action: str,
        priority: int,
    ) -> None:
        work = row.get("work") or {}
        key = str(work.get("key") or "")
        if not key:
            return
        source_id = key.rsplit("/", 1)[-1]
        if not source_id:
            return
        previous = priorities.get(source_id, 0)
        if previous > priority:
            return
        priorities[source_id] = priority
        authors = work.get("author_names") or []
        cover_id = work.get("cover_id")
        item = SourceItem(
            source_id=source_id,
            media_type=MediaTypeEnum.BOOK,
            title=str(work.get("title") or source_id),
            status=shelf,
            year=year_of(work.get("first_publish_year")),
            review=None,
            overview=f"Autor: {authors[0]}" if authors else None,
            cover_url=(
                f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
                if cover_id
                else None
            ),
            release_date=None,
            external_id=source_id,
            external_refs={"openlibraryId": source_id},
            source_url=f"https://openlibrary.org/works/{source_id}",
        )
        # O reading log não tem nota nem resenha; o status é a informação útil.
        item.updated_at = None
        items[source_id] = item
