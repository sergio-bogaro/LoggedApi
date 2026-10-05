"""Cliente da API da OpenLibrary.

Usado pelo importador de livros: ISBN → edição → obra (work). O `external_id`
do Logged para livros é o id da obra (ex.: `OL12345W`).
"""

import httpx

_OPENLIBRARY_URL = "https://openlibrary.org"


def work_id_from_key(key: str | None) -> str:
    """`/works/OL12345W` (ou `OL12345W`) → `OL12345W`."""
    if not key:
        return ""
    parts = [part for part in key.split("/") if part]
    if "works" in parts:
        index = parts.index("works")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1]


class OpenLibraryService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return self._client

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            resp = await self._get_client().get(f"{_OPENLIBRARY_URL}{path}", params=params)
        except httpx.HTTPError as exc:
            raise ValueError(f"Falha ao contatar a OpenLibrary: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ValueError(f"A OpenLibrary retornou um erro ({resp.status_code}).")
        return resp.json()

    async def search(self, query: str) -> list[dict]:
        data = await self._get("/search.json", {"title": query, "limit": 20})
        return list((data or {}).get("docs") or [])

    async def get_by_isbn(self, isbn: str) -> dict | None:
        return await self._get(f"/isbn/{isbn}.json")

    async def get_work(self, key: str) -> dict | None:
        path = key if key.startswith("/") else f"/works/{key}"
        return await self._get(path)


service = OpenLibraryService()
