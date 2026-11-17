"""Operações síncronas dos jobs de importação (API e commit)."""

import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.import_job import ImportJob, ImportJobItem
from models.suwayomi import SuwayomiMangaLink
from schemas.import_job import (
    ImportJobCommitRequest,
    ImportJobItemResponse,
    ImportJobItemsResponse,
    ImportJobPatch,
    ImportJobResponse,
)
from schemas.import_schemas import ImportCommitRequest, ImportCommitResponse
from services.importer.commit import committer
from services.import_jobs.serializers import (
    candidate_to_json,
    commit_entry,
    external_refs_from_json,
    item_counts_for_filter,
    item_response,
    job_response,
    set_counts,
)
from services.import_jobs.storage import delete_job_file

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _suwayomi_id(item: ImportJobItem) -> int | None:
    if item.source != "suwayomi":
        return None
    raw = external_refs_from_json(item.external_refs_json).get("suwayomiId")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _suwayomi_link(
    db: Session, user_id: int, item: ImportJobItem
) -> SuwayomiMangaLink | None:
    suwayomi_id = _suwayomi_id(item)
    if suwayomi_id is None:
        return None

    link = db.execute(
        select(SuwayomiMangaLink).where(
            SuwayomiMangaLink.user_id == user_id,
            SuwayomiMangaLink.suwayomi_manga_id == suwayomi_id,
        )
    ).scalar_one_or_none()
    if link is None:
        link = SuwayomiMangaLink(
            user_id=user_id,
            suwayomi_manga_id=suwayomi_id,
            title=item.title,
        )
        db.add(link)
    elif item.title:
        link.title = item.title
    return link


def _persist_suwayomi_ignore(
    db: Session, user_id: int, item: ImportJobItem, ignored: bool
) -> None:
    """Guarda a decisão de ignorar/restaurar para a sincronização periódica.

    Sem isso, o próximo sync trataria o título como novo e poderia criá-lo ou
    listá-lo como pendente de match, desfazendo a escolha feita na revisão.
    """
    link = _suwayomi_link(db, user_id, item)
    if link is None:
        return

    if ignored:
        # Um vínculo já existente continua sendo sincronizado normalmente.
        if link.media_id is None:
            link.ignored = True
            link.match_error = None
            link.last_match_attempt_at = None
            link.match_attempts = 0
        return

    link.ignored = False
    link.match_error = None
    link.last_match_attempt_at = None
    link.match_attempts = 0


def _persist_suwayomi_existing(
    db: Session, user_id: int, item: ImportJobItem
) -> None:
    """Vincula um item que já existia no Logged, sem depender do match exato."""
    if not item.existing_media_id:
        return
    link = _suwayomi_link(db, user_id, item)
    if link is None or link.media_id is not None:
        return
    link.media_id = item.existing_media_id
    link.ignored = False
    link.match_error = None
    link.last_match_attempt_at = None
    link.match_attempts = 0
    # A próxima sincronização aplica a categoria atual.
    link.last_action = None
    link.last_status = None


def _filter_condition(name: str | None):
    if name in (None, "", "all"):
        return None
    if name == "log":
        return ImportJobItem.logs_count > 0
    if name == "backlog":
        return ImportJobItem.in_backlog.is_(True)
    if name == "validated":
        return ImportJobItem.match_status.in_(["matched", "already_in_library"])
    if name == "attention":
        return ImportJobItem.match_status == "ambiguous"
    if name == "errors":
        return or_(
            ImportJobItem.match_status.in_(["not_found", "error"]),
            ImportJobItem.commit_outcome == "failed",
        )
    if name == "ignored":
        return ImportJobItem.ignored.is_(True)
    raise ValueError(f"Filtro de importação inválido: '{name}'.")


class ImportJobService:
    def create(
        self,
        db: Session,
        *,
        user_id: int,
        provider: str,
        input_type: str,
        media_type=None,
        source_url: str | None = None,
        source_username: str | None = None,
        file_path: str | None = None,
        file_name: str | None = None,
    ) -> ImportJob:
        job = ImportJob(
            user_id=user_id,
            provider=provider,
            input_type=input_type,
            media_type=media_type,
            source_url=source_url,
            source_username=source_username,
            file_path=file_path,
            file_name=file_name,
            status="queued",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def list(
        self, db: Session, user_id: int, status: str | None = None, limit: int = 20
    ) -> list[ImportJobResponse]:
        query = (
            select(ImportJob)
            .where(ImportJob.user_id == user_id)
            .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
            .limit(min(max(limit, 1), 100))
        )
        if status:
            query = query.where(ImportJob.status == status)
        return [job_response(job) for job in db.execute(query).scalars().all()]

    def get(self, db: Session, job_id: int, user_id: int) -> ImportJob:
        job = db.get(ImportJob, job_id)
        if job is None or job.user_id != user_id:
            raise HTTPException(status_code=404, detail="Importação não encontrada.")
        return job

    def get_response(self, db: Session, job_id: int, user_id: int) -> ImportJobResponse:
        return job_response(self.get(db, job_id, user_id))

    def items(
        self,
        db: Session,
        job_id: int,
        user_id: int,
        filter_name: str = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> ImportJobItemsResponse:
        job = self.get(db, job_id, user_id)
        condition = _filter_condition(filter_name)

        base = select(ImportJobItem).where(ImportJobItem.job_id == job.id)
        count_query = select(func.count()).select_from(ImportJobItem).where(
            ImportJobItem.job_id == job.id
        )
        if condition is not None:
            base = base.where(condition)
            count_query = count_query.where(condition)

        total = db.execute(count_query).scalar() or 0
        page = min(max(limit, 1), 200)
        items = (
            db.execute(
                base.order_by(ImportJobItem.id)
                .offset(max(offset, 0))
                .limit(page)
            )
            .scalars()
            .all()
        )
        return ImportJobItemsResponse(
            items=[item_response(item) for item in items],
            total=total,
            offset=max(offset, 0),
            limit=page,
            counts=item_counts_for_filter(db, job.id),
        )

    def patch_item(
        self,
        db: Session,
        job_id: int,
        item_id: int,
        user_id: int,
        data: ImportJobPatch,
    ) -> ImportJobItemResponse:
        job = self.get(db, job_id, user_id)
        if job.status != "ready":
            raise ValueError(
                "A importação ainda não está pronta para revisão. Aguarde o matching."
            )

        item = db.get(ImportJobItem, item_id)
        if item is None or item.job_id != job.id:
            raise HTTPException(status_code=404, detail="Item da importação não encontrado.")

        if "selected" in data.model_fields_set:
            item.selected_json = candidate_to_json(data.selected)
        if data.ignored is not None:
            item.ignored = data.ignored
            _persist_suwayomi_ignore(db, user_id, item, data.ignored)

        db.flush()
        set_counts(db, job)
        db.commit()
        db.refresh(item)
        return item_response(item)

    def commit(
        self,
        db: Session,
        job_id: int,
        user_id: int,
        data: ImportJobCommitRequest,
    ) -> ImportCommitResponse:
        job = self.get(db, job_id, user_id)
        if job.status != "ready":
            raise ValueError("A importação não está pronta para ser confirmada.")

        items = (
            db.execute(
                select(ImportJobItem)
                .where(ImportJobItem.job_id == job.id)
                .order_by(ImportJobItem.id)
            )
            .scalars()
            .all()
        )

        entries = []
        for item in items:
            if item.ignored and item.source == "suwayomi":
                # Ignorados manuais não entram no commit, mas precisam ficar
                # registrados para o sync não tentar criá-los depois.
                if item.match_status == "already_in_library":
                    _persist_suwayomi_existing(db, user_id, item)
                else:
                    _persist_suwayomi_ignore(db, user_id, item, True)
                continue
            entry = commit_entry(item, data.add_to_backlog)
            if entry is not None:
                entries.append(entry)

        if not entries:
            raise ValueError(
                "Nenhum item selecionado. Escolha ao menos um match antes de importar."
            )

        response = committer.commit(
            db, ImportCommitRequest(user_id=user_id, entries=entries)
        )

        outcome_by_key = {item.key: item for item in response.items}
        for item in items:
            result = outcome_by_key.get(item.key)
            if result is None:
                continue
            item.commit_outcome = result.outcome
            item.commit_reason = result.reason
            item.logs_created = result.logs_created
            item.backlog_added = result.backlog_added

        job.status = "done"
        job.finished_at = utcnow()
        job.progress_done = job.progress_total
        job.summary_json = json.dumps(
            response.model_dump(mode="json", by_alias=True), ensure_ascii=False
        )
        db.flush()
        set_counts(db, job)
        db.commit()
        return response

    def cancel(self, db: Session, job_id: int, user_id: int) -> ImportJobResponse:
        job = self.get(db, job_id, user_id)

        if job.status == "queued":
            job.status = "cancelled"
            job.cancel_requested = True
            job.finished_at = utcnow()
            delete_job_file(job)
        elif job.status in ("parsing", "matching"):
            job.cancel_requested = True
            job.status = "cancelled"
            job.finished_at = utcnow()
        elif job.status == "ready":
            job.status = "cancelled"
            job.cancel_requested = True
            job.finished_at = utcnow()
        else:
            raise ValueError("Esta importação não pode mais ser cancelada.")

        db.commit()
        db.refresh(job)
        return job_response(job)

    def delete(self, db: Session, job_id: int, user_id: int) -> None:
        job = self.get(db, job_id, user_id)
        if job.status in ("parsing", "matching"):
            raise ValueError("Cancele a importação antes de excluí-la.")
        delete_job_file(job)
        db.query(ImportJobItem).filter(ImportJobItem.job_id == job.id).delete()
        db.delete(job)
        db.commit()


service = ImportJobService()
