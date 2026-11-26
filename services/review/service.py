"""Serviço da fila de revisão unificada.

A Fase 3A mantém as tabelas originais (`import_job_items` e
`library_sync_suggestions`) e expõe as duas fontes com um contrato comum.
As ações delegam para os serviços existentes, preservando o comportamento
atual dos jobs manuais e do sync.
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.enums import MediaTypeEnum
from models.import_job import ImportJob, ImportJobItem
from models.library_sync import LibrarySyncSuggestion
from schemas.import_job import ImportJobCommitRequest, ImportJobPatch
from schemas.review import (
    ReviewCommitResponse,
    ReviewCommitResult,
    ReviewCounts,
    ReviewItem,
    ReviewItemsResponse,
)
from services.import_jobs.serializers import (
    candidate_from_json,
    candidates_from_json,
    external_refs_from_json,
    logs_from_json,
    set_counts,
)
from services.import_jobs.service import (
    _persist_suwayomi_ignore,
    service as import_job_service,
)
from services.library_sync.suggestions import (
    _apply_suggestion_candidate,
    _apply_suggestion_state,
    approve_suggestions,
    candidate_for_suggestion,
    candidates_from_json as sync_candidates_from_json,
    suggestion_logs,
)
from domain_errors import DomainError

ReviewSourceFilter = Literal["manual", "sync", "all"]
ReviewStateFilter = Literal["pending", "approved", "dismissed", "failed", "all"]
ReviewMatchFilter = Literal[
    "ready", "needs_validation", "no_match", "already_in_library", "all"
]

_MANUAL_JOB_STATUSES = ("ready", "done")


def parse_key(key: str) -> tuple[str, int]:
    source, _, raw = (key or "").partition(":")
    if source not in ("manual", "sync"):
        raise DomainError(f"Item de revisão inválido: '{key}'.")
    try:
        item_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"Item de revisão inválido: '{key}'.") from exc
    return source, item_id


def manual_key(item_id: int) -> str:
    return f"manual:{item_id}"


def sync_key(suggestion_id: int) -> str:
    return f"sync:{suggestion_id}"


# ── adaptação ────────────────────────────────────────────────────────────────


def _manual_match(item: ImportJobItem) -> str:
    if item.existing_media_id is not None:
        return "already_in_library"
    if item.selected_json:
        return "ready"
    if item.match_status == "matched":
        return "ready"
    if item.match_status == "ambiguous":
        return "needs_validation"
    return "no_match"


def _manual_state(item: ImportJobItem) -> str:
    if item.ignored:
        return "dismissed"
    if item.commit_outcome == "failed":
        return "failed"
    if item.commit_outcome:
        return "approved"
    return "pending"


def _manual_action(item: ImportJobItem) -> str:
    if item.in_backlog and item.status is None:
        return "backlog"
    if item.status is not None:
        return item.status.value
    return "ignore"


def _manual_item(item: ImportJobItem, job: ImportJob) -> ReviewItem:
    logs = logs_from_json(item.logs_json)
    match = candidate_from_json(item.match_json)
    selected = candidate_from_json(item.selected_json) or match
    candidates = candidates_from_json(item.candidates_json)
    refs = external_refs_from_json(item.external_refs_json)
    watched = max((log.date for log in logs), default=None)

    return ReviewItem(
        key=manual_key(item.id),
        source="manual",
        provider=job.provider,
        media_type=item.media_type,
        title=item.title,
        year=item.year,
        overview=(selected.overview if selected else None) or item.overview,
        source_url=refs.get("sourceUrl"),
        cover_url=(selected.cover_url if selected else None),
        external_refs=refs,
        action=_manual_action(item),
        target_status=item.status,
        rating=item.rating,
        review=item.review,
        watched_date=watched,
        logs=logs,
        in_backlog=item.in_backlog,
        backlog_date=item.backlog_date,
        candidate=match,
        candidates=candidates,
        selected=selected,
        match=_manual_match(item),
        error=item.match_error,
        state=_manual_state(item),
        ignored=item.ignored,
        job_id=job.id,
        job_status=job.status,
        origin="manual",
        commit_outcome=item.commit_outcome,
        commit_reason=item.commit_reason,
        logs_created=item.logs_created,
        backlog_added=item.backlog_added,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _sync_match(suggestion: LibrarySyncSuggestion) -> str:
    if suggestion.media_id is not None:
        return "already_in_library"
    candidate = candidate_for_suggestion(suggestion)
    if candidate is None:
        return "no_match"
    if suggestion.error:
        return "needs_validation"
    return "ready"


def _sync_item(suggestion: LibrarySyncSuggestion) -> ReviewItem:
    candidate = candidate_for_suggestion(suggestion)
    selected = candidate_from_json(suggestion.candidate_json) or candidate
    candidates = sync_candidates_from_json(suggestion.candidates_json)
    refs = external_refs_from_json(suggestion.source_refs_json)
    logs = suggestion_logs(suggestion)

    return ReviewItem(
        key=sync_key(suggestion.id),
        source="sync",
        provider=suggestion.provider,
        media_type=suggestion.media_type,
        title=suggestion.title,
        year=suggestion.year,
        overview=(selected.overview if selected else None),
        source_url=suggestion.source_url,
        cover_url=(selected.cover_url if selected else None),
        external_refs=refs,
        action=suggestion.action,
        target_status=suggestion.target_status,
        rating=suggestion.rating,
        review=suggestion.review,
        watched_date=suggestion.watched_date,
        logs=logs,
        in_backlog=suggestion.action == "backlog",
        backlog_date=None,
        candidate=candidate,
        candidates=candidates,
        selected=selected,
        match=_sync_match(suggestion),
        error=suggestion.error,
        state=suggestion.review_state,
        ignored=suggestion.review_state == "dismissed",
        sync_run_at=suggestion.sync_run_at,
        origin=suggestion.origin,
        created_at=suggestion.created_at,
        updated_at=suggestion.updated_at,
    )


# ── condições SQL ────────────────────────────────────────────────────────────


def _manual_base(
    user_id: int,
    provider: str | None,
    media_type: MediaTypeEnum | None,
    search: str | None,
) -> list:
    conditions = [
        ImportJob.user_id == user_id,
        ImportJob.status.in_(_MANUAL_JOB_STATUSES),
    ]
    if provider:
        conditions.append(ImportJob.provider == provider)
    if media_type is not None:
        conditions.append(ImportJobItem.media_type == media_type)
    if search:
        conditions.append(ImportJobItem.title.ilike(f"%{search}%"))
    return conditions


def _sync_base(
    user_id: int,
    provider: str | None,
    media_type: MediaTypeEnum | None,
    search: str | None,
) -> list:
    conditions = [LibrarySyncSuggestion.user_id == user_id]
    if provider:
        conditions.append(LibrarySyncSuggestion.provider == provider)
    if media_type is not None:
        conditions.append(LibrarySyncSuggestion.media_type == media_type)
    if search:
        conditions.append(LibrarySyncSuggestion.title.ilike(f"%{search}%"))
    return conditions


def _manual_state_conditions(state: str) -> list:
    if state == "pending":
        return [
            ImportJob.status == "ready",
            ImportJobItem.ignored.is_(False),
            ImportJobItem.commit_outcome.is_(None),
        ]
    if state == "approved":
        return [
            ImportJobItem.commit_outcome.is_not(None),
            ImportJobItem.commit_outcome != "failed",
        ]
    if state == "dismissed":
        return [ImportJobItem.ignored.is_(True)]
    if state == "failed":
        return [ImportJobItem.commit_outcome == "failed"]
    return []


def _sync_state_conditions(state: str) -> list:
    if state in ("pending", "approved", "dismissed"):
        return [LibrarySyncSuggestion.review_state == state]
    if state == "failed":
        return [LibrarySyncSuggestion.id == -1]
    return []


def _manual_match_conditions(match: str) -> list:
    if match == "already_in_library":
        return [ImportJobItem.existing_media_id.is_not(None)]
    if match == "ready":
        return [
            ImportJobItem.existing_media_id.is_(None),
            or_(
                ImportJobItem.match_status == "matched",
                ImportJobItem.selected_json.is_not(None),
            ),
        ]
    if match == "needs_validation":
        return [
            ImportJobItem.match_status == "ambiguous",
            ImportJobItem.selected_json.is_(None),
        ]
    if match == "no_match":
        return [
            or_(
                ImportJobItem.match_status.in_(("not_found", "error")),
                ImportJobItem.match_status.is_(None),
            ),
            ImportJobItem.selected_json.is_(None),
        ]
    return []


def _sync_match_conditions(match: str) -> list:
    if match == "already_in_library":
        return [LibrarySyncSuggestion.media_id.is_not(None)]
    if match == "ready":
        return [
            LibrarySyncSuggestion.candidate_json.is_not(None),
            or_(
                LibrarySyncSuggestion.error.is_(None),
                LibrarySyncSuggestion.error == "",
            ),
        ]
    if match == "needs_validation":
        return [
            LibrarySyncSuggestion.candidate_json.is_not(None),
            LibrarySyncSuggestion.error.is_not(None),
            LibrarySyncSuggestion.error != "",
        ]
    if match == "no_match":
        return [LibrarySyncSuggestion.candidate_json.is_(None)]
    return []


def _manual_query(conditions: list):
    return (
        select(ImportJobItem, ImportJob)
        .join(ImportJob, ImportJob.id == ImportJobItem.job_id)
        .where(*conditions)
    )


def _sync_query(conditions: list):
    return select(LibrarySyncSuggestion).where(*conditions)


def _manual_count(db: Session, conditions: list) -> int:
    return (
        db.execute(
            select(func.count())
            .select_from(ImportJobItem)
            .join(ImportJob, ImportJob.id == ImportJobItem.job_id)
            .where(*conditions)
        ).scalar()
        or 0
    )


def _sync_count(db: Session, conditions: list) -> int:
    return (
        db.execute(
            select(func.count())
            .select_from(LibrarySyncSuggestion)
            .where(*conditions)
        ).scalar()
        or 0
    )


def _manual_jobs_count(
    db: Session,
    user_id: int,
    provider: str | None,
    media_type: MediaTypeEnum | None,
) -> int:
    conditions = [
        ImportJob.user_id == user_id,
        ImportJob.status == "ready",
        ImportJobItem.ignored.is_(False),
        ImportJobItem.commit_outcome.is_(None),
    ]
    if provider:
        conditions.append(ImportJob.provider == provider)
    if media_type is not None:
        conditions.append(ImportJobItem.media_type == media_type)
    return (
        db.execute(
            select(func.count(func.distinct(ImportJob.id)))
            .select_from(ImportJob)
            .join(ImportJobItem, ImportJobItem.job_id == ImportJob.id)
            .where(*conditions)
        ).scalar()
        or 0
    )


# ── serviço ──────────────────────────────────────────────────────────────────


class ReviewService:
    def counts(
        self,
        db: Session,
        user_id: int,
        provider: str | None = None,
        media_type: MediaTypeEnum | None = None,
        source: ReviewSourceFilter = "all",
    ) -> ReviewCounts:
        include_manual = source in ("manual", "all")
        include_sync = source in ("sync", "all")
        manual_base = _manual_base(user_id, provider, media_type, None)
        sync_base = _sync_base(user_id, provider, media_type, None)

        state_counts: dict[str, int] = {}
        for state in ("pending", "approved", "dismissed", "failed"):
            total = 0
            if include_manual:
                total += _manual_count(
                    db, manual_base + _manual_state_conditions(state)
                )
            if include_sync:
                total += _sync_count(db, sync_base + _sync_state_conditions(state))
            state_counts[state] = total

        match_counts: dict[str, int] = {}
        for match in ("ready", "needs_validation", "no_match", "already_in_library"):
            total = 0
            if include_manual:
                total += _manual_count(
                    db,
                    manual_base
                    + _manual_state_conditions("pending")
                    + _manual_match_conditions(match),
                )
            if include_sync:
                total += _sync_count(
                    db,
                    sync_base
                    + _sync_state_conditions("pending")
                    + _sync_match_conditions(match),
                )
            match_counts[match] = total

        manual_jobs = (
            _manual_jobs_count(db, user_id, provider, media_type)
            if include_manual
            else 0
        )
        sync_items = (
            _sync_count(db, sync_base + _sync_state_conditions("pending"))
            if include_sync
            else 0
        )

        return ReviewCounts(
            **state_counts,
            **match_counts,
            manual_jobs=manual_jobs,
            sync_items=sync_items,
            pending_tasks=manual_jobs + sync_items,
        )

    def list_items(
        self,
        db: Session,
        user_id: int,
        state: ReviewStateFilter = "pending",
        match: ReviewMatchFilter = "all",
        source: ReviewSourceFilter = "all",
        provider: str | None = None,
        media_type: MediaTypeEnum | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> ReviewItemsResponse:
        include_manual = source in ("manual", "all")
        include_sync = source in ("sync", "all")
        page = min(max(int(limit), 1), 200)
        start = max(int(offset), 0)
        fetch_limit = start + page

        manual_conditions = _manual_base(user_id, provider, media_type, search)
        if state != "all":
            manual_conditions += _manual_state_conditions(state)
        if match != "all":
            manual_conditions += _manual_match_conditions(match)

        sync_conditions = _sync_base(user_id, provider, media_type, search)
        if state != "all":
            sync_conditions += _sync_state_conditions(state)
        if match != "all":
            sync_conditions += _sync_match_conditions(match)

        total = 0
        items: list[ReviewItem] = []

        if include_manual:
            total += _manual_count(db, manual_conditions)
            rows = (
                db.execute(
                    _manual_query(manual_conditions)
                    .order_by(
                        ImportJobItem.updated_at.desc(), ImportJobItem.id.desc()
                    )
                    .limit(fetch_limit)
                )
                .all()
            )
            items.extend(_manual_item(item, job) for item, job in rows)

        if include_sync:
            total += _sync_count(db, sync_conditions)
            suggestions = (
                db.execute(
                    _sync_query(sync_conditions)
                    .order_by(
                        LibrarySyncSuggestion.updated_at.desc(),
                        LibrarySyncSuggestion.id.desc(),
                    )
                    .limit(fetch_limit)
                )
                .scalars()
                .all()
            )
            items.extend(_sync_item(suggestion) for suggestion in suggestions)

        items.sort(
            key=lambda item: (
                item.updated_at or item.created_at or datetime.min,
                item.key,
            ),
            reverse=True,
        )
        page_items = items[start : start + page]
        counts = self.counts(
            db, user_id, provider=provider, media_type=media_type, source=source
        )
        return ReviewItemsResponse(
            items=page_items,
            total=total,
            counts=counts,
            offset=start,
            limit=page,
        )

    def list_keys(
        self,
        db: Session,
        user_id: int,
        state: ReviewStateFilter = "pending",
        match: ReviewMatchFilter = "all",
        source: ReviewSourceFilter = "all",
        provider: str | None = None,
        media_type: MediaTypeEnum | None = None,
        search: str | None = None,
    ) -> list[str]:
        """Chaves de todos os itens selecionáveis que batem com os filtros."""
        include_manual = source in ("manual", "all")
        include_sync = source in ("sync", "all")
        keys: list[str] = []

        if include_manual:
            conditions = _manual_base(user_id, provider, media_type, search)
            if state != "all":
                conditions += _manual_state_conditions(state)
            if match != "all":
                conditions += _manual_match_conditions(match)
            # Espelha o checkbox da UI: só itens com candidato/seleção.
            conditions.append(
                or_(
                    ImportJobItem.selected_json.is_not(None),
                    ImportJobItem.match_json.is_not(None),
                )
            )
            rows = (
                db.execute(
                    select(ImportJobItem.id)
                    .join(ImportJob, ImportJob.id == ImportJobItem.job_id)
                    .where(*conditions)
                )
                .scalars()
                .all()
            )
            keys.extend(manual_key(item_id) for item_id in rows)

        if include_sync:
            conditions = _sync_base(user_id, provider, media_type, search)
            if state != "all":
                conditions += _sync_state_conditions(state)
            if match != "all":
                conditions += _sync_match_conditions(match)
            conditions.append(LibrarySyncSuggestion.candidate_json.is_not(None))
            rows = (
                db.execute(select(LibrarySyncSuggestion.id).where(*conditions))
                .scalars()
                .all()
            )
            keys.extend(sync_key(suggestion_id) for suggestion_id in rows)

        return keys

    def patch_item(
        self,
        db: Session,
        user_id: int,
        key: str,
        selected=None,
        ignored: bool | None = None,
    ) -> ReviewItem:
        source, item_id = parse_key(key)
        if source == "manual":
            item = db.get(ImportJobItem, item_id)
            if item is None:
                raise DomainError("Item de revisão não encontrado.")
            job = db.get(ImportJob, item.job_id)
            if job is None or job.user_id != user_id:
                raise DomainError("Item de revisão não encontrado.")
            if job.status != "ready":
                raise DomainError("Esta importação não está pronta para revisão.")
            import_job_service.patch_item(
                db,
                job.id,
                item.id,
                user_id,
                ImportJobPatch(selected=selected, ignored=ignored),
            )
            db.expire_all()
            item = db.get(ImportJobItem, item_id)
            job = db.get(ImportJob, item.job_id)
            return _manual_item(item, job)

        suggestion = db.get(LibrarySyncSuggestion, item_id)
        if suggestion is None or suggestion.user_id != user_id:
            raise DomainError("Item de revisão não encontrado.")
        if suggestion.review_state == "approved":
            raise DomainError("Este título já foi adicionado à biblioteca.")

        try:
            if selected is not None:
                _apply_suggestion_candidate(db, user_id, suggestion, selected)
            if ignored is not None:
                _apply_suggestion_state(
                    db,
                    user_id,
                    suggestion,
                    "dismissed" if ignored else "pending",
                )
            db.commit()
        except Exception:
            db.rollback()
            raise

        db.expire_all()
        suggestion = db.get(LibrarySyncSuggestion, item_id)
        return _sync_item(suggestion)

    def set_state(
        self, db: Session, user_id: int, keys: list[str], state: str
    ) -> None:
        if not keys:
            raise DomainError("Selecione ao menos um item.")
        if state not in ("pending", "dismissed"):
            raise DomainError("Estado inválido para revisão.")
        manual_ids: list[int] = []
        sync_ids: list[int] = []
        for key in keys:
            source, item_id = parse_key(key)
            (manual_ids if source == "manual" else sync_ids).append(item_id)

        try:
            jobs: dict[int, ImportJob] = {}
            for item_id in manual_ids:
                item = db.get(ImportJobItem, item_id)
                if item is None:
                    raise DomainError("Item de revisão não encontrado.")
                job = db.get(ImportJob, item.job_id)
                if job is None or job.user_id != user_id:
                    raise DomainError("Item de revisão não encontrado.")
                if job.status != "ready":
                    raise DomainError("Esta importação não está pronta para revisão.")
                item.ignored = state == "dismissed"
                _persist_suwayomi_ignore(db, user_id, item, item.ignored)
                jobs[job.id] = job

            for suggestion_id in sync_ids:
                suggestion = db.get(LibrarySyncSuggestion, suggestion_id)
                if suggestion is None or suggestion.user_id != user_id:
                    raise DomainError("Item de revisão não encontrado.")
                _apply_suggestion_state(db, user_id, suggestion, state)

            db.flush()
            for job in jobs.values():
                set_counts(db, job)
            db.commit()
        except Exception:
            db.rollback()
            raise

    def commit(
        self,
        db: Session,
        user_id: int,
        keys: list[str],
        add_to_backlog: bool = True,
    ) -> ReviewCommitResponse:
        if not keys:
            raise DomainError("Selecione ao menos um item.")

        manual_ids: list[int] = []
        sync_ids: list[int] = []
        for key in keys:
            source, item_id = parse_key(key)
            (manual_ids if source == "manual" else sync_ids).append(item_id)

        response = ReviewCommitResponse()

        # Confirma somente os itens marcados. O job permanece em revisão
        # enquanto sobrar item pendente.
        job_item_ids: dict[int, list[int]] = {}
        for item_id in manual_ids:
            item = db.get(ImportJobItem, item_id)
            if item is None:
                raise DomainError("Item de revisão não encontrado.")
            job = db.get(ImportJob, item.job_id)
            if job is None or job.user_id != user_id:
                raise DomainError("Item de revisão não encontrado.")
            if job.status != "ready":
                raise DomainError("Esta importação não está pronta para revisão.")
            job_item_ids.setdefault(job.id, []).append(item.id)

        for job_id, selected_item_ids in job_item_ids.items():
            job_items = (
                db.execute(
                    select(ImportJobItem).where(ImportJobItem.job_id == job_id)
                )
                .scalars()
                .all()
            )
            requested_keys = set(keys)
            selected_keys = {
                manual_key(item.id)
                for item in job_items
                if manual_key(item.id) in requested_keys
            }
            try:
                result = import_job_service.commit(
                    db,
                    job_id,
                    user_id,
                    ImportJobCommitRequest(
                        add_to_backlog=add_to_backlog,
                        item_ids=selected_item_ids,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — reportado por item
                for key in selected_keys:
                    response.failed += 1
                    response.items.append(
                        ReviewCommitResult(
                            key=key,
                            source="manual",
                            outcome="failed",
                            reason=str(exc),
                        )
                    )
                continue

            for item in job_items:
                key = manual_key(item.id)
                result_item = next(
                    (row for row in result.items if row.key == item.key), None
                )
                if result_item is None:
                    # Itens ignorados não entram no commit.
                    continue
                outcome = result_item.outcome
                reason = result_item.reason
                logs_created = result_item.logs_created
                backlog_added = result_item.backlog_added
                response.items.append(
                    ReviewCommitResult(
                        key=key,
                        source="manual",
                        outcome=outcome,
                        reason=reason,
                        logs_created=logs_created,
                        backlog_added=backlog_added,
                    )
                )
                if outcome == "imported":
                    response.imported += 1
                elif outcome == "merged":
                    response.merged += 1
                elif outcome == "skipped":
                    response.skipped += 1
                else:
                    response.failed += 1
                response.logs_created += logs_created
                response.backlog_added += 1 if backlog_added else 0
            response.failures.extend(result.failures)

        if sync_ids:
            suggestions = []
            for suggestion_id in sync_ids:
                suggestion = db.get(LibrarySyncSuggestion, suggestion_id)
                if suggestion is None or suggestion.user_id != user_id:
                    raise DomainError("Item de revisão não encontrado.")
                if suggestion.review_state != "pending":
                    raise DomainError("Este título não está mais pendente.")
                suggestions.append(suggestion)

            result = approve_suggestions(db, user_id, sync_ids)
            db.expire_all()
            for suggestion_id in sync_ids:
                suggestion = db.get(LibrarySyncSuggestion, suggestion_id)
                key = sync_key(suggestion_id)
                if suggestion is not None and suggestion.review_state == "approved":
                    response.imported += 1
                    response.items.append(
                        ReviewCommitResult(
                            key=key,
                            source="sync",
                            outcome="imported",
                        )
                    )
                else:
                    response.failed += 1
                    reason = suggestion.error if suggestion else "Item não processado."
                    response.items.append(
                        ReviewCommitResult(
                            key=key,
                            source="sync",
                            outcome="failed",
                            reason=reason,
                        )
                    )
            response.failures.extend(result.get("failures") or [])

        return response


service = ReviewService()
