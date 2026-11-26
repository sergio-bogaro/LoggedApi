import httpx

from config import settings
from domain_errors import DomainError
from services.http_client import get_async_client


class TmdbService:
    """Proxy para a API do TMDB usando a chave resolvida (usuário ou instância)."""

    async def _get(
        self,
        path: str,
        api_key: str,
        extra: dict[str, str | int | float] | None = None,
    ) -> dict:
        params: dict[str, str | int | float] = {
            "api_key": api_key,
            "language": "en-US",
        }
        if extra:
            params.update(extra)

        try:
            client = get_async_client(
                "tmdb", timeout=settings.tmdb_request_timeout
            )
            resp = await client.get(
                f"{settings.tmdb_base_url}{path}", params=params
            )
        except httpx.HTTPError:
            # A URL contém api_key; nunca propague a mensagem do httpx.
            raise DomainError("Falha ao contatar o TMDB.")

        if resp.status_code == 401:
            raise DomainError(
                "O TMDB rejeitou a chave de API (401). Verifique a chave configurada."
            )
        if resp.status_code == 429:
            raise DomainError(
                "O TMDB limitou a taxa de requisições (429). Tente novamente em instantes."
            )
        if resp.status_code >= 400:
            raise DomainError(f"O TMDB retornou um erro ({resp.status_code}).")

        try:
            data = resp.json()
        except ValueError:
            raise DomainError("O TMDB retornou uma resposta inválida.")
        if not isinstance(data, dict):
            raise DomainError("O TMDB retornou uma resposta inesperada.")
        return data

    async def search_movies(
        self, query: str, api_key: str, year: int | None = None
    ) -> dict:
        extra: dict[str, str | int | float] = {"query": query, "page": 1}
        if year:
            extra["year"] = year
        return await self._get("/search/movie", api_key, extra)

    async def get_movie(self, movie_id: int, api_key: str) -> dict:
        return await self._get(
            f"/movie/{movie_id}",
            api_key,
            {"append_to_response": "credits,videos,images,recommendations,similar"},
        )

    async def search_tv(
        self, query: str, api_key: str, year: int | None = None
    ) -> dict:
        extra: dict[str, str | int | float] = {"query": query, "page": 1}
        if year:
            extra["first_air_date_year"] = year
        return await self._get("/search/tv", api_key, extra)

    async def get_tv(self, tv_id: int, api_key: str) -> dict:
        return await self._get(
            f"/tv/{tv_id}",
            api_key,
            {
                "append_to_response": (
                    "credits,aggregate_credits,videos,images,recommendations,similar"
                )
            },
        )

    async def find_by_imdb(self, imdb_id: str, api_key: str) -> dict:
        return await self._get(
            f"/find/{imdb_id}", api_key, {"external_source": "imdb_id"}
        )


service = TmdbService()
