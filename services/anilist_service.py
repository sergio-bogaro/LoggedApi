"""Cliente da API GraphQL pública do AniList.

Usado pelo importador para ler listas públicas (sem autenticação) e para casar
anime/mangá. O `external_id` do Logged para anime/mangá é justamente o id do AniList.
"""

import httpx

_ANILIST_URL = "https://graphql.anilist.co"

_MEDIA_FIELDS = """
  id
  idMal
  title { romaji english native }
  coverImage { extraLarge large medium }
  startDate { year month day }
  description
  episodes
  chapters
  volumes
  format
"""

_USER_LIST_QUERY = """
query ($userName: String, $type: MediaType) {
  MediaListCollection(userName: $userName, type: $type) {
    user { mediaListOptions { scoreFormat } }
    lists {
      name
      isCustomList
      entries {
        id
        status
        score
        progress
        notes
        startedAt { year month day }
        completedAt { year month day }
        media {
""" + _MEDIA_FIELDS + """
        }
      }
    }
  }
}
"""

_SEARCH_QUERY = """
query ($search: String, $type: MediaType) {
  Page(page: 1, perPage: 20) {
    media(search: $search, type: $type, sort: POPULARITY_DESC) {
""" + _MEDIA_FIELDS + """
    }
  }
}
"""

_MEDIA_BY_ID_QUERY = """
query ($id: Int) {
  Media(id: $id) {
""" + _MEDIA_FIELDS + """
  }
}
"""

_MEDIA_BY_MAL_ID_QUERY = """
query ($idMal: Int, $type: MediaType) {
  Media(idMal: $idMal, type: $type) {
""" + _MEDIA_FIELDS + """
  }
}
"""

_MEDIA_BY_IDS_QUERY = """
query ($ids: [Int], $type: MediaType) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: $type) {
""" + _MEDIA_FIELDS + """
    }
  }
}
"""

_MEDIA_BY_MAL_IDS_QUERY = """
query ($idMal: [Int], $type: MediaType) {
  Page(page: 1, perPage: 50) {
    media(idMal_in: $idMal, type: $type) {
""" + _MEDIA_FIELDS + """
    }
  }
}
"""


def _chunks(values: list, size: int) -> list[list]:
    return [values[index:index + size] for index in range(0, len(values), size)]


class AniListService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return self._client

    async def _query(self, query: str, variables: dict) -> dict:
        try:
            resp = await self._get_client().post(
                _ANILIST_URL,
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ValueError(f"Falha ao contatar o AniList: {exc}") from exc

        if resp.status_code == 429:
            raise ValueError("O AniList limitou a taxa de requisições (429). Tente novamente.")
        if resp.status_code >= 400:
            raise ValueError(f"O AniList retornou um erro ({resp.status_code}).")

        data = resp.json()
        if data.get("errors"):
            message = data["errors"][0].get("message", "erro desconhecido")
            raise ValueError(f"AniList: {message}")
        return data.get("data") or {}

    async def get_user_list(self, username: str, media_type: str) -> dict:
        data = await self._query(
            _USER_LIST_QUERY, {"userName": username, "type": media_type}
        )
        collection = data.get("MediaListCollection")
        if not collection:
            raise ValueError(
                "Lista não encontrada. Verifique o username e se o perfil do AniList é público."
            )
        return collection

    async def search_media(self, search: str, media_type: str) -> list[dict]:
        data = await self._query(_SEARCH_QUERY, {"search": search, "type": media_type})
        return list((data.get("Page") or {}).get("media") or [])

    async def get_media(self, media_id: int) -> dict | None:
        data = await self._query(_MEDIA_BY_ID_QUERY, {"id": media_id})
        return data.get("Media")

    async def get_media_by_mal_id(self, mal_id: int, media_type: str) -> dict | None:
        data = await self._query(
            _MEDIA_BY_MAL_ID_QUERY, {"idMal": mal_id, "type": media_type}
        )
        return data.get("Media")

    async def get_media_by_ids(self, ids: list[int], media_type: str) -> list[dict]:
        """Busca várias mídias por id do AniList em lotes de 50 (evita rate limit)."""
        results: list[dict] = []
        for chunk in _chunks(ids, 50):
            data = await self._query(_MEDIA_BY_IDS_QUERY, {"ids": chunk, "type": media_type})
            results.extend((data.get("Page") or {}).get("media") or [])
        return results

    async def get_media_by_mal_ids(self, mal_ids: list[int], media_type: str) -> list[dict]:
        """Busca várias mídias por id do MyAnimeList em lotes de 50 (evita rate limit)."""
        results: list[dict] = []
        for chunk in _chunks(mal_ids, 50):
            data = await self._query(
                _MEDIA_BY_MAL_IDS_QUERY, {"idMal": chunk, "type": media_type}
            )
            results.extend((data.get("Page") or {}).get("media") or [])
        return results


service = AniListService()
