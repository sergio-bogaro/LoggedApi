"""Cliente da API da OpenLibrary.

Usado pelo importador de livros: ISBN → edição → obra (work). O `external_id`
do Logged para livros é o id da obra (ex.: `OL12345W`).
"""

import asyncio
import time

import httpx
from domain_errors import DomainError
from services.http_client import get_async_client, get_loop_lock

_OPENLIBRARY_URL = "https://openlibrary.org"
_OPENLIBRARY_INTERVAL = 0.35
_openlibrary_next = 0.0


async def _throttle() -> None:
    global _openlibrary_next
    async with get_loop_lock("openlibrary"):
        now = time.monotonic()
        slot = max(now, _openlibrary_next)
        _openlibrary_next = slot + _OPENLIBRARY_INTERVAL
    delay = slot - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


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
    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        resp = None
        for attempt in range(3):
            await _throttle()
            try:
                client = get_async_client("openlibrary", timeout=20.0)
                resp = await client.get(
                    f"{_OPENLIBRARY_URL}{path}", params=params
                )
            except httpx.HTTPError:
                raise DomainError("Falha ao contatar a OpenLibrary.")
            if resp.status_code < 500 and resp.status_code != 429:
                break
            await asyncio.sleep(1.0 * (attempt + 1))

        assert resp is not None
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise DomainError(f"A OpenLibrary retornou um erro ({resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            raise DomainError("A OpenLibrary retornou uma resposta inválida.")
        if not isinstance(data, dict):
            raise DomainError("A OpenLibrary retornou uma resposta inesperada.")
        return data

    async def search(self, query: str) -> list[dict]:
        data = await self._get("/search.json", {"title": query, "limit": 20})
        return list((data or {}).get("docs") or [])

    async def get_by_isbn(self, isbn: str) -> dict | None:
        return await self._get(f"/isbn/{isbn}.json")

    async def get_work(self, key: str) -> dict | None:
        path = key if key.startswith("/") else f"/works/{key}"
        if not path.endswith(".json"):
            path += ".json"
        return await self._get(path)


service = OpenLibraryService()
