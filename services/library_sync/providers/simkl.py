"""Adaptador do Simkl (filmes, séries e anime).

Usa OAuth 2.0 device flow. O Simkl tem exatamente os status que o Logged usa
(watching, plantowatch, hold, dropped, completed), então é o mapeamento mais
direto entre os provedores de filmes/séries.
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
    to_date,
    to_datetime,
    to_float,
    year_of,
)
from domain_errors import DomainError

_API = "https://api.simkl.com"
_APP_NAME = "logged"
_APP_VERSION = "1.0"
_SCOPE = "media:read"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SimklProvider(LibraryProvider):
    id = "simkl"
    label = "Simkl"
    media_types = [MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES, MediaTypeEnum.ANIME]
    auth_kind = "oauth"
    trusted_external_id = True
    creates_logs = True

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("watching", "Assistindo", "in_progress", MediaStatusEnum.IN_PROGRESS),
            SourceStatus("completed", "Completo", "finished", MediaStatusEnum.FINISHED),
            SourceStatus("hold", "Em pausa", "on_hold", MediaStatusEnum.ON_HOLD),
            SourceStatus("dropped", "Dropado", "dropped", MediaStatusEnum.DROPPED),
            SourceStatus("plantowatch", "Planejado", "backlog", None),
        ]

    # ── OAuth ──

    @staticmethod
    def _client() -> tuple[str, str]:
        client_id = (settings.simkl_client_id or "").strip()
        client_secret = (settings.simkl_client_secret or "").strip()
        if not client_id:
            raise DomainError(
                "Configure SIMKL_CLIENT_ID (e SIMKL_CLIENT_SECRET, se for app de "
                "servidor) para conectar o Simkl."
            )
        return client_id, client_secret

    @staticmethod
    def _params(client_id: str) -> dict[str, str]:
        return {
            "client_id": client_id,
            "app-name": _APP_NAME,
            "app-version": _APP_VERSION,
        }

    async def start_device(self) -> dict:
        client_id, _ = self._client()
        async with client() as http:
            response = await http.post(
                f"{_API}/oauth2/device",
                data={"client_id": client_id, "scope": _SCOPE},
            )
        response.raise_for_status()
        data = response.json()
        return {
            "user_code": data.get("user_code"),
            "verification_url": data.get("verification_uri_complete")
            or data.get("verification_uri"),
            "expires_in": int(data.get("expires_in") or 900),
            "interval": int(data.get("interval") or 5),
            "device_code": data.get("device_code"),
        }

    async def poll_device(self, device_code: str) -> dict:
        client_id, client_secret = self._client()
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device_code,
        }
        if client_secret:
            data["client_secret"] = client_secret
        async with client() as http:
            response = await http.post(f"{_API}/oauth2/token", data=data)
        if response.status_code == 200:
            return {"status": "connected", "tokens": response.json()}
        try:
            error = (response.json() or {}).get("error")
        except ValueError:
            error = None
        if error == "authorization_pending":
            return {"status": "pending"}
        if error == "slow_down":
            return {"status": "slow_down"}
        if error == "expired_token":
            return {"status": "expired"}
        return {"status": "error", "error": error or f"Simkl retornou {response.status_code}."}

    async def refresh(self, connection: LibraryConnection) -> None:
        auth = self.auth(connection)
        refresh_token = str(auth.get("refresh_token") or "")
        if not refresh_token:
            connection.needs_reauth = True
            raise DomainError("Conexão com o Simkl expirada. Reconecte a conta.")
        client_id, client_secret = self._client()
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
        if client_secret:
            data["client_secret"] = client_secret
        async with client() as http:
            response = await http.post(f"{_API}/oauth2/token", data=data)
        if response.status_code >= 400:
            connection.needs_reauth = True
            raise DomainError("Não foi possível renovar a conexão com o Simkl.")
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
            raise DomainError("Conecte sua conta do Simkl em Configurações > Integrações.")
        expires_at = to_datetime(auth.get("expires_at"))
        if expires_at is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if expires_at - now < timedelta(hours=1):
                await self.refresh(connection)
                auth = self.auth(connection)
                token = str(auth.get("access_token") or "")
        return token

    async def _request(
        self,
        connection: LibraryConnection,
        path: str,
        params: dict | None = None,
        method: str = "GET",
    ) -> dict | list:
        client_id, _ = self._client()
        token = await self._ensure_token(connection)
        query = self._params(client_id)
        if params:
            query.update(params)
        headers = {"Authorization": f"Bearer {token}"}
        async with client() as http:
            for attempt in range(4):
                response = await http.request(
                    method, f"{_API}{path}", params=query, headers=headers
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
                    raise DomainError("Conexão com o Simkl expirada. Reconecte a conta.")
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
        raise DomainError("O Simkl limitou as requisições. Tente novamente mais tarde.")

    async def test(self, connection: LibraryConnection) -> str:
        data = await self._request(connection, "/users/settings")
        auth = self.auth(connection)
        user = data.get("user") if isinstance(data, dict) else None
        name = ""
        if isinstance(user, dict):
            name = str(user.get("name") or user.get("username") or "")
        if name:
            auth["account"] = name
            self.set_auth(connection, auth)
        return name or str(auth.get("account") or "Simkl")

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        activities = await self._request(connection, "/sync/activities")
        previous = cursor.get("activities") if isinstance(cursor, dict) else None
        if isinstance(activities, dict) and isinstance(previous, dict):
            if activities.get("all") and activities.get("all") == previous.get("all"):
                return SourceSnapshot(skipped=True, statuses=self.statuses())

        removed_changed = True
        date_from = None
        if isinstance(previous, dict):
            date_from = previous.get("all")
            removed_changed = (
                (activities.get("tv_shows") or {}).get("removed_from_list")
                or (activities.get("anime") or {}).get("removed_from_list")
                or (activities.get("movies") or {}).get("removed_from_list")
            ) != (
                (previous.get("tv_shows") or {}).get("removed_from_list")
                or (previous.get("anime") or {}).get("removed_from_list")
                or (previous.get("movies") or {}).get("removed_from_list")
            )
        full = bool(removed_changed) or not date_from
        params = {"extended": "full"}
        if not full and date_from:
            params["date_from"] = date_from
        data = await self._request(connection, "/sync/all-items", params)
        if not isinstance(data, dict):
            data = {}

        items: dict[str, SourceItem] = {}
        for key, media_type, kind in (
            ("movies", MediaTypeEnum.MOVIES, "movie"),
            ("shows", MediaTypeEnum.SERIES, "show"),
            ("anime", MediaTypeEnum.ANIME, "anime"),
        ):
            for row in data.get(key) or []:
                self._add(items, row, media_type, kind)

        return SourceSnapshot(
            items=list(items.values()),
            statuses=self.statuses(),
            cursor={"activities": activities if isinstance(activities, dict) else {}},
            full=full,
        )

    def _add(
        self,
        items: dict[str, SourceItem],
        row: dict,
        media_type: MediaTypeEnum,
        kind: str,
    ) -> None:
        obj = row.get(kind) or row.get("movie") or row.get("show") or row.get("anime") or {}
        ids = obj.get("ids") or row.get("ids") or {}
        simkl_id = ids.get("simkl") or row.get("simkl_id")
        if not simkl_id:
            return
        source_id = f"simkl:{kind}:{simkl_id}"
        status = str(row.get("status") or obj.get("status") or "plantowatch")
        rating = to_float(row.get("user_rating"))
        if rating is not None and rating <= 0:
            rating = None
        # O Logged usa AniList para anime e TMDB para filmes/séries. Os demais
        # ids continuam em external_refs para o matcher resolver.
        if media_type == MediaTypeEnum.ANIME:
            external_id = ids.get("anilist")
        else:
            external_id = ids.get("tmdb")
        external_id = str(external_id) if external_id else None
        title = obj.get("title") or obj.get("name") or row.get("title") or str(simkl_id)
        year = year_of(obj.get("year") or obj.get("released") or obj.get("release_date"))
        items[source_id] = SourceItem(
            source_id=source_id,
            media_type=media_type,
            title=str(title),
            status=status,
            year=year,
            rating=rating,
            review=None,
            overview=obj.get("overview") or obj.get("description") or None,
            external_id=external_id,
            external_refs={
                **({"tmdbId": str(ids["tmdb"])} if ids.get("tmdb") else {}),
                **({"imdbId": str(ids["imdb"])} if ids.get("imdb") else {}),
                **({"anilistId": str(ids["anilist"])} if ids.get("anilist") else {}),
                **({"malId": str(ids["mal"])} if ids.get("mal") else {}),
                "simklId": str(simkl_id),
            },
            source_url=f"https://simkl.com/{'movies' if media_type == MediaTypeEnum.MOVIES else 'tv' if media_type == MediaTypeEnum.SERIES else 'anime'}/{simkl_id}",
            updated_at=to_datetime(row.get("last_watched_at")),
            watched_at=to_date(row.get("last_watched_at")),
        )
