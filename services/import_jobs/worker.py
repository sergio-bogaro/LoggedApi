"""Worker global das importações em background.

Um job por vez (respeitando os rate limits das APIs de metadata). O parse e o
matching são persistidos em `import_job_items`, então um reinício da API
retoma o job sem refazer o que já foi casado.
"""

import asyncio
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal
from models.import_job import ImportJob, ImportJobItem
from schemas.import_schemas import ImportMatchRequest
from services.credentials_service import resolve_igdb_credentials, resolve_tmdb_key
from services.importer.matcher import matcher
from services.importer.registry import get_provider
from services.import_jobs.serializers import (
    candidate_to_json,
    job_summary_counts,
    match_request_item,
    set_counts,
    store_external_refs,
    store_logs,
)
from services.import_jobs.storage import delete_job_file

_MATCH_BATCH = 20
_TICK_SECONDS = 2

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_file_sync(provider, filename: str, raw: bytes):
    return asyncio.run(provider.parse_file(filename, raw))


class ImportJobWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        if not settings.import_job_worker_enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._requeue_interrupted()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="import-job-worker")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            # O job atual é retomado no próximo start (status parsing/matching
            # volta para queued). Cancelar evita travar o shutdown por minutos.
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _requeue_interrupted(self) -> None:
        db = SessionLocal()
        try:
            db.query(ImportJob).filter(
                ImportJob.status.in_(["parsing", "matching"])
            ).update({"status": "queued"}, synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def _next_queued_id(self) -> int | None:
        db = SessionLocal()
        try:
            return db.execute(
                select(ImportJob.id)
                .where(ImportJob.status == "queued")
                .where(ImportJob.cancel_requested.is_(False))
                .order_by(ImportJob.created_at, ImportJob.id)
                .limit(1)
            ).scalar_one_or_none()
        finally:
            db.close()

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — o loop não pode morrer
                print(f"Import job worker: {exc}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        job_id = self._next_queued_id()
        if job_id is not None:
            await self.process(job_id)

    async def process(self, job_id: int) -> None:
        """Processa um job do início ao matching (usado pelo worker e em testes)."""
        db = SessionLocal()
        try:
            job = db.get(ImportJob, job_id)
            if job is None or job.status != "queued":
                return

            if job.cancel_requested:
                self._mark_cancelled(db, job)
                return

            job.status = "parsing"
            job.started_at = utcnow()
            job.error = None
            db.commit()

            provider = get_provider(job.provider)
            await self._parse(db, job, provider)
            await self._match(db, job, provider)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reportado no job
            traceback.print_exc()
            db.rollback()
            job = db.get(ImportJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = utcnow()
                delete_job_file(job)
                db.commit()
        finally:
            db.close()

    async def _parse(self, db: Session, job: ImportJob, provider) -> None:
        item_count = (
            db.execute(
                select(func.count())
                .select_from(ImportJobItem)
                .where(ImportJobItem.job_id == job.id)
            ).scalar()
            or 0
        )
        # Retomada: os itens já foram parseados; segue direto ao matching.
        if item_count > 0 and job.progress_total > 0:
            job.status = "matching"
            db.commit()
            return

        if job.input_type == "file":
            if not job.file_path or not Path(job.file_path).exists():
                raise ValueError("O arquivo da importação não está mais disponível.")
            raw = Path(job.file_path).read_bytes()
            preview = await asyncio.to_thread(
                _parse_file_sync, provider, job.file_name or "", raw
            )
        elif job.input_type == "url":
            preview = await provider.parse_url(
                job.source_url or "", job.media_type, db, job.user_id
            )
        elif job.input_type == "connection":
            preview = await provider.parse_connection(
                db, job.user_id, job.media_type
            )
        else:
            preview = await provider.parse_username(
                job.source_username or "", job.media_type
            )

        if not preview.entries:
            raise ValueError("Nenhum item encontrado na fonte da importação.")

        db.refresh(job)
        if job.cancel_requested:
            db.rollback()
            return

        for entry in preview.entries:
            logs_json, logs_count = store_logs(entry.logs)
            db.add(
                ImportJobItem(
                    job_id=job.id,
                    key=entry.key,
                    media_type=entry.media_type,
                    title=entry.title,
                    year=entry.year,
                    overview=entry.overview,
                    source=entry.source,
                    external_refs_json=store_external_refs(entry.external_refs),
                    status=entry.status,
                    rating=entry.rating,
                    review=entry.review,
                    logs_json=logs_json,
                    logs_count=logs_count,
                    in_backlog=entry.in_backlog,
                    backlog_date=entry.backlog_date,
                )
            )

        db.refresh(job)
        if job.cancel_requested:
            db.rollback()
            return

        job.progress_total = len(preview.entries)
        job.progress_done = 0
        job.counts_json = json.dumps(job_summary_counts(preview.counts), ensure_ascii=False)
        job.status = "matching"
        db.commit()
        delete_job_file(job)
        db.commit()

    async def _match(self, db: Session, job: ImportJob, provider) -> None:
        tmdb_key = resolve_tmdb_key(db, job.user_id)
        igdb_credentials = resolve_igdb_credentials(db, job.user_id)

        while True:
            db.refresh(job)
            if job.cancel_requested:
                self._mark_cancelled(db, job)
                return

            pending = (
                db.execute(
                    select(ImportJobItem)
                    .where(ImportJobItem.job_id == job.id)
                    .where(ImportJobItem.match_status.is_(None))
                    .order_by(ImportJobItem.id)
                    .limit(_MATCH_BATCH)
                )
                .scalars()
                .all()
            )
            if not pending:
                break

            response = await matcher.match(
                db,
                ImportMatchRequest(
                    user_id=job.user_id,
                    items=[match_request_item(item) for item in pending],
                ),
                tmdb_key,
                igdb_credentials,
            )
            by_key = {result.key: result for result in response.results}

            for item in pending:
                result = by_key.get(item.key)
                if result is None:
                    item.match_status = "error"
                    item.match_error = "O matcher não retornou resultado para este item."
                    continue
                item.match_status = result.status
                item.match_json = candidate_to_json(result.match)
                item.candidates_json = json.dumps(
                    [candidate.model_dump(mode="json") for candidate in result.candidates],
                    ensure_ascii=False,
                )
                item.existing_media_id = result.existing_media_id
                item.match_error = result.error
                if provider.skip_existing and result.status == "already_in_library":
                    item.ignored = True

            db.flush()
            job.progress_done = (
                db.execute(
                    select(func.count())
                    .select_from(ImportJobItem)
                    .where(ImportJobItem.job_id == job.id)
                    .where(ImportJobItem.match_status.is_not(None))
                ).scalar()
                or 0
            )
            set_counts(db, job)
            db.commit()

        db.refresh(job)
        if job.cancel_requested:
            self._mark_cancelled(db, job)
            return

        db.flush()
        job.progress_done = job.progress_total
        set_counts(db, job)
        job.status = "ready"
        job.finished_at = utcnow()
        db.commit()

    @staticmethod
    def _mark_cancelled(db: Session, job: ImportJob) -> None:
        job.status = "cancelled"
        job.cancel_requested = True
        job.finished_at = utcnow()
        delete_job_file(job)
        db.commit()


worker = ImportJobWorker()
