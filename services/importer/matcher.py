"""Resolve o `external_id` do app para cada item importado.

- Filmes/séries → TMDB (por id, por IMDb futuramente, ou busca título+ano).
- Anime/mangá → AniList (id direto, `idMal`, ou busca título).

A checagem de "já existe na biblioteca" é feita em uma única consulta por tipo.
"""

import asyncio
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.enums import MediaTypeEnum
from models.media import Media
from schemas.import_schemas import (
    ImportCandidate,
    ImportMatchRequest,
    ImportMatchRequestItem,
    ImportMatchResponse,
    ImportMatchResult,
)
from services.anilist_service import service as anilist_service
from services.tmdb_service import service as tmdb_service

_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
_MATCH_CONCURRENCY = 6
_MATCH_THRESHOLD = 0.5
_ANIME_TYPES = {MediaTypeEnum.ANIME, MediaTypeEnum.MANGA}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _to_date(value: str | None) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _fuzzy_date(value: dict | None) -> date | None:
    if not value or not value.get("year"):
        return None
    try:
        return date(int(value["year"]), int(value.get("month") or 1), int(value.get("day") or 1))
    except (TypeError, ValueError):
        return None


def _score_match(wanted_title: str, wanted_year: int | None, title: str, original_title: str, cand_year: int | None, popularity: float = 0.0) -> float:
    wanted = _normalize(wanted_title)
    norm_title = _normalize(title)
    norm_original = _normalize(original_title)
    score = 0.0

    if wanted_year and cand_year == wanted_year:
        score += 0.5
    elif wanted_year and cand_year and abs(cand_year - wanted_year) <= 1:
        score += 0.2

    if wanted and wanted == norm_title:
        score += 0.5
    elif wanted and wanted == norm_original:
        score += 0.4
    elif wanted and (wanted in norm_title or wanted in norm_original):
        score += 0.2

    score += min(float(popularity or 0) / 2000.0, 0.05)
    return round(score, 3)


class ImportMatcher:
    async def match(
        self, db: Session, data: ImportMatchRequest, tmdb_api_key: str | None
    ) -> ImportMatchResponse:
        items = data.items[:2000]
        cache = await self._prefetch_anilist(items)
        semaphore = asyncio.Semaphore(_MATCH_CONCURRENCY)

        async def resolve(item: ImportMatchRequestItem):
            async with semaphore:
                try:
                    return item, await self._candidates(item, tmdb_api_key, cache)
                except Exception:  # noqa: BLE001 — uma falha vira "não encontrado"
                    return item, []

        resolved = await asyncio.gather(*(resolve(item) for item in items))
        existing = self._existing_media(db, data.user_id, resolved)

        results = [self._build_result(item, candidates, existing) for item, candidates in resolved]
        return ImportMatchResponse(results=results)

    async def _prefetch_anilist(
        self, items: list[ImportMatchRequestItem]
    ) -> dict[str, ImportCandidate]:
        """Resolve anilistId/malId em lote (1 request por 50) para evitar o rate limit do AniList."""
        ids_by_type: dict[MediaTypeEnum, set[int]] = {}
        mal_by_type: dict[MediaTypeEnum, set[int]] = {}
        for item in items:
            if item.media_type not in _ANIME_TYPES:
                continue
            refs = item.external_refs or {}
            try:
                if refs.get("anilistId"):
                    ids_by_type.setdefault(item.media_type, set()).add(int(refs["anilistId"]))
                elif refs.get("malId"):
                    mal_by_type.setdefault(item.media_type, set()).add(int(refs["malId"]))
            except (TypeError, ValueError):
                continue

        cache: dict[str, ImportCandidate] = {}
        for media_type, ids in ids_by_type.items():
            media_list = await self._safe_batch(
                anilist_service.get_media_by_ids, list(ids), media_type
            )
            for media in media_list:
                cache[f"{media_type.value}:anilist:{media['id']}"] = self._anilist_candidate(
                    media, media_type, 1.0
                )
        for media_type, mal_ids in mal_by_type.items():
            media_list = await self._safe_batch(
                anilist_service.get_media_by_mal_ids, list(mal_ids), media_type
            )
            for media in media_list:
                mal = media.get("idMal")
                if mal is not None:
                    cache[f"{media_type.value}:mal:{mal}"] = self._anilist_candidate(
                        media, media_type, 1.0
                    )
        return cache

    @staticmethod
    async def _safe_batch(fn, ids: list[int], media_type: MediaTypeEnum) -> list[dict]:
        anilist_type = "ANIME" if media_type == MediaTypeEnum.ANIME else "MANGA"
        try:
            return await fn(ids, anilist_type)
        except ValueError:
            return []

    async def search(
        self, media_type: MediaTypeEnum, query: str, tmdb_api_key: str | None
    ) -> list[ImportCandidate]:
        if media_type in _ANIME_TYPES:
            return await self._anilist_search(media_type, query)
        if media_type in (MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES):
            if not tmdb_api_key:
                raise ValueError("Nenhuma chave do TMDB configurada.")
            return await self._tmdb_search_candidates(media_type, query, None, tmdb_api_key)
        return []

    # ── candidatos por tipo ──

    async def _candidates(
        self,
        item: ImportMatchRequestItem,
        tmdb_api_key: str | None,
        cache: dict[str, ImportCandidate],
    ) -> list[ImportCandidate]:
        if item.media_type in _ANIME_TYPES:
            return await self._anilist_candidates(item, cache)
        if item.media_type in (MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES):
            if not tmdb_api_key:
                return []
            return await self._tmdb_search_candidates(
                item.media_type, item.title, item.year, tmdb_api_key, item.external_refs
            )
        return []

    async def _anilist_candidates(
        self, item: ImportMatchRequestItem, cache: dict[str, ImportCandidate]
    ) -> list[ImportCandidate]:
        media_type = item.media_type
        refs = item.external_refs or {}

        if refs.get("anilistId"):
            cached = cache.get(f"{media_type.value}:anilist:{refs['anilistId']}")
            if cached:
                return [cached]

        if refs.get("malId"):
            cached = cache.get(f"{media_type.value}:mal:{refs['malId']}")
            if cached:
                return [cached]

        return await self._anilist_search(media_type, item.title, item.year)

    async def _anilist_search(
        self, media_type: MediaTypeEnum, query: str, year: int | None = None
    ) -> list[ImportCandidate]:
        anilist_type = "ANIME" if media_type == MediaTypeEnum.ANIME else "MANGA"
        try:
            results = await anilist_service.search_media(query, anilist_type)
        except ValueError:
            return []
        candidates = [self._anilist_candidate(media, media_type, None, query, year) for media in results]
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates[:5]

    async def _tmdb_search_candidates(
        self,
        media_type: MediaTypeEnum,
        title: str,
        year: int | None,
        api_key: str,
        refs: dict[str, str] | None = None,
    ) -> list[ImportCandidate]:
        refs = refs or {}
        is_tv = media_type == MediaTypeEnum.SERIES
        search_fn = tmdb_service.search_tv if is_tv else tmdb_service.search_movies

        if refs.get("tmdbId"):
            detail_fn = tmdb_service.get_tv if is_tv else tmdb_service.get_movie
            try:
                movie = await detail_fn(int(refs["tmdbId"]), api_key)
            except ValueError:
                movie = None
            if movie:
                return [self._tmdb_candidate(movie, media_type, 1.0, title, year)]

        results = await self._run_tmdb_search(search_fn, title, api_key, year)
        if not results and year:
            results = await self._run_tmdb_search(search_fn, title, api_key, None)

        candidates = [self._tmdb_candidate(movie, media_type, None, title, year) for movie in results]
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates[:5]

    @staticmethod
    async def _run_tmdb_search(search_fn, query: str, api_key: str, year: int | None) -> list[dict]:
        try:
            data = await search_fn(query, api_key, year)
        except ValueError:
            return []
        return list(data.get("results") or [])

    # ── conversão para candidatos ──

    @staticmethod
    def _tmdb_candidate(
        movie: dict, media_type: MediaTypeEnum, score: float | None, wanted_title: str, wanted_year: int | None
    ) -> ImportCandidate:
        raw_date = movie.get("release_date") or movie.get("first_air_date")
        release_date = _to_date(raw_date)
        poster = movie.get("poster_path")
        title = movie.get("title") or movie.get("name") or wanted_title
        original = movie.get("original_title") or movie.get("original_name") or ""
        cand_year = release_date.year if release_date else None
        resolved_score = score if score is not None else _score_match(
            wanted_title, wanted_year, title, original, cand_year, movie.get("popularity")
        )
        return ImportCandidate(
            provider="tmdb",
            external_id=str(movie["id"]),
            media_type=media_type,
            title=title,
            year=cand_year,
            cover_url=f"{_TMDB_IMAGE_BASE}{poster}" if poster else None,
            overview=movie.get("overview") or None,
            release_date=release_date,
            score=resolved_score,
        )

    @staticmethod
    def _anilist_candidate(
        media: dict,
        media_type: MediaTypeEnum,
        score: float | None,
        wanted_title: str | None = None,
        wanted_year: int | None = None,
    ) -> ImportCandidate:
        title_block = media.get("title") or {}
        title = title_block.get("english") or title_block.get("romaji") or title_block.get("native") or ""
        original = title_block.get("romaji") or ""
        start = _fuzzy_date(media.get("startDate"))
        cand_year = start.year if start else None
        cover = media.get("coverImage") or {}
        resolved_score = score if score is not None else _score_match(
            wanted_title or "", wanted_year, title, original, cand_year, media.get("popularity")
        )
        return ImportCandidate(
            provider="anilist",
            external_id=str(media["id"]),
            media_type=media_type,
            title=title,
            year=cand_year,
            cover_url=cover.get("large") or cover.get("medium") or cover.get("extraLarge"),
            overview=media.get("description") or None,
            release_date=start,
            score=resolved_score,
        )

    # ── duplicados e resultado ──

    @staticmethod
    def _existing_media(db: Session, user_id: int, resolved) -> dict[str, int]:
        pairs = {
            (item.media_type, candidates[0].external_id)
            for item, candidates in resolved
            if candidates
        }
        existing: dict[str, int] = {}
        if not pairs:
            return existing

        for media_type in {media_type for media_type, _ in pairs}:
            ids = [external_id for mt, external_id in pairs if mt == media_type]
            query = select(Media).where(
                Media.user_id == user_id,
                Media.type == media_type,
                Media.external_id.in_(ids),
            )
            for media in db.execute(query).scalars().all():
                existing[f"{media.type.value}:{media.external_id}"] = media.id
        return existing

    @staticmethod
    def _build_result(
        item: ImportMatchRequestItem,
        candidates: list[ImportCandidate],
        existing: dict[str, int],
    ) -> ImportMatchResult:
        if not candidates:
            return ImportMatchResult(key=item.key, status="not_found")

        best = candidates[0]
        status = "matched"
        if best.score < _MATCH_THRESHOLD:
            status = "ambiguous"
        elif (
            len(candidates) > 1
            and candidates[1].score >= _MATCH_THRESHOLD
            and candidates[1].score >= best.score - 0.05
        ):
            status = "ambiguous"

        media_id = existing.get(f"{item.media_type.value}:{best.external_id}")
        if media_id is not None:
            status = "already_in_library"

        return ImportMatchResult(
            key=item.key,
            status=status,
            match=best,
            candidates=candidates,
            existing_media_id=media_id,
        )


matcher = ImportMatcher()
