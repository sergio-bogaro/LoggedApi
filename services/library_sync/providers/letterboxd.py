"""Adaptador do Letterboxd (filmes) via RSS público.

O Letterboxd não tem API pública. O feed do membro traz os ~50 diários mais
recentes, com id do TMDB, nota, data e review. Não há watchlist no feed; o
histórico completo continua vindo do export CSV.
"""

from xml.etree import ElementTree as ET

import re

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
    to_date,
    to_datetime,
    to_float,
    year_of,
)
from domain_errors import DomainError

_NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)
# O <description> de um item "watched" sem review é só essa frase. Ela não é
# review e não deve poluir `Media.review` nem os logs.
_WATCHED_ONLY_RE = re.compile(r"^Watched on [^\n]+?\.\s*")
class LetterboxdProvider(LibraryProvider):
    id = "letterboxd"
    label = "Letterboxd"
    media_types = [MediaTypeEnum.MOVIES]
    auth_kind = "username"
    tracks_removals = False
    trusted_external_id = True
    sync_partial = True
    status_mapping = False
    creates_logs = True

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
            raise DomainError("Username do Letterboxd não encontrado.")
        if response.status_code in (403, 429, 503):
            raise DomainError(
                "O Letterboxd bloqueou a leitura do RSS (Cloudflare). "
                "Tente novamente mais tarde."
            )
        response.raise_for_status()
        return response.content

    async def test(self, connection: LibraryConnection) -> str:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise DomainError("Informe o username do Letterboxd.")
        await self._feed(username)
        return username

    async def fetch(
        self, connection: LibraryConnection, cursor: dict
    ) -> SourceSnapshot:
        username = str(self.auth(connection).get("username") or "").strip()
        if not username:
            raise DomainError("Informe o username do Letterboxd.")
        raw = await self._feed(username)
        try:
            root = safe_fromstring(raw)
        except ET.ParseError as exc:
            raise DomainError("O feed do Letterboxd não pôde ser lido.") from exc

        items: dict[str, SourceItem] = {}

        # O feed é sempre reprocessado por inteiro. Assim um título que estava
        # pendente pode ser vinculado/criado quando a metadata estiver
        # disponível, em vez de desaparecer no cursor de "já vistos".
        for element in root.findall(".//item"):
            guid = _text(element, "guid")
            if not guid or not (
                guid.startswith("letterboxd-watch-")
                or guid.startswith("letterboxd-review-")
            ):
                continue
            item = self._to_item(element)
            if item is not None:
                items[item.source_id] = item

        return SourceSnapshot(
            items=list(items.values()),
            statuses=self.statuses(),
            cursor={},
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
        watched = to_date(_ns_text(element, "letterboxd", "watchedDate"))
        release_year = year_of(_ns_text(element, "letterboxd", "filmYear"))
        published = to_datetime(_text(element, "pubDate"))
        review_text = strip_html(_text(element, "description")) or ""
        # Remove a frase "Watched on ..." que o feed usa em itens sem review.
        review = _WATCHED_ONLY_RE.sub("", review_text, count=1).strip() or None
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
            watched_at=watched,
        )


def _text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _ns_text(element: ET.Element, prefix: str, tag: str) -> str:
    child = element.find(f"{prefix}:{tag}", _NS)
    return (child.text or "").strip() if child is not None and child.text else ""
