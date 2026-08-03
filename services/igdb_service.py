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
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=settings.igdb_request_timeout, follow_redirects=True
            )
        return self._client

    async def _ensure_token(self) -> str:
        """Get a valid Twitch OAuth token, refreshing if needed."""
        now = datetime.now()
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token

        async with self._lock:
            # Double-check after acquiring lock
            if self._token and self._token_expires_at and datetime.now() < self._token_expires_at:
                return self._token

            if not settings.igdb_client_id or not settings.igdb_client_secret:
                raise ValueError(
                    "IGDB_CLIENT_ID and IGDB_CLIENT_SECRET must be set in .env"
                )

            client = self._get_client()
            resp = await client.post(
                settings.igdb_oauth_url,
                params={
                    "client_id": settings.igdb_client_id,
                    "client_secret": settings.igdb_client_secret,
                    "grant_type": "client_credentials",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            access_token: str = data["access_token"]
            self._token = access_token
            # Refresh 60 seconds before expiry
            self._token_expires_at = now + timedelta(seconds=data["expires_in"] - 60)
            return access_token

    async def _post(self, path: str, body: str) -> list[dict]:
        """Make an authenticated POST request to IGDB."""
        token = await self._ensure_token()
        client = self._get_client()

        resp = await client.post(
            f"{settings.igdb_base_url}{path}",
            content=body,
            headers={
                "Client-ID": settings.igdb_client_id or "",
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "text/plain",
            },
        )
        resp.raise_for_status()
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

    async def search_games(self, query: str, limit: int = 20) -> list[IgdbSearchItem]:
        """Search IGDB for games by title."""
        # version_parent=null filters out editions/versions
        body = (
            f'search "{query}"; '
            f"fields id,name,cover.image_id,first_release_date,summary; "
            f"where version_parent = null; "
            f"limit {limit};"
        )

        results = await self._post("/games", body)

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

    async def get_game(self, game_id: int) -> IgdbGame:
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

        results = await self._post("/games", body)
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
