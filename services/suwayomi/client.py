"""Cliente da API GraphQL do Suwayomi-Server."""

from urllib.parse import urlparse, urlunparse

import httpx

from config import settings

_LIBRARY_QUERY = """
query ($first: Int, $offset: Int) {
  mangas(condition: { inLibrary: true }, first: $first, offset: $offset) {
    totalCount
    nodes {
      id
      title
      description
      author
      artist
      thumbnailUrl
      realUrl
      url
      status
      inLibraryAt
      lastReadChapter { id name chapterNumber lastReadAt }
      latestReadChapter { id name chapterNumber lastReadAt }
      categories {
        totalCount
        nodes { id name }
      }
    }
  }
}
"""

_CATEGORIES_QUERY = """
{
  categories {
    totalCount
    nodes { id name order default }
  }
}
"""

_ABOUT_QUERY = """
{
  aboutServer { name version }
}
"""


def normalize_base_url(url: str) -> str:
    """Valida e normaliza a URL do servidor.

    Aceita tanto ``http://host:4567`` quanto a URL do GraphQL
    (``http://host:4567/api/graphql``) e devolve sempre a raiz.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Informe a URL do Suwayomi.")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("A URL do Suwayomi deve começar com http:// ou https://.")

    path = parsed.path.rstrip("/")
    for suffix in ("/api/graphql", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    path = path.rstrip("/")

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


class SuwayomiClient:
    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        auth = (username, password) if username and password else None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth,
            timeout=timeout or settings.suwayomi_request_timeout,
            follow_redirects=True,
        )

    async def __aenter__(self) -> "SuwayomiClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query(self, query: str, variables: dict | None = None) -> dict:
        try:
            response = await self._client.post(
                "/api/graphql",
                json={"query": query, "variables": variables or {}},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ValueError(
                f"Não foi possível conectar ao Suwayomi em {self.base_url}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise ValueError(
                "O Suwayomi recusou a autenticação. Confira usuário e senha "
                "(apenas autenticação Basic é suportada)."
            )
        if response.status_code >= 400:
            raise ValueError(f"O Suwayomi retornou um erro ({response.status_code}).")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(
                "O Suwayomi respondeu algo que não é JSON. Confira a URL do servidor."
            ) from exc

        if payload.get("errors"):
            message = payload["errors"][0].get("message", "erro desconhecido")
            raise ValueError(f"Suwayomi: {message}")

        return payload.get("data") or {}

    async def about(self) -> dict:
        data = await self.query(_ABOUT_QUERY)
        return data.get("aboutServer") or {}

    async def get_categories(self) -> list[dict]:
        data = await self.query(_CATEGORIES_QUERY)
        categories = (data.get("categories") or {}).get("nodes") or []
        return [
            {
                "id": int(category["id"]),
                "name": category.get("name") or "",
                "order": int(category.get("order") or 0),
                "default": bool(category.get("default")),
            }
            for category in categories
        ]

    async def get_library(self, page_size: int = 200, max_pages: int = 100) -> list[dict]:
        """Lê toda a biblioteca em páginas (o servidor do usuário tem ~700 itens)."""
        items: list[dict] = []
        offset = 0
        total = None

        for _ in range(max_pages):
            data = await self.query(
                _LIBRARY_QUERY, {"first": page_size, "offset": offset}
            )
            page = data.get("mangas") or {}
            total = page.get("totalCount")
            nodes = page.get("nodes") or []
            items.extend(nodes)
            offset += len(nodes)
            if not nodes or len(nodes) < page_size:
                break
            if total is not None and offset >= int(total):
                break

        return items
