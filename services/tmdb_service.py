import httpx

from config import settings


class TmdbService:
    """Proxy para a API do TMDB usando a chave resolvida (usuário ou instância)."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=settings.tmdb_request_timeout, follow_redirects=True
            )
        return self._client

    async def _get(self, path: str, api_key: str, extra: dict | None = None) -> dict:
        params: dict[str, object] = {"api_key": api_key, "language": "en-US"}
        if extra:
            params.update(extra)

        try:
            resp = await self._get_client().get(
                f"{settings.tmdb_base_url}{path}", params=params
            )
        except httpx.HTTPError as exc:
            raise ValueError(f"Falha ao contatar o TMDB: {exc}") from exc

        if resp.status_code == 401:
            raise ValueError(
                "O TMDB rejeitou a chave de API (401). Verifique a chave configurada."
            )
        if resp.status_code == 429:
            raise ValueError(
                "O TMDB limitou a taxa de requisições (429). Tente novamente em instantes."
            )
        if resp.status_code >= 400:
            raise ValueError(f"O TMDB retornou um erro ({resp.status_code}).")

        return resp.json()

    async def search_movies(
        self, query: str, api_key: str, year: int | None = None
    ) -> dict:
        extra: dict[str, object] = {"query": query, "page": 1}
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
        extra: dict[str, object] = {"query": query, "page": 1}
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


service = TmdbService()
