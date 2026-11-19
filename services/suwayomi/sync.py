"""Sincronização da biblioteca do Suwayomi com o Logged.

A unidade é o título: a categoria do mangá no Suwayomi define o status e/ou o
backlog no Logged. Capítulos e progresso não são lidos.
"""

import asyncio
import json
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, noload

from database import SessionLocal
from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import LibrarySyncSuggestion
from models.media import Media
from models.media_list_item import MediaListItem
from models.media_log import MediaLog
from models.suwayomi import SuwayomiAccount, SuwayomiCategoryMapping, SuwayomiMangaLink
from schemas.import_schemas import ImportCandidate, ImportMatchResult
from services.importer.matcher import matcher
from services.importer.text import normalize_title, search_title
from services.library_sync.suggestions import (
    load_suggestions,
    stage_suggestion_values,
)
from services.suwayomi.categories import (
    ACTION_BACKLOG,
    ACTION_IGNORE,
    ACTION_STATUS,
    resolve_action,
)
from services.suwayomi.client import SuwayomiClient
from domain_errors import DomainError

_PENDING_RETRY_INTERVAL = timedelta(hours=24)
ProgressCallback = Callable[[dict], None]


def _report(progress: ProgressCallback | None, **fields) -> None:
    if progress is None:
        return
    try:
        progress(fields)
    except Exception:  # noqa: BLE001 — progresso nunca derruba o sync
        pass


def utcnow() -> datetime:
    """Agora em UTC, sem timezone, no mesmo formato do SQLite do app."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _from_unix(value: object) -> datetime | None:
    """Converte o timestamp Unix (segundos) do Suwayomi para UTC naive."""
    try:
        timestamp = int(str(value))
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)


def _manga_dates(manga: dict) -> tuple[datetime | None, datetime | None]:
    """Devolve (entrada na biblioteca, última leitura)."""
    in_library = _from_unix(manga.get("inLibraryAt"))
    last_read = None
    for key in ("lastReadChapter", "latestReadChapter"):
        chapter = manga.get(key) or {}
        last_read = _from_unix(chapter.get("lastReadAt"))
        if last_read is not None:
            break
    return in_library, last_read


def _has_log(
    db: Session, media_id: int, day: date, status: MediaStatusEnum
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
    user_id: int,
    media: Media,
    status: MediaStatusEnum | None,
    in_library_at: datetime | None,
    last_read_at: datetime | None,
    summary: dict,
) -> None:
    if status is None:
        return

    ended = status in (MediaStatusEnum.FINISHED, MediaStatusEnum.DROPPED)
    moment = last_read_at or in_library_at or utcnow()
    day = moment.date()
    if _has_log(db, media.id, day, status):
        return

    start = in_library_at
    end = moment if ended else None
    if start is not None and end is not None and start > end:
        start = end
    db.add(
        MediaLog(
            user_id=user_id,
            media_id=media.id,
            date=day,
            status=status,
            start_date=start,
            end_date=end,
        )
    )
    summary["logsCreated"] += 1


def _load_mappings(db: Session, user_id: int) -> dict[int, SuwayomiCategoryMapping]:
    rows = (
        db.execute(
            select(SuwayomiCategoryMapping).where(
                SuwayomiCategoryMapping.user_id == user_id
            )
        )
        .scalars()
        .all()
    )
    return {row.category_id: row for row in rows}


def _get_media(db: Session, media_id: int) -> Media | None:
    return db.execute(
        select(Media)
        .where(Media.id == media_id)
        .options(noload(Media.logs), noload(Media.tags))
    ).scalar_one_or_none()


def _find_backlog_item(db: Session, user_id: int, media_id: int) -> MediaListItem | None:
    return db.execute(
        select(MediaListItem).where(
            MediaListItem.user_id == user_id,
            MediaListItem.media_id == media_id,
            MediaListItem.list_type == "backlog",
        )
    ).scalar_one_or_none()


def _add_to_backlog(
    db: Session,
    user_id: int,
    media_id: int,
    link: SuwayomiMangaLink,
    summary: dict,
    date_log: datetime | None = None,
) -> None:
    if _find_backlog_item(db, user_id, media_id) is not None:
        return
    db.add(
        MediaListItem(
            user_id=user_id,
            media_type=MediaTypeEnum.MANGA,
            media_id=media_id,
            list_type="backlog",
            date_log=date_log or utcnow(),
        )
    )
    link.backlog_owned = True
    summary["backlogAdded"] += 1


def _remove_owned_backlog(
    db: Session,
    user_id: int,
    media_id: int,
    link: SuwayomiMangaLink,
    summary: dict,
) -> None:
    if not link.backlog_owned:
        return
    item = _find_backlog_item(db, user_id, media_id)
    if item is not None:
        db.delete(item)
        summary["backlogRemoved"] += 1
    link.backlog_owned = False


def _apply_action(
    db: Session,
    user_id: int,
    media: Media,
    link: SuwayomiMangaLink,
    action: str,
    status: MediaStatusEnum | None,
    summary: dict,
    in_library_at: datetime | None = None,
) -> None:
    if action == ACTION_STATUS and status is not None:
        if media.status != status:
            media.status = status
            summary["statusUpdated"] += 1
        _remove_owned_backlog(db, user_id, media.id, link, summary)
        return

    if action == ACTION_BACKLOG:
        # Ao sair de um status aplicado pelo sync, limpa o resumo — desde que
        # o usuário não tenha trocado o status manualmente.
        if link.last_status is not None and media.status == link.last_status:
            media.status = None
        _add_to_backlog(db, user_id, media.id, link, summary, in_library_at)


def _suggestion_action(action: str, status: MediaStatusEnum | None) -> str:
    if action == ACTION_STATUS and status is not None:
        return status.value
    if action == ACTION_BACKLOG:
        return ACTION_BACKLOG
    return ACTION_IGNORE


def _stage_review_suggestion(
    db: Session,
    suggestions: dict[str, LibrarySyncSuggestion],
    *,
    user_id: int,
    link: SuwayomiMangaLink,
    manga: dict,
    title: str,
    action: str,
    status: MediaStatusEnum | None,
    candidate: ImportCandidate | None,
    candidates: list[ImportCandidate] | None,
    error: str | None,
    sync_run_at: datetime,
    summary: dict,
) -> LibrarySyncSuggestion:
    source_id = str(link.suwayomi_manga_id)
    real_url = (manga.get("realUrl") or manga.get("url") or "").strip()
    refs = {"suwayomiId": source_id}
    if real_url:
        refs["suwayomiUrl"] = real_url

    watched_date: date | None = None
    if status is not None:
        in_library_at, last_read_at = _manga_dates(manga)
        moment = last_read_at or in_library_at or utcnow()
        watched_date = moment.date()

    already_exists = source_id in suggestions
    suggestion = stage_suggestion_values(
        db,
        suggestions,
        user_id=user_id,
        provider="suwayomi",
        source_id=source_id,
        media_type=MediaTypeEnum.MANGA,
        title=title,
        source_status=_suggestion_action(action, status),
        source_label=_suggestion_action(action, status),
        action=_suggestion_action(action, status),
        target_status=status,
        candidate=candidate,
        error=error,
        watched_date=watched_date,
        source_url=real_url or None,
        refs=refs,
        candidates=candidates,
        sync_run_at=sync_run_at,
    )
    if not already_exists:
        summary["reviewStaged"] += 1
    return suggestion


def _approve_suggestion(
    suggestions: dict[str, LibrarySyncSuggestion],
    suwayomi_id: int,
    media: Media,
) -> None:
    suggestion = suggestions.get(str(suwayomi_id))
    if suggestion is None or suggestion.review_state == "approved":
        return
    suggestion.review_state = "approved"
    suggestion.media_id = media.id
    suggestion.error = None
    suggestion.decided_at = utcnow()


def _match_error(result: ImportMatchResult) -> str:
    if result.error:
        return result.error
    if result.match is not None:
        return "Match duvidoso; revise antes de adicionar."
    return "Título não encontrado no AniList; busque um match."


class SuwayomiSyncService:
    def __init__(self) -> None:
        self._running: set[int] = set()
        self._lock = threading.Lock()

    def sync_user(
        self,
        user_id: int,
        run_at: datetime | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict:
        """Executa o sync em uma thread própria (abre sessão e event loop)."""
        with self._lock:
            if user_id in self._running:
                raise DomainError("Já existe uma sincronização do Suwayomi em andamento.")
            self._running.add(user_id)

        try:
            db = SessionLocal()
            try:
                account = db.execute(
                    select(SuwayomiAccount).where(SuwayomiAccount.user_id == user_id)
                ).scalar_one_or_none()
                if account is None:
                    raise DomainError("O Suwayomi ainda não foi configurado.")
                return asyncio.run(self._sync(db, account, run_at, progress))
            finally:
                db.close()
        finally:
            with self._lock:
                self._running.discard(user_id)

    async def _sync(
        self,
        db: Session,
        account: SuwayomiAccount,
        run_at: datetime | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict:
        started = run_at or utcnow()
        summary = self._empty_summary(started)
        _report(
            progress,
            phase="fetching",
            label="Suwayomi",
            provider="suwayomi",
            done=0,
            total=0,
        )

        client = SuwayomiClient(
            account.base_url, username=account.username, password=account.password
        )
        try:
            categories = await client.get_categories()
            library = await client.get_library()
            _report(
                progress,
                phase="syncing",
                label="Suwayomi",
                provider="suwayomi",
                done=0,
                total=len(library),
            )
            await self._run(
                db, account, categories, library, summary, started, progress
            )
            status = "partial" if summary["unmatched"] or summary["errors"] else "ok"
        except Exception as exc:  # noqa: BLE001 — reportado na tela
            db.rollback()
            summary["errors"].append(str(exc))
            status = "error"
        finally:
            await client.aclose()

        finished = utcnow()
        summary["finishedAt"] = finished.isoformat()
        summary["status"] = status
        _report(
            progress,
            phase="finishing",
            label="Suwayomi",
            provider="suwayomi",
        )

        # Reabre a transação para persistir o resultado mesmo após rollback.
        account.last_sync_at = finished
        account.last_sync_status = status
        account.last_sync_summary = json.dumps(summary, ensure_ascii=False)
        if account.enabled:
            account.next_sync_at = finished + self._interval(account.updates_per_day)
        else:
            account.next_sync_at = None
        db.add(account)
        db.commit()

        return summary

    async def _run(
        self,
        db: Session,
        account: SuwayomiAccount,
        categories: list[dict],
        library: list[dict],
        summary: dict,
        run_at: datetime | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        run_at = run_at or utcnow()
        user_id = account.user_id
        mappings = _load_mappings(db, user_id)
        if not mappings:
            raise DomainError(
                "Nenhuma categoria do Suwayomi foi mapeada. "
                "Configure em Configurações → Integrações."
            )
        category_meta = {category["id"]: category for category in categories}

        # Atualiza nomes/ordem das categorias já mapeadas para refletir o servidor.
        for category_id, mapping in mappings.items():
            meta = category_meta.get(category_id)
            if meta is not None:
                mapping.category_name = meta.get("name") or mapping.category_name
                mapping.category_order = int(meta.get("order") or 0)

        links = {
            link.suwayomi_manga_id: link
            for link in db.execute(
                select(SuwayomiMangaLink).where(SuwayomiMangaLink.user_id == user_id)
            )
            .scalars()
            .all()
        }
        suggestions = load_suggestions(db, user_id, "suwayomi")

        # Mapa de títulos já existentes para vincular sem duplicar mídias.
        existing_by_title: dict[str, Media] = {}
        existing_media = (
            db.execute(
                select(Media)
                .where(Media.user_id == user_id, Media.type == MediaTypeEnum.MANGA)
                .options(noload(Media.logs), noload(Media.tags))
            )
            .scalars()
            .all()
        )
        for media in existing_media:
            existing_by_title.setdefault(normalize_title(media.title), media)
        media_by_id = {media.id: media for media in existing_media}

        seen: set[int] = set()

        for index, manga in enumerate(library, start=1):
            _report(
                progress,
                phase="syncing",
                label="Suwayomi",
                provider="suwayomi",
                done=index - 1,
                total=len(library),
            )
            try:
                suwayomi_id = int(manga["id"])
            except (KeyError, TypeError, ValueError):
                continue
            seen.add(suwayomi_id)
            title = manga.get("title") or str(suwayomi_id)
            action, status = resolve_action(
                (manga.get("categories") or {}).get("nodes") or [],
                mappings,
                category_meta,
            )
            now = utcnow()
            link = links.get(suwayomi_id)
            suggestion = suggestions.get(str(suwayomi_id))

            # Ignorado manualmente na revisão da importação: não tenta match,
            # não cria mídia e não aparece como pendente.
            if (
                suggestion is not None
                and suggestion.review_state == "dismissed"
                and (link is None or link.media_id is None)
            ):
                if link is not None:
                    link.ignored = True
                    link.match_error = None
                    link.last_seen_at = now
                summary["ignoredSkipped"] += 1
                continue

            if link is not None and link.ignored and link.media_id is None:
                link.last_seen_at = now
                summary["ignoredSkipped"] += 1
                continue

            if action is None:
                if link is not None:
                    link.last_seen_at = now
                    if link.last_action not in (None, ACTION_IGNORE):
                        # "Ignorar" para de gerenciar o título, mas não desfaz
                        # o que já foi aplicado. O last_status é mantido para
                        # que uma ida posterior ao backlog saiba o que limpar.
                        link.last_action = ACTION_IGNORE
                    if not link.title:
                        link.title = title
                continue

            in_library_at, last_read_at = _manga_dates(manga)
            if in_library_at is not None or last_read_at is not None:
                summary["datedItems"] += 1

            if link is None:
                media = existing_by_title.get(normalize_title(title))
                if media is None:
                    try:
                        result = await matcher.resolve_manga(db, user_id, title, search_title(title))
                    except Exception as exc:  # noqa: BLE001 — tenta de novo no próximo sync
                        if len(summary["errors"]) < 50:
                            summary["errors"].append(f"{title}: {exc}")
                        continue

                    if result.status == "already_in_library" and result.existing_media_id:
                        media = _get_media(db, result.existing_media_id)
                    else:
                        error = (
                            None
                            if result.status == "matched" and result.match is not None
                            else _match_error(result)
                        )
                        link = SuwayomiMangaLink(
                            user_id=user_id,
                            suwayomi_manga_id=suwayomi_id,
                            media_id=None,
                            title=title,
                            last_seen_at=now,
                            last_match_attempt_at=now,
                            match_attempts=1,
                            match_error=error,
                        )
                        db.add(link)
                        links[suwayomi_id] = link
                        has_candidate = result.match is not None
                        _stage_review_suggestion(
                            db,
                            suggestions,
                            user_id=user_id,
                            link=link,
                            manga=manga,
                            title=title,
                            action=action,
                            status=status,
                            candidate=result.match if has_candidate else None,
                            candidates=(
                                result.candidates if has_candidate else None
                            ),
                            error=error,
                            sync_run_at=run_at,
                            summary=summary,
                        )
                        if result.status != "matched":
                            self._append_unmatched(summary, title, error or "")
                        continue

                link = SuwayomiMangaLink(
                    user_id=user_id,
                    suwayomi_manga_id=suwayomi_id,
                    media_id=media.id if media else None,
                    title=title,
                    last_seen_at=now,
                )
                db.add(link)
                links[suwayomi_id] = link
                if media is not None:
                    existing_by_title.setdefault(normalize_title(media.title), media)
                    _approve_suggestion(suggestions, suwayomi_id, media)

            if link.media_id is None:
                link.last_seen_at = now
                link.title = title
                resolved = await self._resolve_pending_link(
                    db,
                    user_id,
                    link,
                    title,
                    manga,
                    action,
                    status,
                    account,
                    existing_by_title,
                    media_by_id,
                    suggestions,
                    summary,
                    run_at,
                )
                if not resolved:
                    self._append_unmatched(
                        summary,
                        title,
                        link.match_error
                        or "Título pendente; vincule ou ignore em Configurações → Suwayomi.",
                    )
                    continue

            media = media_by_id.get(link.media_id)
            if media is None:
                media = _get_media(db, link.media_id)
                if media is not None:
                    media_by_id[media.id] = media
            if media is None:
                link.media_id = None
                link.match_error = "A mídia vinculada foi removida do Logged."
                link.last_seen_at = now
                removed_suggestion = suggestions.get(str(suwayomi_id))
                if removed_suggestion is not None:
                    removed_suggestion.review_state = "pending"
                    removed_suggestion.media_id = None
                    removed_suggestion.error = link.match_error
                    removed_suggestion.decided_at = None
                self._append_unmatched(summary, title, link.match_error)
                continue

            _approve_suggestion(suggestions, suwayomi_id, media)

            target_status = status if action == ACTION_STATUS else None
            if (link.last_action, link.last_status) != (action, target_status):
                _apply_action(
                    db,
                    user_id,
                    media,
                    link,
                    action,
                    status,
                    summary,
                    in_library_at,
                )
                previous = (
                    link.last_status.value
                    if link.last_action == ACTION_STATUS and link.last_status
                    else link.last_action
                )
                self._append_change(
                    summary,
                    title,
                    previous,
                    target_status.value if target_status else action,
                    suwayomi_id,
                )
                if action == ACTION_STATUS and status is not None:
                    _add_media_log(
                        db,
                        user_id,
                        media,
                        status,
                        in_library_at,
                        last_read_at,
                        summary,
                    )
                link.last_action = action
                link.last_status = target_status

            link.last_seen_at = now
            link.title = title
            link.match_error = None
            _report(
                progress,
                phase="syncing",
                label="Suwayomi",
                provider="suwayomi",
                done=index,
                total=len(library),
            )

        if any(mapping.action != ACTION_IGNORE for mapping in mappings.values()):
            for link in links.values():
                if link.suwayomi_manga_id not in seen:
                    if len(summary["removed"]) < 50:
                        summary["removed"].append(link.title)

        summary["library"] = len(library)
        summary["processed"] = len(seen)
        _report(
            progress,
            phase="syncing",
            label="Suwayomi",
            provider="suwayomi",
            done=len(library),
            total=len(library),
        )

    async def _resolve_pending_link(
        self,
        db: Session,
        user_id: int,
        link: SuwayomiMangaLink,
        title: str,
        manga: dict,
        action: str,
        status: MediaStatusEnum | None,
        account: SuwayomiAccount,
        existing_by_title: dict[str, Media],
        media_by_id: dict[int, Media],
        suggestions: dict[str, LibrarySyncSuggestion],
        summary: dict,
        run_at: datetime,
    ) -> bool:
        """Tenta resolver um link que ficou pendente em syncs anteriores.

        Título exato é tentado sempre (barato e resolve o caso de mídia
        adicionada manualmente). O AniList é tentado no máximo uma vez a cada
        24h para não martelar a API com títulos que nunca vão casar.
        """
        suggestion = suggestions.get(str(link.suwayomi_manga_id))
        if suggestion is not None:
            if suggestion.review_state == "approved":
                if link.media_id is None:
                    suggestion.review_state = "pending"
                    suggestion.media_id = None
                else:
                    return False
            if suggestion.review_state == "dismissed":
                return False
            # Já existe candidato aguardando decisão; não sobrescreve a escolha.
            if suggestion.candidate_json:
                return False

        media = existing_by_title.get(normalize_title(title))

        if media is None:
            now = utcnow()
            if (
                link.last_match_attempt_at is not None
                and now - link.last_match_attempt_at < _PENDING_RETRY_INTERVAL
            ):
                _stage_review_suggestion(
                    db,
                    suggestions,
                    user_id=user_id,
                    link=link,
                    manga=manga,
                    title=title,
                    action=action,
                    status=status,
                    candidate=None,
                    candidates=None,
                    error=(
                        link.match_error
                        or "Aguardando novo match no AniList; busque um match."
                    ),
                    sync_run_at=run_at,
                    summary=summary,
                )
                return False

            link.last_match_attempt_at = now
            link.match_attempts = (link.match_attempts or 0) + 1

            try:
                result = await matcher.resolve_manga(
                    db, user_id, title, search_title(title)
                )
            except Exception as exc:  # noqa: BLE001 — tenta de novo no próximo sync
                link.match_error = str(exc)
                if len(summary["errors"]) < 50:
                    summary["errors"].append(f"{title}: {exc}")
                return False

            if result.status == "already_in_library" and result.existing_media_id:
                media = _get_media(db, result.existing_media_id)
            else:
                error = (
                    None
                    if result.status == "matched" and result.match is not None
                    else _match_error(result)
                )
                link.match_error = error
                has_candidate = result.match is not None
                _stage_review_suggestion(
                    db,
                    suggestions,
                    user_id=user_id,
                    link=link,
                    manga=manga,
                    title=title,
                    action=action,
                    status=status,
                    candidate=result.match if has_candidate else None,
                    candidates=result.candidates if has_candidate else None,
                    error=error,
                    sync_run_at=run_at,
                    summary=summary,
                )
                return False

        if media is None:
            link.match_error = "Título não encontrado no Logged nem no AniList."
            _stage_review_suggestion(
                db,
                suggestions,
                user_id=user_id,
                link=link,
                manga=manga,
                title=title,
                action=action,
                status=status,
                candidate=None,
                candidates=None,
                error=link.match_error,
                sync_run_at=run_at,
                summary=summary,
            )
            return False

        link.media_id = media.id
        link.match_error = None
        media_by_id[media.id] = media
        existing_by_title.setdefault(normalize_title(media.title), media)
        _approve_suggestion(suggestions, link.suwayomi_manga_id, media)
        if len(summary["pendingResolved"]) < 50:
            summary["pendingResolved"].append(title)
        return True

    @staticmethod
    def _interval(updates_per_day: int) -> timedelta:
        per_day = min(max(int(updates_per_day or 1), 1), 24)
        return timedelta(hours=24 / per_day)

    @staticmethod
    def _empty_summary(started: datetime) -> dict:
        return {
            "status": "ok",
            "startedAt": started.isoformat(),
            "finishedAt": None,
            "library": 0,
            "processed": 0,
            "created": 0,
            "statusUpdated": 0,
            "backlogAdded": 0,
            "backlogRemoved": 0,
            "logsCreated": 0,
            "datedItems": 0,
            "ignoredSkipped": 0,
            "reviewStaged": 0,
            "unmatched": [],
            "removed": [],
            "changes": [],
            "errors": [],
            "pendingResolved": [],
        }

    @staticmethod
    def _append_change(
        summary: dict,
        title: str,
        previous: str | None,
        action: str,
        suwayomi_id: int,
    ) -> None:
        if len(summary["changes"]) >= 50:
            return
        summary["changes"].append(
            {
                "suwayomiId": suwayomi_id,
                "title": title,
                "from": previous,
                "to": action,
            }
        )

    @staticmethod
    def _append_unmatched(summary: dict, title: str, reason: str) -> None:
        if len(summary["unmatched"]) >= 50:
            return
        if any(item.get("title") == title for item in summary["unmatched"]):
            return
        summary["unmatched"].append({"title": title, "reason": reason})


service = SuwayomiSyncService()
