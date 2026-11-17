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

from sqlalchemy import select
from sqlalchemy.orm import Session, noload

from database import SessionLocal
from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import (
    LibraryConnection,
    LibraryStatusMapping,
    LibrarySyncLink,
)
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

_STATUS_ACTIONS = {status.value for status in MediaStatusEnum}
_PENDING_INTERVAL = timedelta(hours=24)
_MAX_LIST = 50
_SYNC_LOCK = threading.Lock()


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
        .options(noload(Media.logs), noload(Media.tags))
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


def _release_datetime(item: SourceItem) -> datetime | None:
    if item.release_date is not None:
        return datetime.combine(item.release_date, datetime.min.time())
    if item.year:
        return datetime(item.year, 1, 1)
    return None


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
        self, db: Session, connection: LibraryConnection
    ) -> dict:
        provider = get_provider(connection.provider)
        summary = self._empty_summary()
        summary["provider"] = connection.provider
        summary["startedAt"] = utcnow().isoformat()
        connection.last_sync_status = "syncing"
        db.commit()

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
        baseline_missing = set(cursor.get("baseline_missing") or [])
        snapshot = await provider.fetch(connection, cursor)

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
        baseline = connection.baseline_completed_at is None
        seen: set[str] = set()

        for item in snapshot.items:
            seen.add(item.source_id)
            summary["processed"] += 1
            try:
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
                    baseline,
                    baseline_missing,
                    summary,
                )
            except Exception as exc:  # noqa: BLE001 — um item não derruba o sync
                summary["errors"].append(f"{item.title}: {exc}")

        if (
            not baseline
            and snapshot.full
            and provider.tracks_removals
        ):
            removed = set(links) - seen
            if removed:
                self._handle_removed(
                    db, connection, links, removed, summary
                )

        if baseline and not summary["errors"]:
            connection.baseline_completed_at = utcnow()
            summary["baseline"] = True

        # Provedores em modo revisão não usam mais baseline_missing: um match
        # exato é criado e o restante vai para a fila de revisão.
        if provider.review_baseline:
            baseline_missing.clear()
        summary["pendingReview"] = pending_count(
            db, connection.user_id, connection.provider
        )
        snapshot.cursor["baseline_missing"] = sorted(baseline_missing)
        provider.set_cursor(connection, snapshot.cursor)
        connection.last_sync_at = utcnow()
        connection.last_sync_status = "ok" if not summary["errors"] else "partial"
        summary["finishedAt"] = utcnow().isoformat()
        connection.last_sync_summary = _dumps(summary)
        connection.next_sync_at = utcnow() + _interval(connection.updates_per_day)
        connection.needs_reauth = False
        db.commit()
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
        baseline: bool,
        baseline_missing: set[str],
        summary: dict,
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
            elif baseline:
                baseline_missing.add(item.source_id)
            return

        created_link = False
        if link is None:
            link = LibrarySyncLink(
                user_id=connection.user_id,
                provider=connection.provider,
                source_id=item.source_id,
                media_type=item.media_type,
                title=item.title,
                source_status=item.status,
                last_source_status=None,
            )
            db.add(link)
            db.flush()
            links[item.source_id] = link
            created_link = True

        media = media_by_id.get(link.media_id) if link.media_id else None
        if media is None and link.media_id is not None:
            media = _get_media(db, link.media_id)
            if media is not None:
                media_by_id[media.id] = media

        if media is None:
            media = self._find_existing(
                item, media_by_external, media_by_title
            )

        # Depois do baseline, todo provedor usa a fila: match exato cria direto,
        # match duvidoso vira sugestão. O baseline dos provedores grandes
        # continua apenas vinculando o que já existe.
        use_review = (
            provider.review_baseline
            or suggestion is not None
            or (not baseline and item.source_id not in baseline_missing)
        )
        if media is None and use_review:
            media = await self._resolve_review_item(
                db,
                connection,
                provider,
                item,
                link,
                media_by_external,
                media_by_title,
                suggestion,
                suggestions,
                action,
                target_status,
                status_def.label if status_def else item.status,
                summary,
            )
            if media is None:
                if created_link:
                    db.delete(link)
                    links.pop(item.source_id, None)
                    db.flush()
                summary["missing"] += 1
                if len(summary["missingTitles"]) < _MAX_LIST:
                    summary["missingTitles"].append(item.title)
                return

        if media is None:
            if baseline:
                if created_link:
                    db.delete(link)
                    links.pop(item.source_id, None)
                    db.flush()
                baseline_missing.add(item.source_id)
                summary["missing"] += 1
                if len(summary["missingTitles"]) < _MAX_LIST:
                    summary["missingTitles"].append(item.title)
                return
            if item.source_id in baseline_missing:
                summary["missing"] += 1
                if len(summary["missingTitles"]) < _MAX_LIST:
                    summary["missingTitles"].append(item.title)
                link.source_status = item.status
                link.title = item.title
                link.last_seen_at = utcnow()
                db.flush()
                return
            link.source_status = item.status
            link.title = item.title
            link.last_seen_at = utcnow()
            summary["unmatched"] += 1
            if len(summary["unmatchedTitles"]) < _MAX_LIST:
                summary["unmatchedTitles"].append(
                    {"title": item.title, "reason": link.match_error}
                )
            db.flush()
            return

        baseline_missing.discard(item.source_id)
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
    def _synthetic_candidate(
        connection: LibraryConnection, item: SourceItem
    ) -> ImportCandidate | None:
        if not item.external_id:
            return None
        return ImportCandidate(
            provider=connection.provider,
            external_id=str(item.external_id),
            media_type=item.media_type,
            title=item.title,
            year=item.year,
            cover_url=item.cover_url,
            overview=item.overview,
            release_date=item.release_date,
            score=0.0,
        )

    @staticmethod
    def _is_perfect_match(
        item: SourceItem,
        candidate: ImportCandidate | None,
        status: str | None,
    ) -> bool:
        if candidate is None:
            return False
        if item.external_id and str(candidate.external_id) == str(item.external_id):
            return True
        # Mesmo título e ano, sem concorrente próximo, também é match perfeito.
        return status == "matched" and candidate.score >= 1.0

    async def _match_review_item(
        self,
        db: Session,
        connection: LibraryConnection,
        item: SourceItem,
    ) -> tuple[Media | None, ImportCandidate | None, str | None, str | None]:
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
            return None, None, str(exc), None

        if result is None:
            return None, None, "O matcher não retornou resultado.", None

        if result.status == "already_in_library" and result.existing_media_id:
            media = _get_media(db, result.existing_media_id)
            if media is not None:
                return media, result.match, result.error, result.status

        candidate = result.match
        if candidate is None and result.candidates:
            candidate = result.candidates[0]
        return None, candidate, result.error, result.status

    async def _resolve_review_item(
        self,
        db: Session,
        connection: LibraryConnection,
        provider: LibraryProvider,
        item: SourceItem,
        link: LibrarySyncLink,
        media_by_external: dict[tuple[MediaTypeEnum, str], Media],
        media_by_title: dict[tuple[MediaTypeEnum, str], Media],
        suggestion,
        suggestions: dict,
        action: str,
        target_status: MediaStatusEnum | None,
        source_label: str,
        summary: dict,
    ) -> Media | None:
        """Cria direto em match exato; caso contrário, alimenta a revisão."""
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
            )
            return None

        if suggestion is not None and suggestion.review_state == "approved":
            # A mídia aprovada foi removida; volta para revisão.
            suggestion.review_state = "pending"
            suggestion.decided_at = None

        media, candidate, error, match_status = await self._match_review_item(
            db, connection, item
        )
        if media is not None:
            return media

        if candidate is None:
            candidate = self._synthetic_candidate(connection, item)

        if self._is_perfect_match(item, candidate, match_status):
            media = self._create_media(db, connection.user_id, item, candidate)
            media_by_external[(item.media_type, str(candidate.external_id))] = media
            media_by_title.setdefault(
                (item.media_type, normalize_title(media.title)), media
            )
            summary["created"] += 1
            return media

        if suggestion is None:
            summary["reviewStaged"] += 1
        if candidate is None:
            error = error or "Sem candidato de metadata; revise antes de adicionar."
        else:
            error = error or "Match não exato; revise antes de adicionar."
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
        )
        return None

    def _create_media(
        self,
        db: Session,
        user_id: int,
        item: SourceItem,
        candidate: ImportCandidate,
    ) -> Media:
        media = Media(
            user_id=user_id,
            external_id=str(candidate.external_id),
            title=candidate.title or item.title,
            type=item.media_type,
            description=candidate.overview or item.overview,
            cover_url=candidate.cover_url or item.cover_url,
            release_date=candidate.release_date
            and datetime.combine(candidate.release_date, datetime.min.time())
            or _release_datetime(item),
            status=None,
        )
        db.add(media)
        db.flush()
        return media

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
                .options(noload(Media.logs), noload(Media.tags))
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


def sync_user(user_id: int, provider: str) -> dict:
    if not _SYNC_LOCK.acquire(blocking=False):
        raise ValueError("Já existe uma sincronização em andamento. Tente novamente.")
    db = SessionLocal()
    try:
        connection = db.execute(
            select(LibraryConnection).where(
                LibraryConnection.user_id == user_id,
                LibraryConnection.provider == provider,
            )
        ).scalar_one_or_none()
        if connection is None:
            raise ValueError("Conexão não configurada.")
        summary = asyncio.run(engine.sync(db, connection))
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
        raise ValueError(f"Falha ao sincronizar com {provider}: {exc}") from exc
    finally:
        db.close()
        _SYNC_LOCK.release()
