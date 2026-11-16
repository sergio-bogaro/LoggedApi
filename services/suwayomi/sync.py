"""Sincronização da biblioteca do Suwayomi com o Logged.

A unidade é o título: a categoria do mangá no Suwayomi define o status e/ou o
backlog no Logged. Capítulos e progresso não são lidos.
"""

import asyncio
import json
import threading
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urljoin

from sqlalchemy import select
from sqlalchemy.orm import Session, noload

from database import SessionLocal
from models.enums import MediaStatusEnum, MediaTypeEnum
from models.media import Media
from models.media_list_item import MediaListItem
from models.suwayomi import SuwayomiAccount, SuwayomiCategoryMapping, SuwayomiMangaLink
from schemas.import_schemas import ImportCandidate
from services.importer.matcher import matcher
from services.importer.text import normalize_title, search_title
from services.suwayomi.categories import (
    ACTION_BACKLOG,
    ACTION_IGNORE,
    ACTION_STATUS,
    resolve_action,
)
from services.suwayomi.client import SuwayomiClient

_PENDING_RETRY_INTERVAL = timedelta(hours=24)


def utcnow() -> datetime:
    """Agora em UTC, sem timezone, no mesmo formato do SQLite do app."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_datetime(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min)


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
) -> None:
    if _find_backlog_item(db, user_id, media_id) is not None:
        return
    db.add(
        MediaListItem(
            user_id=user_id,
            media_type=MediaTypeEnum.MANGA,
            media_id=media_id,
            list_type="backlog",
            date_log=utcnow(),
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


def _create_media(
    db: Session,
    user_id: int,
    manga: dict,
    candidate: ImportCandidate,
    status: MediaStatusEnum | None,
    base_url: str,
) -> Media:
    release_date = _to_datetime(candidate.release_date)
    if release_date is None and candidate.year:
        release_date = datetime(int(candidate.year), 1, 1)

    thumbnail = manga.get("thumbnailUrl")
    fallback_cover = urljoin(base_url + "/", thumbnail) if thumbnail else None

    media = Media(
        user_id=user_id,
        external_id=str(candidate.external_id),
        title=candidate.title or manga.get("title") or "",
        type=MediaTypeEnum.MANGA,
        description=candidate.overview or manga.get("description"),
        cover_url=candidate.cover_url or fallback_cover,
        release_date=release_date,
        status=status,
    )
    db.add(media)
    db.flush()
    return media


def _apply_action(
    db: Session,
    user_id: int,
    media: Media,
    link: SuwayomiMangaLink,
    action: str,
    status: MediaStatusEnum | None,
    summary: dict,
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
        _add_to_backlog(db, user_id, media.id, link, summary)


class SuwayomiSyncService:
    def __init__(self) -> None:
        self._running: set[int] = set()
        self._lock = threading.Lock()

    def sync_user(self, user_id: int) -> dict:
        """Executa o sync em uma thread própria (abre sessão e event loop)."""
        with self._lock:
            if user_id in self._running:
                raise ValueError("Já existe uma sincronização do Suwayomi em andamento.")
            self._running.add(user_id)

        try:
            db = SessionLocal()
            try:
                account = db.execute(
                    select(SuwayomiAccount).where(SuwayomiAccount.user_id == user_id)
                ).scalar_one_or_none()
                if account is None:
                    raise ValueError("O Suwayomi ainda não foi configurado.")
                return asyncio.run(self._sync(db, account))
            finally:
                db.close()
        finally:
            with self._lock:
                self._running.discard(user_id)

    async def _sync(self, db: Session, account: SuwayomiAccount) -> dict:
        started = utcnow()
        summary = self._empty_summary(started)

        client = SuwayomiClient(
            account.base_url, username=account.username, password=account.password
        )
        try:
            categories = await client.get_categories()
            library = await client.get_library()
            await self._run(db, account, categories, library, summary)
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
    ) -> None:
        user_id = account.user_id
        mappings = _load_mappings(db, user_id)
        if not mappings:
            raise ValueError(
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

        for manga in library:
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
                    elif result.status == "matched" and result.match is not None:
                        media = _create_media(
                            db,
                            user_id,
                            manga,
                            result.match,
                            status if action == ACTION_STATUS else None,
                            account.base_url,
                        )
                        media_by_id[media.id] = media
                        existing_by_title[normalize_title(media.title)] = media
                        summary["created"] += 1
                    else:
                        link = SuwayomiMangaLink(
                            user_id=user_id,
                            suwayomi_manga_id=suwayomi_id,
                            media_id=None,
                            title=title,
                            last_seen_at=now,
                            last_match_attempt_at=now,
                            match_attempts=1,
                            match_error=(
                                result.error
                                or "Título não encontrado no AniList; resolva na página Importar."
                            ),
                        )
                        db.add(link)
                        links[suwayomi_id] = link
                        self._append_unmatched(summary, title, link.match_error)
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
                    summary,
                )
                if not resolved:
                    self._append_unmatched(
                        summary,
                        title,
                        link.match_error
                        or "Título pendente de revisão na página Importar.",
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
                self._append_unmatched(summary, title, link.match_error)
                continue

            target_status = status if action == ACTION_STATUS else None
            if (link.last_action, link.last_status) != (action, target_status):
                _apply_action(
                    db, user_id, media, link, action, status, summary
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
                link.last_action = action
                link.last_status = target_status

            link.last_seen_at = now
            link.title = title
            link.match_error = None

        if any(mapping.action != ACTION_IGNORE for mapping in mappings.values()):
            for link in links.values():
                if link.suwayomi_manga_id not in seen:
                    if len(summary["removed"]) < 50:
                        summary["removed"].append(link.title)

        summary["library"] = len(library)
        summary["processed"] = len(seen)

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
        summary: dict,
    ) -> bool:
        """Tenta resolver um link que ficou pendente em syncs anteriores.

        Título exato é tentado sempre (barato e resolve o caso de mídia
        adicionada manualmente). O AniList é tentado no máximo uma vez a cada
        24h para não martelar a API com títulos que nunca vão casar.
        """
        media = existing_by_title.get(normalize_title(title))

        if media is None:
            now = utcnow()
            if (
                link.last_match_attempt_at is not None
                and now - link.last_match_attempt_at < _PENDING_RETRY_INTERVAL
            ):
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
            elif result.status == "matched" and result.match is not None:
                media = _create_media(
                    db,
                    user_id,
                    manga,
                    result.match,
                    status if action == ACTION_STATUS else None,
                    account.base_url,
                )
                media_by_id[media.id] = media
                existing_by_title[normalize_title(media.title)] = media
                summary["created"] += 1
            else:
                link.match_error = (
                    result.error
                    or "Título não encontrado no AniList; resolva na página Importar."
                )
                return False

        if media is None:
            link.match_error = "Título não encontrado no Logged nem no AniList."
            return False

        link.media_id = media.id
        link.match_error = None
        media_by_id[media.id] = media
        existing_by_title.setdefault(normalize_title(media.title), media)
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
