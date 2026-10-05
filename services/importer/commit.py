"""Commit genérico de uma importação — serve qualquer tipo de mídia.

Cria/atualiza `Media`, os `MediaLog` das sessões e o item de backlog, tudo em
uma transação com savepoint por item (uma falha não derruba o lote).
"""

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.enums import MediaStatusEnum
from models.media import Media
from models.media_list_item import MediaListItem
from models.media_log import MediaLog
from schemas.import_schemas import (
    ImportCommitEntry,
    ImportCommitItem,
    ImportCommitRequest,
    ImportCommitResponse,
    ImportFailure,
)


def _to_datetime(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min)


@dataclass
class _CommitOutcome:
    created: bool = False
    changed: bool = False
    logs: int = 0
    backlog: bool = False


class ImportCommitter:
    def commit(self, db: Session, data: ImportCommitRequest) -> ImportCommitResponse:
        response = ImportCommitResponse()

        for entry in data.entries:
            try:
                # Savepoint por item: uma falha não derruba o lote inteiro.
                with db.begin_nested():
                    outcome = self._commit_entry(db, data.user_id, entry)
            except Exception as exc:  # noqa: BLE001 — reportado ao usuário por linha
                reason = str(exc)
                response.failures.append(ImportFailure(name=entry.title, reason=reason))
                response.items.append(
                    ImportCommitItem(
                        key=entry.key,
                        external_id=entry.external_id,
                        media_type=entry.media_type,
                        title=entry.title,
                        outcome="failed",
                        reason=reason,
                    )
                )
                continue

            if outcome.created:
                response.imported += 1
                result = "imported"
            elif outcome.changed:
                response.merged += 1
                result = "merged"
            else:
                response.skipped += 1
                result = "skipped"
            response.logs_created += outcome.logs
            if outcome.backlog:
                response.backlog_added += 1

            response.items.append(
                ImportCommitItem(
                    key=entry.key,
                    external_id=entry.external_id,
                    media_type=entry.media_type,
                    title=entry.title,
                    outcome=result,
                    logs_created=outcome.logs,
                    backlog_added=outcome.backlog,
                )
            )

        db.commit()
        return response

    def _commit_entry(
        self, db: Session, user_id: int, entry: ImportCommitEntry
    ) -> _CommitOutcome:
        outcome = _CommitOutcome()

        media = db.execute(
            select(Media).where(
                Media.user_id == user_id,
                Media.type == entry.media_type,
                Media.external_id == entry.external_id,
            )
        ).scalar_one_or_none()

        if media is None:
            media = Media(
                user_id=user_id,
                external_id=entry.external_id,
                title=entry.title,
                type=entry.media_type,
                description=entry.overview,
                cover_url=entry.cover_url,
                release_date=_to_datetime(entry.release_date),
                status=entry.status,
                rating=entry.rating,
                review=entry.review,
            )
            db.add(media)
            db.flush()
            outcome.created = True
        else:
            # Mescla: preenche só o que estiver vazio, sem sobrescrever o usuário.
            if media.rating is None and entry.rating is not None:
                media.rating = entry.rating
                outcome.changed = True
            if not media.review and entry.review:
                media.review = entry.review
                outcome.changed = True
            if media.status is None and entry.status is not None:
                media.status = entry.status
                outcome.changed = True
            if not media.cover_url and entry.cover_url:
                media.cover_url = entry.cover_url
                outcome.changed = True
            if not media.description and entry.overview:
                media.description = entry.overview
                outcome.changed = True
            if media.release_date is None and entry.release_date:
                media.release_date = _to_datetime(entry.release_date)
                outcome.changed = True

        existing_dates = {log.date for log in media.logs}
        for log in entry.logs:
            if log.date in existing_dates:
                continue
            status = log.status or entry.status or MediaStatusEnum.FINISHED
            end_date = log.end_date
            if end_date is None and status in (MediaStatusEnum.FINISHED, MediaStatusEnum.DROPPED):
                end_date = _to_datetime(log.date)
            db.add(
                MediaLog(
                    user_id=user_id,
                    media_id=media.id,
                    date=log.date,
                    status=status,
                    rating=log.rating if log.rating is not None else entry.rating,
                    review=log.review or entry.review,
                    start_date=log.start_date or _to_datetime(log.date),
                    end_date=end_date,
                )
            )
            existing_dates.add(log.date)
            outcome.logs += 1
            outcome.changed = True

        if entry.add_to_backlog and not self._in_backlog(db, user_id, media.id):
            db.add(
                MediaListItem(
                    user_id=user_id,
                    media_type=entry.media_type,
                    media_id=media.id,
                    list_type="backlog",
                    date_log=_to_datetime(entry.backlog_date) or datetime.now(),
                )
            )
            outcome.backlog = True
            outcome.changed = True

        return outcome

    @staticmethod
    def _in_backlog(db: Session, user_id: int, media_id: int) -> bool:
        query = select(MediaListItem).where(
            MediaListItem.user_id == user_id,
            MediaListItem.media_id == media_id,
            MediaListItem.list_type == "backlog",
        )
        return db.execute(query).scalar_one_or_none() is not None


committer = ImportCommitter()
