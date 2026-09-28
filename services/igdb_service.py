import asyncio
from datetime import datetime, timedelta

import httpx

from config import settings
from schemas.igdb import (
    IgdbArtwork,
    IgdbGame,
    IgdbGenre,
    IgdbInvolvedCompany,
    IgdbPlatform,
    IgdbScreenshot,
    IgdbSearchItem,
    IgdbVideo,
    IgdbWebsite,
)


class IgdbService:
    """Service for IGDB API with Twitch OAuth2 token management."""

    def __init__(self) -> None:
        # Caches por client_id — cada usuário pode ter as próprias credenciais.
        self._tokens: dict[str, tuple[str, datetime]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=settings.igdb_request_timeout, follow_redirects=True
            )
        return self._client

    async def _ensure_token(self, client_id: str, client_secret: str) -> str:
        """Get a valid Twitch OAuth token for the given credentials, refreshing if needed."""
        now = datetime.now()
        cached = self._tokens.get(client_id)
        if cached and now < cached[1]:
            return cached[0]

        lock = self._locks.setdefault(client_id, asyncio.Lock())
        async with lock:
            # Double-check after acquiring the lock
            cached = self._tokens.get(client_id)
            if cached and datetime.now() < cached[1]:
                return cached[0]

            client = self._get_client()
            try:
                resp = await client.post(
                    settings.igdb_oauth_url,
                    params={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "client_credentials",
                    },
                )
            except httpx.HTTPError as exc:
                raise ValueError(f"Falha ao contatar o Twitch/IGDB: {exc}") from exc
            if resp.status_code in (400, 401, 403):
                raise ValueError(
                    "O IGDB/Twitch rejeitou as credenciais. Verifique o Client ID e o Secret."
                )
            resp.raise_for_status()
            data = resp.json()

            access_token: str = data["access_token"]
            # Refresh 60 seconds before expiry
            self._tokens[client_id] = (
                access_token,
                now + timedelta(seconds=data["expires_in"] - 60),
            )
            return access_token

    async def _post(
        self, path: str, body: str, client_id: str, client_secret: str
    ) -> list[dict]:
        """Make an authenticated POST request to IGDB."""
        token = await self._ensure_token(client_id, client_secret)
        client = self._get_client()

        try:
            resp = await client.post(
                f"{settings.igdb_base_url}{path}",
                content=body,
                headers={
                    "Client-ID": client_id,
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "text/plain",
                },
            )
        except httpx.HTTPError as exc:
            raise ValueError(f"Falha ao contatar o IGDB: {exc}") from exc

        if resp.status_code >= 400:
            raise ValueError(f"O IGDB retornou um erro ({resp.status_code}).")
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"IGDB returned unexpected response: {data}")
        return data

    @staticmethod
    def _igdb_image_url(image_id: str | None, size: str = "t_cover_big") -> str:
        """Construct an IGDB image URL from an image_id."""
        if not image_id:
            return ""
        return f"https://images.igdb.com/igdb/image/upload/{size}/{image_id}.jpg"

    @staticmethod
    def _unix_to_iso_date(unix_ts: int | float | None) -> str | None:
        """Convert a Unix timestamp (seconds) to ISO date string YYYY-MM-DD."""
        if not unix_ts:
            return None
        return datetime.fromtimestamp(unix_ts).date().isoformat()

    async def search_games(
        self, query: str, client_id: str, client_secret: str, limit: int = 20
    ) -> list[IgdbSearchItem]:
        """Search IGDB for games by title."""
        # version_parent=null filters out editions/versions
        body = (
            f'search "{query}"; '
            f"fields id,name,cover.image_id,first_release_date,summary; "
            f"where version_parent = null; "
            f"limit {limit};"
        )

        results = await self._post("/games", body, client_id, client_secret)

        items: list[IgdbSearchItem] = []
        for g in results:
            cover = g.get("cover")
            cover_url = ""
            if isinstance(cover, dict):
                img_id = str(cover.get("image_id", "")) if cover.get("image_id") else ""
                cover_url = self._igdb_image_url(img_id, "t_cover_big")

            items.append(
                IgdbSearchItem(
                    id=g["id"],
                    name=g.get("name", ""),
                    cover_url=cover_url,
                    first_release_date=self._unix_to_iso_date(g.get("first_release_date")),
                    summary=g.get("summary"),
                )
            )
        return items

    async def get_game(
        self, game_id: int, client_id: str, client_secret: str
    ) -> IgdbGame:
        """Get full game details from IGDB."""
        body = (
            "fields id,slug,name,summary,storyline,first_release_date,"
            "rating,total_rating_count,"
            "cover.image_id,"
            "platforms.id,platforms.name,platforms.abbreviation,"
            "genres.id,genres.name,"
            "involved_companies.company.name,involved_companies.developer,involved_companies.publisher,"
            "involved_companies.company.id,"
            "screenshots.id,screenshots.image_id,"
            "artworks.id,artworks.image_id,"
            "videos.id,videos.name,videos.video_id,"
            "websites.type,websites.url; "
            f"where id = {game_id};"
        )

        results = await self._post("/games", body, client_id, client_secret)
        if not results:
            raise ValueError(f"Game {game_id} not found in IGDB")

        g = results[0]

        # Cover
        cover = g.get("cover")
        cover_url = ""
        if isinstance(cover, dict):
            cover_url = self._igdb_image_url(cover.get("image_id", ""), "t_cover_big")

        # Platforms
        platforms = []
        for p in g.get("platforms", []):
            platforms.append(
                IgdbPlatform(
                    id=p["id"],
                    name=p.get("name", ""),
                    abbreviation=p.get("abbreviation"),
                )
            )

        # Genres
        genres = []
        for gen in g.get("genres", []):
            genres.append(IgdbGenre(id=gen["id"], name=gen.get("name", "")))

        # Involved companies
        involved_companies = []
        for ic in g.get("involved_companies", []):
            company = ic.get("company")
            if isinstance(company, dict):
                involved_companies.append(
                    IgdbInvolvedCompany(
                        company_id=company["id"],
                        company_name=company.get("name", ""),
                        developer=ic.get("developer", False),
                        publisher=ic.get("publisher", False),
                    )
                )

        # Screenshots
        screenshots = []
        for s in g.get("screenshots", []):
            image_id = s.get("image_id", "")
            screenshots.append(
                IgdbScreenshot(
                    id=s["id"],
                    url=self._igdb_image_url(image_id, "t_screenshot_med"),
                )
            )

        # Artworks
        artworks = []
        for a in g.get("artworks", []):
            image_id = a.get("image_id", "")
            artworks.append(
                IgdbArtwork(
                    id=a["id"],
                    url=self._igdb_image_url(image_id, "t_screenshot_big"),
                )
            )

        # Videos
        videos = []
        for v in g.get("videos", []):
            videos.append(
                IgdbVideo(
                    id=v["id"],
                    name=v.get("name", ""),
                    video_id=v.get("video_id", ""),
                )
            )

        # Websites
        websites = []
        for w in g.get("websites", []):
            websites.append(IgdbWebsite(type=w.get("type", 0), url=w.get("url", "")))

        # Rating: IGDB is 0-100, normalize to 0-5
        rating = g.get("rating")
        if rating is not None:
            rating = round(rating / 20, 1)

        return IgdbGame(
            id=g["id"],
            slug=g.get("slug", ""),
            name=g.get("name", ""),
            summary=g.get("summary"),
            storyline=g.get("storyline"),
            first_release_date=self._unix_to_iso_date(g.get("first_release_date")),
            rating=rating,
            total_rating_count=g.get("total_rating_count"),
            cover_url=cover_url,
            platforms=platforms,
            genres=genres,
            involved_companies=involved_companies,
            screenshots=screenshots,
            artworks=artworks,
            videos=videos,
            websites=websites,
        )
