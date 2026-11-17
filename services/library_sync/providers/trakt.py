"""Adaptador do Trakt (filmes e séries).

Usa a API oficial com OAuth device code. O `external_id` do Logged para filmes
e séries é o id do TMDB.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from config import settings
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
    to_datetime,
    to_float,
    year_of,
)

_API = "https://api.trakt.tv"
_AUTH = "https://api.trakt.tv"
_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
_PAGE = 250


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TraktProvider(LibraryProvider):
    id = "trakt"
    label = "Trakt"
    media_types = [MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES]
    auth_kind = "oauth"

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("watched", "Assistido", "finished", MediaStatusEnum.FINISHED),
            SourceStatus("watching", "Em andamento", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("watchlist", "Watchlist", "backlog", None),
        ]

    # ── OAuth ──

    @staticmethod
    def _credentials() -> tuple[str, str]:
        client_id = (settings.trakt_client_id or "").strip()
        client_secret = (settings.trakt_client_secret or "").strip()
        if not client_id:
            raise ValueError(
                "Configure TRAKT_CLIENT_ID e TRAKT_CLIENT_SECRET no servidor para "
                "conectar o Trakt."
            )
        return client_id, client_secret

    @staticmethod
    def _headers(client_id: str, token: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": client_id,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def start_device(self) -> dict:
        client_id, _ = self._credentials()
        async with client() as http:
            response = await http.post(
                f"{_AUTH}/oauth/device/code",
                json={"client_id": client_id},
                headers=self._headers(client_id),
            )
        response.raise_for_status()
        data = response.json()
        return {
            "user_code": data.get("user_code"),
            "verification_url": data.get("verification_url"),
            "expires_in": int(data.get("expires_in") or 600),
            "interval": int(data.get("interval") or 5),
            "device_code": data.get("device_code"),
        }

    async def poll_device(self, device_code: str) -> dict:
        client_id, client_secret = self._credentials()
        async with client() as http:
            response = await http.post(
                f"{_AUTH}/oauth/device/token",
                json={
                    "code": device_code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers=self._headers(client_id),
            )
        if response.status_code == 200:
            return {"status": "connected", "tokens": response.json()}
        if response.status_code == 400:
            return {"status": "pending"}
        if response.status_code == 429:
            return {"status": "slow_down"}
        if response.status_code == 410:
            return {"status": "expired"}
        if response.status_code == 418:
            return {"status": "denied"}
        return {"status": "error", "error": f"Trakt retornou {response.status_code}."}

    async def refresh(self, connection: LibraryConnection) -> None:
        auth = self.auth(connection)
        refresh_token = str(auth.get("refresh_token") or "")
        if not refresh_token:
            connection.needs_reauth = True
            raise ValueError("Conexão com o Trakt expirada. Reconecte a conta.")
        client_id, client_secret = self._credentials()
        payload = {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "redirect_uri": _REDIRECT,
        }
        async with client() as http:
            response = await http.post(
                f"{_AUTH}/oauth/token",
                json=payload,
                headers=self._headers(client_id),
            )
            if response.status_code >= 400 and payload.get("redirect_uri"):
                payload.pop("redirect_uri", None)
                response = await http.post(
                    f"{_AUTH}/oauth/token",
                    json=payload,
                    headers=self._headers(client_id),
                )
        if response.status_code >= 400:
            connection.needs_reauth = True
            raise ValueError("Não foi possível renovar a conexão com o Trakt.")
        tokens = response.json()
        auth.update(tokens)
        auth["expires_at"] = (
            _now() + timedelta(seconds=int(tokens.get("expires_in") or 604800))
        ).isoformat()
        self.set_auth(connection, auth)
        connection.needs_reauth = False

    async def _ensure_token(self, connection: LibraryConnection) -> str:
        auth = self.auth(connection)
        token = str(auth.get("access_token") or "")
        if not token:
            connection.needs_reauth = True
            raise ValueError("Conecte sua conta do Trakt em Configurações > Integrações.")
        expires_at = to_datetime(auth.get("expires_at"))
        if expires_at is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if expires_at - now < timedelta(hours=1):
                await self.refresh(connection)
                auth = self.auth(connection)
                token = str(auth.get("access_token") or "")
        return token

    # ── leitura ──

    async def _get(
        self,
        connection: LibraryConnection,
        path: str,
        params: dict | None = None,
    ) -> list | dict:
        client_id, _ = self._credentials()
        token = await self._ensure_token(connection)
        headers = self._headers(client_id, token)
        async with client() as http:
            for attempt in range(4):
                response = await http.get(
                    f"{_API}{path}", params=params, headers=headers
                )
                if response.status_code == 429 and attempt < 3:
                    retry = response.headers.get("Retry-After")
                    try:
                        delay = float(retry) if retry else 2.0
                    except ValueError:
                        delay = 2.0
                    await asyncio.sleep(min(max(delay, 0.5), 30.0))
                    continue
                if response.status_code == 401:
                    connection.needs_reauth = True
                    raise ValueError("Conexão com o Trakt expirada. Reconecte a conta.")
                response.raise_for_status()
                return response.json()
        raise ValueError("O Trakt limitou as requisições. Tente novamente mais tarde.")

    async def _paged(self, connection: LibraryConnection, path: str) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            data = await self._get(
                connection, path, {"page": page, "limit": _PAGE, "extended": "full"}
            )
            rows = data if isinstance(data, list) else []
            results.extend(rows)
            if len(rows) < _PAGE:
                break
            page += 1
        return results

    async def test(self, connection: LibraryConnection) -> str:
        token = await self._ensure_token(connection)
        client_id, _ = self._credentials()
        async with client() as http:
            response = await http.get(
                f"{_API}/users/me", headers=self._headers(client_id, token)
            )
        if response.status_code == 401:
            connection.needs_reauth = True
            raise ValueError("Conexão com o Trakt expirada. Reconecte a conta.")
        response.raise_for_status()
        data = response.json()
        return str(data.get("username") or data.get("name") or "Trakt")

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        watched_movies = await self._paged(connection, "/sync/watched/movies")
        watched_shows = await self._paged(connection, "/sync/watched/shows")
        watch_movies = await self._paged(connection, "/sync/watchlist/movies")
        watch_shows = await self._paged(connection, "/sync/watchlist/shows")
        ratings_movies = await self._paged(connection, "/users/me/ratings/movies")
        ratings_shows = await self._paged(connection, "/users/me/ratings/shows")
        progress_rows = await self._paged(connection, "/sync/progress/watched")

        rating_movies = {
            str((row.get("movie") or {}).get("ids", {}).get("trakt")): to_float(row.get("rating"))
            for row in ratings_movies
        }
        rating_shows = {
            str((row.get("show") or {}).get("ids", {}).get("trakt")): to_float(row.get("rating"))
            for row in ratings_shows
        }
        progress = {
            str((row.get("show") or {}).get("ids", {}).get("trakt")): row.get("progress") or {}
            for row in progress_rows
        }

        items: dict[str, SourceItem] = {}

        def add_movie(row: dict, status: str) -> None:
            movie = row.get("movie") or {}
            ids = movie.get("ids") or {}
            trakt_id = str(ids.get("trakt") or "")
            if not trakt_id:
                return
            source_id = f"trakt:movie:{trakt_id}"
            existing = items.get(source_id)
            if existing is not None and existing.status == "watched":
                return
            tmdb = str(ids.get("tmdb") or "")
            items[source_id] = SourceItem(
                source_id=source_id,
                media_type=MediaTypeEnum.MOVIES,
                title=str(movie.get("title") or trakt_id),
                status=status,
                year=year_of(movie.get("year")),
                rating=rating_movies.get(trakt_id),
                overview=movie.get("overview") or None,
                external_id=tmdb or None,
                external_refs={
                    **({"tmdbId": tmdb} if tmdb else {}),
                    **({"imdbId": str(ids.get("imdb"))} if ids.get("imdb") else {}),
                    "traktId": trakt_id,
                },
                source_url=f"https://trakt.tv/movies/{ids.get('slug') or trakt_id}",
                updated_at=to_datetime(row.get("last_watched_at") or row.get("listed_at")),
            )

        def add_show(row: dict, status: str) -> None:
            show = row.get("show") or {}
            ids = show.get("ids") or {}
            trakt_id = str(ids.get("trakt") or "")
            if not trakt_id:
                return
            source_id = f"trakt:show:{trakt_id}"
            existing = items.get(source_id)
            if existing is not None and existing.status in ("watched", "watching"):
                return
            tmdb = str(ids.get("tmdb") or "")
            items[source_id] = SourceItem(
                source_id=source_id,
                media_type=MediaTypeEnum.SERIES,
                title=str(show.get("title") or trakt_id),
                status=status,
                year=year_of(show.get("year")),
                rating=rating_shows.get(trakt_id),
                overview=show.get("overview") or None,
                external_id=tmdb or None,
                external_refs={
                    **({"tmdbId": tmdb} if tmdb else {}),
                    **({"imdbId": str(ids.get("imdb"))} if ids.get("imdb") else {}),
                    **({"tvdbId": str(ids.get("tvdb"))} if ids.get("tvdb") else {}),
                    "traktId": trakt_id,
                },
                source_url=f"https://trakt.tv/shows/{ids.get('slug') or trakt_id}",
                updated_at=to_datetime(row.get("last_watched_at") or row.get("listed_at")),
            )

        for row in watch_movies:
            add_movie(row, "watchlist")
        for row in watched_movies:
            add_movie(row, "watched")
        for row in watch_shows:
            add_show(row, "watchlist")
        for row in watched_shows:
            progress_info = progress.get(str((row.get("show") or {}).get("ids", {}).get("trakt")))
            if progress_info:
                aired = int(progress_info.get("aired") or 0)
                completed = int(progress_info.get("completed") or 0)
                if aired > 0 and completed >= aired:
                    add_show(row, "watched")
                elif completed > 0:
                    add_show(row, "watching")
                else:
                    add_show(row, "watched")
            else:
                add_show(row, "watched")

        return SourceSnapshot(items=list(items.values()), statuses=self.statuses())
