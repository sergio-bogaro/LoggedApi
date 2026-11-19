"""Adaptador do Hardcover (livros).

GraphQL com Personal Access Token. Como o Logged usa Open Library como
`external_id` de livros, o ISBN é enviado no `external_refs` e o matcher resolve
para o work id correspondente.
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
from domain_errors import DomainError

_API = "https://api.hardcover.app/v1/graphql"
_PAGE = 1000

_QUERY = """
query ($offset: Int!) {
  me {
    id
    username
    user_books(limit: 1000, offset: $offset, order_by: {id: asc}) {
      id
      status_id
      rating
      review
      date_added
      first_read_date
      last_read_date
      edition { isbn_13 }
      book {
        id
        title
        pages
        release_date
        image { url }
        contributions { author { name } }
      }
    }
  }
}
"""


class HardcoverProvider(LibraryProvider):
    id = "hardcover"
    label = "Hardcover"
    media_types = [MediaTypeEnum.BOOK]
    auth_kind = "token"
    creates_logs = True

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("1", "Quero ler", "backlog", None),
            SourceStatus("2", "Lendo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("3", "Lido", "finished", MediaStatusEnum.FINISHED),
            SourceStatus("4", "Em pausa", "on_hold", MediaStatusEnum.ON_HOLD),
            SourceStatus("5", "Não terminei", "dropped", MediaStatusEnum.DROPPED),
            SourceStatus("6", "Ignorado", "ignore", None),
        ]

    @staticmethod
    def _token(connection: LibraryConnection) -> str:
        token = str(LibraryProvider.auth(connection).get("token") or "").strip()
        if not token:
            raise DomainError(
                "Informe o Personal Access Token do Hardcover em Configurações > Integrações."
            )
        return token

    async def _query(self, connection: LibraryConnection, query: str, variables: dict) -> dict:
        token = self._token(connection)
        async with client() as http:
            response = await http.post(
                _API,
                json={"query": query, "variables": variables},
                headers={"Authorization": token, "Content-Type": "application/json"},
            )
            if response.status_code in (401, 403):
                response = await http.post(
                    _API,
                    json={"query": query, "variables": variables},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
        if response.status_code in (401, 403):
            connection.needs_reauth = True
            raise DomainError("Token do Hardcover inválido ou expirado.")
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            message = body["errors"][0].get("message", "erro desconhecido")
            raise DomainError(f"Hardcover: {message}")
        return body.get("data") or {}

    async def test(self, connection: LibraryConnection) -> str:
        data = await self._query(connection, "query { me { id username } }", {})
        me = data.get("me")
        if not isinstance(me, dict):
            raise DomainError("Token do Hardcover inválido.")
        return str(me.get("username") or me.get("id") or "Hardcover")

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        items: dict[str, SourceItem] = {}
        offset = 0
        while True:
            data = await self._query(connection, _QUERY, {"offset": offset})
            me = data.get("me")
            if not isinstance(me, dict):
                raise DomainError("Token do Hardcover inválido.")
            rows = me.get("user_books") or []
            for row in rows:
                self._add(items, row)
            if len(rows) < _PAGE:
                break
            offset += _PAGE
        return SourceSnapshot(items=list(items.values()), statuses=self.statuses())

    def _add(self, items: dict[str, SourceItem], row: dict) -> None:
        book = row.get("book") or {}
        source_id = f"hardcover:{row.get('id')}"
        status_id = str(row.get("status_id") or "1")
        rating = to_float(row.get("rating"))
        rating_10 = (
            round(min(max(rating / 5.0 * 10.0, 0.0), 10.0), 1)
            if rating is not None and rating > 0
            else None
        )
        release = to_date(book.get("release_date"))
        authors = [
            (entry.get("author") or {}).get("name")
            for entry in book.get("contributions") or []
            if (entry.get("author") or {}).get("name")
        ]
        isbn = str(
            (row.get("edition") or {}).get("isbn_13")
            or next(
                (
                    str(edition.get("isbn_13"))
                    for edition in book.get("editions") or []
                    if edition.get("isbn_13")
                ),
                "",
            )
        ).strip()
        items[source_id] = SourceItem(
            source_id=source_id,
            media_type=MediaTypeEnum.BOOK,
            title=str(book.get("title") or f"Hardcover {row.get('id')}"),
            status=status_id,
            year=year_of(release),
            rating=rating_10,
            review=row.get("review") or None,
            overview=", ".join(authors) if authors else None,
            cover_url=image_url(book.get("image")),
            release_date=release,
            external_refs={"isbn13": isbn} if isbn else {},
            source_url=f"https://hardcover.app/books/{book.get('id') or ''}".rstrip("/"),
            watched_at=to_date(
                row.get("last_read_date") or row.get("first_read_date")
            ),
        )
