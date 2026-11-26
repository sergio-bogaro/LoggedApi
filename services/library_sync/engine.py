"""Motor genérico de sincronização de bibliotecas externas.

A semântica é a mesma do Suwayomi:
- o status de origem define uma ação no Logged (status, backlog ou ignorar);
- edições manuais são preservadas enquanto o status de origem não muda;
- só o backlog criado pelo sync é removido pelo sync;
- vínculos pendentes são retentados (título exato sempre, metadata 1x/24h).
"""

import asyncio
import json
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, lazyload

from database import engine as db_engine
from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import (
    LibraryConnection,
    LibraryStatusMapping,
    LibrarySyncLink,
)
from services.http_client import close_loop_clients
from models.media import Media
from models.media_list_item import MediaListItem
from models.media_log import MediaLog
from schemas.import_schemas import ImportCandidate, ImportMatchRequest, ImportMatchRequestItem
from services.credentials_service import resolve_igdb_credentials, resolve_tmdb_key
from services.importer.matcher import matcher
from services.importer.text import normalize_title
from services.library_sync.providers.base import (
    LibraryProvider,
    SourceItem,
    SourceSnapshot,
)
from services.library_sync.registry import get_provider
from services.library_sync.suggestions import (
    load_suggestions,
    pending_count,
    stage_suggestion,
)
from domain_errors import DomainError

_STATUS_ACTIONS = {status.value for status in MediaStatusEnum}
_PENDING_INTERVAL = timedelta(hours=24)
_MAX_LIST = 50
_SYNC_LOCK = threading.Lock()
# O sync faz chamadas HTTP dentro do processamento de cada item. Commitar a
# cada item mantém a transação de escrita curta (SQLite tem um único writer).
_COMMIT_EVERY = 1

ProgressCallback = Callable[[dict], None]


def _report(progress: ProgressCallback | None, **fields) -> None:
    if progress is None:
        return
    try:
        progress(fields)
    except Exception:  # noqa: BLE001 — progresso nunca derruba o sync
        pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _interval(updates_per_day: int) -> timedelta:
    per_day = min(max(int(updates_per_day or 1), 1), 24)
    return timedelta(hours=24 / per_day)


def _action_for_status(
    mapping: LibraryStatusMapping | None,
    status_def_action: str | None,
) -> tuple[str, MediaStatusEnum | None]:
    if mapping is not None:
        action = mapping.action
        status = mapping.status
    else:
        action = status_def_action or "ignore"
        status = None
    if action in _STATUS_ACTIONS:
        return action, MediaStatusEnum(action)
    if action == "backlog":
        return "backlog", None
    return "ignore", None


def _get_media(db: Session, media_id: int | None) -> Media | None:
    if media_id is None:
        return None
    return db.execute(
        select(Media)
        .where(Media.id == media_id)
        .options(lazyload(Media.logs), lazyload(Media.tags))
    ).scalar_one_or_none()


def _find_backlog(db: Session, user_id: int, media_id: int) -> MediaListItem | None:
    return db.execute(
        select(MediaListItem).where(
            MediaListItem.user_id == user_id,
            MediaListItem.media_id == media_id,
            MediaListItem.list_type == "backlog",
        )
    ).scalar_one_or_none()


def _add_backlog(
    db: Session, user_id: int, media_id: int, media_type: MediaTypeEnum
) -> bool:
    if _find_backlog(db, user_id, media_id) is not None:
        return False
    db.add(
        MediaListItem(
            user_id=user_id,
            media_type=media_type,
            media_id=media_id,
            list_type="backlog",
            date_log=utcnow(),
        )
    )
    return True


def _remove_backlog(db: Session, user_id: int, media_id: int) -> bool:
    item = _find_backlog(db, user_id, media_id)
    if item is None:
        return False
    db.delete(item)
    return True


def _apply_notes(media: Media, item: SourceItem) -> bool:
    changed = False
    if media.rating is None and item.rating is not None:
        media.rating = item.rating
        changed = True
    if not media.review and item.review:
        media.review = item.review
        changed = True
    if not media.description and item.overview:
        media.description = item.overview
        changed = True
    if not media.cover_url and item.cover_url:
        media.cover_url = item.cover_url
        changed = True
    return changed


def _has_media_log(
    db: Session, media_id: int, day, status: MediaStatusEnum
) -> bool:
    return (
        db.execute(
            select(MediaLog.id).where(
                MediaLog.media_id == media_id,
                MediaLog.date == day,
                MediaLog.status == status,
            )
        ).scalar_one_or_none()
        is not None
    )


def _add_media_log(
    db: Session,
    connection: LibraryConnection,
    item: SourceItem,
    media: Media,
    status: MediaStatusEnum,
    summary: dict,
) -> None:
    if item.watched_at is None or media.id is None:
        return
    if _has_media_log(db, media.id, item.watched_at, status):
        return
    start = datetime.combine(item.watched_at, time.min)
    ended = status in (MediaStatusEnum.FINISHED, MediaStatusEnum.DROPPED)
    db.add(
        MediaLog(
            user_id=connection.user_id,
            media_id=media.id,
            date=item.watched_at,
            status=status,
            rating=item.rating,
            review=item.review,
            start_date=start,
            end_date=start if ended else None,
        )
    )
    summary["logsCreated"] += 1


class LibrarySyncEngine:
    async def sync(
        self,
        db: Session,
        connection: LibraryConnection,
        run_at: datetime | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict:
        run_at = run_at or utcnow()
        provider = get_provider(connection.provider)
        summary = self._empty_summary()
        summary["provider"] = connection.provider
        summary["startedAt"] = run_at.isoformat()
        connection.last_sync_status = "syncing"
        db.commit()
        _report(
            progress,
            phase="fetching",
            label=provider.label,
            provider=connection.provider,
            done=0,
            total=0,
        )

        mappings = self._ensure_mappings(db, connection, provider)
        links = {
            link.source_id: link
            for link in db.execute(
                select(LibrarySyncLink).where(
                    LibrarySyncLink.user_id == connection.user_id,
                    LibrarySyncLink.provider == connection.provider,
                )
            )
            .scalars()
            .all()
        }
        media_by_id, media_by_external, media_by_title = self._load_media(
            db, connection.user_id
        )
        suggestions = load_suggestions(db, connection.user_id, connection.provider)
        cursor = provider.cursor(connection)
        # Cursor antigo do baseline: não bloqueia mais nenhum título.
        cursor.pop("baseline_missing", None)
        snapshot = await provider.fetch(connection, cursor)
        _report(
            progress,
            phase="syncing",
            label=provider.label,
            provider=connection.provider,
            done=0,
            total=len(snapshot.items),
        )

        if snapshot.skipped:
            summary["skipped"] = True
            summary["pendingReview"] = pending_count(
                db, connection.user_id, connection.provider
            )
            summary["finishedAt"] = utcnow().isoformat()
            connection.last_sync_at = utcnow()
            connection.last_sync_status = "ok"
            connection.last_sync_summary = _dumps(summary)
            connection.next_sync_at = utcnow() + _interval(connection.updates_per_day)
            connection.needs_reauth = False
            db.commit()
            return summary

        summary["library"] = len(snapshot.items)
        first_sync = connection.baseline_completed_at is None
        seen: set[str] = set()

        for index, item in enumerate(snapshot.items, start=1):
            seen.add(item.source_id)
            summary["processed"] += 1
            try:
                # Savepoint por item: uma falha desfaz só este item, sem deixar
                # a transação envenenada para os próximos.
                with db.begin_nested():
                    await self._process_item(
                        db,
                        connection,
                        provider,
                        snapshot,
                        item,
                        mappings,
                        links,
                        media_by_id,
                        media_by_external,
                        media_by_title,
                        suggestions,
                        summary,
                        run_at,
                    )
            except Exception as exc:  # noqa: BLE001 — um item não derruba o sync
                summary["errors"].append(f"{item.title}: {exc}")
            if index % _COMMIT_EVERY == 0:
                db.commit()
            _report(
                progress,
                phase="syncing",
                label=provider.label,
                provider=connection.provider,
                done=index,
                total=len(snapshot.items),
            )

        if (
            not first_sync
            and snapshot.full
            and provider.tracks_removals
        ):
            removed = set(links) - seen
            if removed:
                self._handle_removed(
                    db, connection, links, removed, summary
                )

        # Só conclui a primeira execução quando ela terminou sem erros. Até lá,
        # a remoção automática continua desligada para não apagar vínculos por
        # causa de uma leitura parcial.
        if first_sync and not summary["errors"]:
            connection.baseline_completed_at = utcnow()
            summary["baseline"] = True

        summary["pendingReview"] = pending_count(
            db, connection.user_id, connection.provider
        )
        provider.set_cursor(connection, snapshot.cursor)
        connection.last_sync_at = utcnow()
        connection.last_sync_status = "ok" if not summary["errors"] else "partial"
        summary["finishedAt"] = utcnow().isoformat()
        connection.last_sync_summary = _dumps(summary)
        connection.next_sync_at = utcnow() + _interval(connection.updates_per_day)
        connection.needs_reauth = False
        db.commit()
        _report(
            progress,
            phase="finishing",
            label=provider.label,
            provider=connection.provider,
            done=len(snapshot.items),
            total=len(snapshot.items),
        )
        return summary

    async def _process_item(
        self,
        db: Session,
        connection: LibraryConnection,
        provider: LibraryProvider,
        snapshot: SourceSnapshot,
        item: SourceItem,
        mappings: dict[str, LibraryStatusMapping],
        links: dict[str, LibrarySyncLink],
        media_by_id: dict[int, Media],
        media_by_external: dict[tuple[MediaTypeEnum, str], Media],
        media_by_title: dict[tuple[MediaTypeEnum, str], Media],
        suggestions: dict,
        summary: dict,
        run_at: datetime,
    ) -> None:
        status_def = next(
            (value for value in snapshot.statuses if value.key == item.status), None
        )
        mapping = mappings.get(item.status)
        action, target_status = _action_for_status(
            mapping, status_def.default_action if status_def else None
        )

        link = links.get(item.source_id)
        suggestion = suggestions.get(item.source_id)

        # Ignorado na revisão: não cria, não vincula e não tenta de novo.
        if suggestion is not None and suggestion.review_state == "dismissed":
            if link is not None:
                link.title = item.title
                link.last_seen_at = utcnow()
            return

        if action == "ignore":
            if link is not None:
                link.source_status = item.status
                link.last_source_status = item.status
                link.last_action = "ignore"
                link.last_status = None
                link.title = item.title
                link.last_seen_at = utcnow()
                db.flush()
            return

        media = media_by_id.get(link.media_id) if link and link.media_id else None
        if media is None and link is not None and link.media_id is not None:
            media = _get_media(db, link.media_id)
            if media is not None:
                media_by_id[media.id] = media

        if media is None:
            media = self._find_existing(
                item, media_by_external, media_by_title
            )

        if media is None:
            media = await self._resolve_review_item(
                db,
                connection,
                provider,
                item,
                suggestion,
                suggestions,
                action,
                target_status,
                status_def.label if status_def else item.status,
                summary,
                run_at,
            )
            if media is None:
                if link is not None:
                    link.source_status = item.status
                    link.title = item.title
                    link.last_seen_at = utcnow()
                summary["missing"] += 1
                if len(summary["missingTitles"]) < _MAX_LIST:
                    summary["missingTitles"].append(item.title)
                db.flush()
                return

        if link is None:
            link = LibrarySyncLink(
                user_id=connection.user_id,
                provider=connection.provider,
                source_id=item.source_id,
                media_id=media.id,
                media_type=item.media_type,
                title=item.title,
                source_status=item.status,
                last_source_status=None,
            )
            db.add(link)
            db.flush()
            links[item.source_id] = link

        if suggestion is not None and suggestion.review_state == "pending":
            suggestion.review_state = "approved"
            suggestion.media_id = media.id
            suggestion.decided_at = utcnow()
        if link.media_id is None:
            link.media_id = media.id
            link.match_error = None
            summary["pendingResolved"] += 1
        media_by_id[media.id] = media
        if item.external_id:
            media_by_external[(item.media_type, item.external_id)] = media
        media_by_title.setdefault(
            (item.media_type, normalize_title(media.title)), media
        )

        # Mídias antigas criadas por um caminho rápido sem metadata podem estar
        # sem capa. Busca os detalhes uma vez e preenche só o que estiver vazio.
        if not media.cover_url and not item.cover_url:
            await self._enrich_media(db, connection, media, item, summary)

        if _apply_notes(media, item):
            summary["notesUpdated"] += 1

        # Primeiro vínculo não sobrescreve um status já existente (baseline).
        first_action = link.last_action is None
        source_changed = link.last_source_status != item.status
        if action == "ignore":
            link.last_action = "ignore"
            link.last_status = None
        elif action == "backlog":
            if media.id is not None and _add_backlog(
                db, connection.user_id, media.id, media.type
            ):
                link.backlog_owned = True
                summary["backlogAdded"] += 1
            link.last_action = "backlog"
            link.last_status = None
        elif target_status is not None:
            if link.backlog_owned and media.id is not None:
                if _remove_backlog(db, connection.user_id, media.id):
                    summary["backlogRemoved"] += 1
                link.backlog_owned = False
            should_apply = source_changed or first_action
            if should_apply and not (first_action and media.status is not None):
                if media.status != target_status:
                    media.status = target_status
                    summary["statusUpdated"] += 1
            link.last_action = target_status.value
            link.last_status = target_status

        link.source_status = item.status
        link.last_source_status = item.status
        link.title = item.title
        link.last_seen_at = utcnow()
        link.media_type = item.media_type

        if (
            provider.creates_logs
            and target_status is not None
            and item.watched_at is not None
            and (first_action or source_changed)
        ):
            _add_media_log(db, connection, item, media, target_status, summary)

        db.flush()

    def _find_existing(
        self,
        item: SourceItem,
        media_by_external: dict[tuple[MediaTypeEnum, str], Media],
        media_by_title: dict[tuple[MediaTypeEnum, str], Media],
    ) -> Media | None:
        if item.external_id:
            media = media_by_external.get((item.media_type, item.external_id))
            if media is not None:
                return media
        return media_by_title.get((item.media_type, normalize_title(item.title)))

    @staticmethod
    def _source_refs(item: SourceItem) -> dict[str, str]:
        refs = dict(item.external_refs or {})
        if item.external_id:
            if item.media_type in (MediaTypeEnum.ANIME, MediaTypeEnum.MANGA):
                refs.setdefault("anilistId", str(item.external_id))
            elif item.media_type in (MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES):
                refs.setdefault("tmdbId", str(item.external_id))
            elif item.media_type == MediaTypeEnum.BOOK:
                refs.setdefault("openlibraryId", str(item.external_id))
        return refs

    @staticmethod
    def _candidate_provider(media_type: MediaTypeEnum, fallback: str) -> str:
        if media_type in (MediaTypeEnum.ANIME, MediaTypeEnum.MANGA):
            return "anilist"
        if media_type in (MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES):
            return "tmdb"
        if media_type == MediaTypeEnum.BOOK:
            return "openlibrary"
        if media_type == MediaTypeEnum.GAME:
            return "igdb"
        return fallback

    @classmethod
    def _synthetic_candidate(
        cls, connection: LibraryConnection, item: SourceItem
    ) -> ImportCandidate | None:
        if not item.external_id:
            return None
        return ImportCandidate(
            provider=cls._candidate_provider(item.media_type, connection.provider),
            external_id=str(item.external_id),
            media_type=item.media_type,
            title=item.title,
            year=item.year,
            cover_url=item.cover_url,
            overview=item.overview,
            release_date=item.release_date,
            score=1.0,
        )

    @staticmethod
    def _is_exact_match(
        item: SourceItem, candidate: ImportCandidate | None
    ) -> bool:
        if candidate is None:
            return False
        if item.external_id and str(candidate.external_id) == str(item.external_id):
            return True
        return candidate.score >= 1.0

    async def _match_review_item(
        self,
        db: Session,
        connection: LibraryConnection,
        item: SourceItem,
    ) -> tuple[
        Media | None,
        ImportCandidate | None,
        list[ImportCandidate],
        str | None,
        str | None,
    ]:
        try:
            response = await matcher.match(
                db,
                ImportMatchRequest(
                    user_id=connection.user_id,
                    items=[
                        ImportMatchRequestItem(
                            key=item.source_id,
                            title=item.title,
                            year=item.year,
                            overview=item.overview,
                            media_type=item.media_type,
                            external_refs=self._source_refs(item),
                        )
                    ],
                ),
                resolve_tmdb_key(db, connection.user_id),
                resolve_igdb_credentials(db, connection.user_id),
            )
            result = response.results[0] if response.results else None
        except Exception as exc:  # noqa: BLE001 — metadata indisponível
            return None, None, [], str(exc), None

        if result is None:
            return None, None, [], "O matcher não retornou resultado.", None

        if result.status == "already_in_library" and result.existing_media_id:
            media = _get_media(db, result.existing_media_id)
            if media is not None:
                return (
                    media,
                    result.match,
                    result.candidates,
                    result.error,
                    result.status,
                )

        candidate = result.match
        if candidate is None and result.candidates:
            candidate = result.candidates[0]
        return (
            None,
            candidate,
            result.candidates,
            result.error,
            result.status,
        )

    async def _enrich_media(
        self,
        db: Session,
        connection: LibraryConnection,
        media: Media,
        item: SourceItem,
        summary: dict,
    ) -> None:
        """Preenche capa/descrição de uma mídia já vinculada que está incompleta."""
        if not (item.external_id or item.external_refs):
            return
        _, candidate, _candidates, _error, _status = await self._match_review_item(
            db, connection, item
        )
        if candidate is None or not media.external_id:
            return
        if str(candidate.external_id) != str(media.external_id):
            return

        changed = False
        if not media.cover_url and candidate.cover_url:
            media.cover_url = candidate.cover_url
            changed = True
        if not media.description and candidate.overview:
            media.description = candidate.overview
            changed = True
        if media.release_date is None and candidate.release_date is not None:
            media.release_date = datetime.combine(
                candidate.release_date, datetime.min.time()
            )
            changed = True
        if changed:
            summary["notesUpdated"] += 1

    async def _resolve_review_item(
        self,
        db: Session,
        connection: LibraryConnection,
        provider: LibraryProvider,
        item: SourceItem,
        suggestion,
        suggestions: dict,
        action: str,
        target_status: MediaStatusEnum | None,
        source_label: str,
        summary: dict,
        run_at: datetime,
    ) -> Media | None:
        """Nunca cria mídia: alimenta a fila de revisão.

        A única exceção é quando o matcher encontra uma mídia que já existe no
        Logged; nesse caso o item é apenas vinculado.
        """
        if suggestion is not None and suggestion.review_state == "pending":
            stage_suggestion(
                db,
                connection,
                suggestions,
                item,
                action=action,
                target_status=target_status,
                source_label=source_label,
                candidate=None,
                error=None,
                sync_run_at=run_at,
            )
            return None

        if suggestion is not None and suggestion.review_state == "approved":
            # A mídia aprovada foi removida; volta para revisão.
            suggestion.review_state = "pending"
            suggestion.decided_at = None

        candidate: ImportCandidate | None = None
        candidates: list[ImportCandidate] = []
        error: str | None = None
        match_status: str | None = None

        if (
            provider.trusted_external_id
            and item.external_id
            and item.cover_url
        ):
            # O provider já entregou o ID usado pelo Logged e os metadados
            # visuais; não há por que gastar uma chamada de metadata.
            candidate = self._synthetic_candidate(connection, item)
            candidates = [candidate] if candidate is not None else []
            match_status = "matched"
        else:
            media, candidate, candidates, error, match_status = (
                await self._match_review_item(db, connection, item)
            )
            if media is not None:
                return media
            if candidate is None and item.external_id:
                candidate = self._synthetic_candidate(connection, item)
                if candidate is not None and not candidates:
                    candidates = [candidate]

        if suggestion is None:
            summary["reviewStaged"] += 1
        if candidate is None:
            error = error or "Sem candidato de metadata; revise antes de adicionar."
        elif error is None and (
            match_status == "ambiguous"
            or not self._is_exact_match(item, candidate)
        ):
            error = "Match não exato; revise antes de adicionar."
        stage_suggestion(
            db,
            connection,
            suggestions,
            item,
            action=action,
            target_status=target_status,
            source_label=source_label,
            candidate=candidate,
            error=error,
            candidates=candidates,
            sync_run_at=run_at,
        )
        return None

    def _handle_removed(
        self,
        db: Session,
        connection: LibraryConnection,
        links: dict[str, LibrarySyncLink],
        removed: set[str],
        summary: dict,
    ) -> None:
        for source_id in removed:
            link = links.get(source_id)
            if link is None or link.last_action == "ignore":
                continue
            if link.media_id is not None and link.backlog_owned:
                if _remove_backlog(db, connection.user_id, link.media_id):
                    summary["backlogRemoved"] += 1
                link.backlog_owned = False
            link.last_action = "ignore"
            link.last_status = None
            link.last_source_status = None
            summary["removed"] += 1
            if len(summary["removedTitles"]) < _MAX_LIST:
                summary["removedTitles"].append(link.title)

    def _load_media(
        self, db: Session, user_id: int
    ) -> tuple[
        dict[int, Media],
        dict[tuple[MediaTypeEnum, str], Media],
        dict[tuple[MediaTypeEnum, str], Media],
    ]:
        rows = (
            db.execute(
                select(Media)
                .where(Media.user_id == user_id)
                .options(lazyload(Media.logs), lazyload(Media.tags))
            )
            .scalars()
            .all()
        )
        by_id = {media.id: media for media in rows}
        by_external = {(media.type, str(media.external_id)): media for media in rows}
        by_title: dict[tuple[MediaTypeEnum, str], Media] = {}
        for media in rows:
            by_title.setdefault((media.type, normalize_title(media.title)), media)
        return by_id, by_external, by_title

    def _ensure_mappings(
        self,
        db: Session,
        connection: LibraryConnection,
        provider: LibraryProvider,
    ) -> dict[str, LibraryStatusMapping]:
        existing = {
            row.source_status: row
            for row in db.execute(
                select(LibraryStatusMapping).where(
                    LibraryStatusMapping.user_id == connection.user_id,
                    LibraryStatusMapping.provider == connection.provider,
                )
            )
            .scalars()
            .all()
        }
        for index, status_def in enumerate(provider.statuses()):
            if status_def.key in existing:
                continue
            action, target = _normalize_default(status_def.default_action)
            row = LibraryStatusMapping(
                user_id=connection.user_id,
                provider=connection.provider,
                source_status=status_def.key,
                source_label=status_def.label,
                position=index,
                action=action,
                status=target,
            )
            db.add(row)
            existing[status_def.key] = row
        db.flush()
        return existing

    @staticmethod
    def _empty_summary() -> dict:
        return {
            "provider": None,
            "processed": 0,
            "library": 0,
            "created": 0,
            "statusUpdated": 0,
            "backlogAdded": 0,
            "backlogRemoved": 0,
            "notesUpdated": 0,
            "logsCreated": 0,
            "pending": 0,
            "pendingResolved": 0,
            "missing": 0,
            "missingTitles": [],
            "unmatched": 0,
            "unmatchedTitles": [],
            "removed": 0,
            "removedTitles": [],
            "errors": [],
            "reviewStaged": 0,
            "pendingReview": 0,
            "baseline": False,
            "skipped": False,
        }


def _normalize_default(action: str) -> tuple[str, MediaStatusEnum | None]:
    if action in _STATUS_ACTIONS:
        return action, MediaStatusEnum(action)
    if action == "backlog":
        return "backlog", None
    return "ignore", None


engine = LibrarySyncEngine()


def sync_user(
    user_id: int,
    provider: str,
    run_at: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    if not _SYNC_LOCK.acquire(blocking=False):
        raise DomainError("Já existe uma sincronização em andamento. Tente novamente.")
    db = Session(bind=db_engine, expire_on_commit=False)
    try:
        connection = db.execute(
            select(LibraryConnection).where(
                LibraryConnection.user_id == user_id,
                LibraryConnection.provider == provider,
            )
        ).scalar_one_or_none()
        if connection is None:
            raise DomainError("Conexão não configurada.")

        async def _run_sync() -> dict:
            try:
                return await engine.sync(db, connection, run_at, progress)
            finally:
                # O sync roda em um loop próprio (asyncio.run): fecha os clientes
                # HTTP criados nele para não vazar conexões.
                await close_loop_clients()

        summary = asyncio.run(_run_sync())
        return summary
    except Exception as exc:  # noqa: BLE001 — reportado no último sync
        db.rollback()
        connection = db.execute(
            select(LibraryConnection).where(
                LibraryConnection.user_id == user_id,
                LibraryConnection.provider == provider,
            )
        ).scalar_one_or_none()
        if connection is not None:
            summary = engine._empty_summary()
            summary["provider"] = provider
            summary["error"] = str(exc)
            summary["finishedAt"] = utcnow().isoformat()
            connection.last_sync_at = utcnow()
            connection.last_sync_status = "failed"
            message = str(exc).lower()
            if any(
                token in message
                for token in ("reconecte", "expirada", "inválido", "invalid", "token")
            ):
                connection.needs_reauth = True
            connection.last_sync_summary = _dumps(summary)
            connection.next_sync_at = utcnow() + _interval(connection.updates_per_day)
            db.commit()
        if isinstance(exc, ValueError):
            raise
        raise DomainError(f"Falha ao sincronizar com {provider}: {exc}") from exc
    finally:
        db.close()
        _SYNC_LOCK.release()
