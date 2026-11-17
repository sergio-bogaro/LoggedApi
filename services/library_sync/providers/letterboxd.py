"""Adaptador do Letterboxd (filmes) via RSS público.

O Letterboxd não tem API pública. O feed do membro traz os ~50 diários mais
recentes, com id do TMDB, nota, data e review. Não há watchlist no feed; o
histórico completo continua vindo do export CSV.
"""

from xml.etree import ElementTree as ET

from defusedxml.ElementTree import fromstring as safe_fromstring

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
    strip_html,
    to_datetime,
    to_float,
    year_of,
)

_NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)
_MAX_SEEN = 800


class LetterboxdProvider(LibraryProvider):
    id = "letterboxd"
    label = "Letterboxd"
    media_types = [MediaTypeEnum.MOVIES]
    auth_kind = "username"
    tracks_removals = False

    def statuses(self) -> list[SourceStatus]:
        return [
            SourceStatus("watched", "Assistido", "finished", MediaStatusEnum.FINISHED),
        ]

    async def _feed(self, username: str) -> bytes:
        async with client() as http:
            response = await http.get(
                f"https://letterboxd.com/{username}/rss/",
                headers={"User-Agent": _BROWSER_UA},
            )
        if response.status_code == 404:
            raise ValueError("Username do Letterboxd não encontrado.")
        if response.status_code in (403, 429, 503):
            raise ValueError(
                "O Letterboxd bloqueou a leitura do RSS (Cloudflare). "
                "Tente novamente mais tarde."
            )
        response.raise_for_status()
        return response.content

    async def test(self, connection: LibraryConnection) -> str:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do Letterboxd.")
        await self._feed(username)
        return username

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise ValueError("Informe o username do Letterboxd.")
        raw = await self._feed(username)
        try:
            root = safe_fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError("O feed do Letterboxd não pôde ser lido.") from exc

        baseline_done = bool(connection.baseline_completed_at)
        seen = set(cursor.get("seen_guids") or []) if baseline_done else set()
        current_guids: list[str] = []
        items: dict[str, SourceItem] = {}

        for element in root.findall(".//item"):
            guid = _text(element, "guid")
            if not guid or not (
                guid.startswith("letterboxd-watch-")
                or guid.startswith("letterboxd-review-")
            ):
                continue
            current_guids.append(guid)
            if guid in seen:
                continue
            item = self._to_item(element)
            if item is not None:
                items[item.source_id] = item

        merged = list(current_guids)
        for guid in cursor.get("seen_guids") or []:
            if guid not in merged:
                merged.append(guid)
        new_cursor = {"seen_guids": merged[-_MAX_SEEN:]}
        return SourceSnapshot(
            items=list(items.values()),
            statuses=self.statuses(),
            cursor=new_cursor,
            full=False,
        )

    def _to_item(self, element: ET.Element) -> SourceItem | None:
        title = _ns_text(element, "letterboxd", "filmTitle")
        if not title:
            return None
        tmdb_id = _ns_text(element, "tmdb", "movieId")
        link = _text(element, "link")
        slug = ""
        if link:
            parts = [part for part in link.split("/") if part]
            if "film" in parts:
                index = parts.index("film")
                if index + 1 < len(parts):
                    slug = parts[index + 1]
        source_id = f"letterboxd:{tmdb_id or slug or title}"
        rating_raw = to_float(_ns_text(element, "letterboxd", "memberRating"))
        rating = round(rating_raw * 2.0, 1) if rating_raw and rating_raw > 0 else None
        watched = _ns_text(element, "letterboxd", "watchedDate")
        release_year = year_of(_ns_text(element, "letterboxd", "filmYear"))
        published = to_datetime(_text(element, "pubDate"))
        review = strip_html(_text(element, "description"))
        return SourceItem(
            source_id=source_id,
            media_type=MediaTypeEnum.MOVIES,
            title=title,
            status="watched",
            year=release_year,
            rating=rating,
            review=review,
            overview=None,
            release_date=None,
            external_id=tmdb_id or None,
            external_refs=(
                {"tmdbId": tmdb_id} if tmdb_id else {"letterboxdSlug": slug}
            ),
            source_url=link or None,
            updated_at=published,
        )


def _text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _ns_text(element: ET.Element, prefix: str, tag: str) -> str:
    child = element.find(f"{prefix}:{tag}", _NS)
    return (child.text or "").strip() if child is not None and child.text else ""
