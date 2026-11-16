"""Cliente da API GraphQL pública do AniList.

Usado pelo importador para ler listas públicas (sem autenticação) e para casar
anime/mangá. O `external_id` do Logged para anime/mangá é justamente o id do AniList.
"""

import asyncio
import threading
import time

import httpx

_ANILIST_URL = "https://graphql.anilist.co"

# O AniList anuncia 90 req/min, mas atualmente opera em modo degradado
# (30 req/min) e tem um burst limiter. Começamos pelo limite conservador,
# ajustamos pelos headers e respeitamos Retry-After/X-RateLimit-Reset.
_ANILIST_DEFAULT_LIMIT = 30
_anilist_lock = threading.Lock()
_anilist_limit = _ANILIST_DEFAULT_LIMIT
_anilist_interval = 60.0 / _ANILIST_DEFAULT_LIMIT
_anilist_next_slot = 0.0
_anilist_blocked_until = 0.0


def _observe_rate_limit(headers) -> None:
    global _anilist_limit, _anilist_interval, _anilist_blocked_until

    limit_raw = headers.get("X-RateLimit-Limit")
    remaining_raw = headers.get("X-RateLimit-Remaining")
    reset_raw = headers.get("X-RateLimit-Reset")

    with _anilist_lock:
        if limit_raw:
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                limit = 0
            if limit > 0:
                _anilist_limit = limit
                # Nunca menos de 0,5 s entre chamadas, para não estourar o
                # burst limiter mesmo com o limite de 90/min.
                _anilist_interval = max(60.0 / limit, 0.5)

        if remaining_raw == "0" and reset_raw:
            try:
                reset_epoch = float(reset_raw)
            except (TypeError, ValueError):
                reset_epoch = 0
            if reset_epoch > 0:
                wait = max(reset_epoch - time.time(), 0.0)
                _anilist_blocked_until = max(
                    _anilist_blocked_until, time.monotonic() + wait
                )


def _observe_retry_after(headers) -> None:
    global _anilist_blocked_until

    raw = headers.get("Retry-After")
    try:
        delay = float(raw) if raw else 5.0
    except (TypeError, ValueError):
        delay = 5.0
    delay = min(max(delay, 1.0), 120.0)
    with _anilist_lock:
        _anilist_blocked_until = max(
            _anilist_blocked_until, time.monotonic() + delay
        )


async def _throttle_anilist() -> None:
    global _anilist_next_slot

    while True:
        with _anilist_lock:
            now = time.monotonic()
            wait = max(
                _anilist_blocked_until - now,
                _anilist_next_slot - now,
                0.0,
            )
            if wait <= 0:
                _anilist_next_slot = now + _anilist_interval
                return
        # Acorda no máximo a cada 30 s para reavaliar bloqueios longos.
        await asyncio.sleep(min(wait, 30.0))



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
    async def _query(self, query: str, variables: dict) -> dict:
        for attempt in range(5):
            await _throttle_anilist()
            try:
                # Cliente por chamada: o serviço também é usado em event loops
                # criados por syncs em background, e um AsyncClient não pode ser
                # compartilhado entre loops.
                async with httpx.AsyncClient(
                    timeout=20.0, follow_redirects=True
                ) as client:
                    response = await client.post(
                        _ANILIST_URL,
                        json={"query": query, "variables": variables},
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                    )
            except httpx.HTTPError as exc:
                raise ValueError(f"Falha ao contatar o AniList: {exc}") from exc

            _observe_rate_limit(response.headers)

            if response.status_code == 429:
                _observe_retry_after(response.headers)
                if attempt < 4:
                    # O próximo _throttle_anilist espera o bloqueio.
                    continue
                raise ValueError(
                    "O AniList limitou a taxa de requisições (429) repetidamente. "
                    "Tente novamente mais tarde."
                )

            if response.status_code >= 400:
                raise ValueError(f"O AniList retornou um erro ({response.status_code}).")

            data = response.json()
            if data.get("errors"):
                message = data["errors"][0].get("message", "erro desconhecido")
                raise ValueError(f"AniList: {message}")
            return data.get("data") or {}

        raise ValueError("Falha ao contatar o AniList.")

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
