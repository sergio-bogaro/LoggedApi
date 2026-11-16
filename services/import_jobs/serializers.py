"""Conversão entre linhas do banco, schemas da API e entradas do importador."""

import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.import_job import ImportJob, ImportJobItem
from schemas.import_job import ImportJobItemResponse, ImportJobResponse
from schemas.import_schemas import (
    ImportCandidate,
    ImportCommitEntry,
    ImportLogEntry,
    ImportMatchRequestItem,
)

_EMPTY_CANDIDATES: list[ImportCandidate] = []


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def candidate_to_json(candidate: ImportCandidate | None) -> str | None:
    if candidate is None:
        return None
    return _dumps(candidate.model_dump(mode="json"))


def candidate_from_json(value: str | None) -> ImportCandidate | None:
    data = _loads(value, None)
    if not isinstance(data, dict):
        return None
    try:
        return ImportCandidate.model_validate(data)
    except ValueError:
        return None


def candidates_from_json(value: str | None) -> list[ImportCandidate]:
    data = _loads(value, [])
    if not isinstance(data, list):
        return list(_EMPTY_CANDIDATES)
    result: list[ImportCandidate] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            result.append(ImportCandidate.model_validate(entry))
        except ValueError:
            continue
    return result


def logs_from_json(value: str | None) -> list[ImportLogEntry]:
    data = _loads(value, [])
    if not isinstance(data, list):
        return []
    result: list[ImportLogEntry] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            result.append(ImportLogEntry.model_validate(entry))
        except ValueError:
            continue
    return result


def external_refs_from_json(value: str | None) -> dict[str, str]:
    data = _loads(value, {})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(item) for key, item in data.items() if item is not None}


def job_response(job: ImportJob) -> ImportJobResponse:
    return ImportJobResponse(
        id=job.id,
        provider=job.provider,
        media_type=job.media_type,
        input_type=job.input_type,
        status=job.status,
        progress_done=job.progress_done,
        progress_total=job.progress_total,
        counts=_loads(job.counts_json, {}),
        summary=_loads(job.summary_json, None),
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def item_response(item: ImportJobItem) -> ImportJobItemResponse:
    match = candidate_from_json(item.match_json)
    selected = candidate_from_json(item.selected_json) or match
    return ImportJobItemResponse(
        id=item.id,
        key=item.key,
        media_type=item.media_type,
        title=item.title,
        year=item.year,
        overview=item.overview,
        source=item.source,
        external_refs=external_refs_from_json(item.external_refs_json),
        status=item.status,
        rating=item.rating,
        review=item.review,
        logs=logs_from_json(item.logs_json),
        in_backlog=item.in_backlog,
        backlog_date=item.backlog_date,
        match_status=item.match_status,
        match=match,
        candidates=candidates_from_json(item.candidates_json),
        existing_media_id=item.existing_media_id,
        match_error=item.match_error,
        selected=selected,
        ignored=item.ignored,
        commit_outcome=item.commit_outcome,
        commit_reason=item.commit_reason,
        logs_created=item.logs_created,
        backlog_added=item.backlog_added,
    )


def match_request_item(item: ImportJobItem) -> ImportMatchRequestItem:
    return ImportMatchRequestItem(
        key=item.key,
        title=item.title,
        year=item.year,
        overview=item.overview,
        media_type=item.media_type,
        external_refs=external_refs_from_json(item.external_refs_json),
    )


def commit_entry(
    item: ImportJobItem, add_to_backlog: bool
) -> ImportCommitEntry | None:
    """Monta a entrada de commit a partir do candidato efetivamente escolhido."""
    candidate = candidate_from_json(item.selected_json) or candidate_from_json(
        item.match_json
    )
    if candidate is None or item.ignored:
        return None
    return ImportCommitEntry(
        key=item.key,
        media_type=item.media_type,
        external_id=candidate.external_id,
        title=candidate.title,
        year=item.year,
        cover_url=candidate.cover_url,
        overview=candidate.overview or item.overview,
        release_date=candidate.release_date,
        status=item.status,
        rating=item.rating,
        review=item.review,
        logs=logs_from_json(item.logs_json),
        add_to_backlog=bool(add_to_backlog and item.in_backlog),
        backlog_date=item.backlog_date,
        source=item.source,
        external_refs=external_refs_from_json(item.external_refs_json),
    )


def _has_review(review: str | None) -> bool:
    return bool(review and review.strip())


def compute_counts(db: Session, job_id: int) -> dict[str, int]:
    rows = db.execute(
        select(
            ImportJobItem.match_status,
            ImportJobItem.ignored,
            ImportJobItem.logs_count,
            ImportJobItem.rating,
            ImportJobItem.review,
            ImportJobItem.in_backlog,
            ImportJobItem.selected_json,
            ImportJobItem.match_json,
            ImportJobItem.commit_outcome,
        ).where(ImportJobItem.job_id == job_id)
    ).all()

    counts = {
        "total": 0,
        "withLogs": 0,
        "rated": 0,
        "reviewed": 0,
        "backlog": 0,
        "matched": 0,
        "ambiguous": 0,
        "notFound": 0,
        "alreadyInLibrary": 0,
        "errors": 0,
        "ignored": 0,
        "selected": 0,
    }

    for (
        match_status,
        ignored,
        logs_count,
        rating,
        review,
        in_backlog,
        selected_json,
        match_json,
        commit_outcome,
    ) in rows:
        counts["total"] += 1
        if logs_count:
            counts["withLogs"] += 1
        if rating is not None:
            counts["rated"] += 1
        if _has_review(review):
            counts["reviewed"] += 1
        if in_backlog:
            counts["backlog"] += 1
        if match_status == "matched":
            counts["matched"] += 1
        elif match_status == "ambiguous":
            counts["ambiguous"] += 1
        elif match_status == "not_found":
            counts["notFound"] += 1
        elif match_status == "already_in_library":
            counts["alreadyInLibrary"] += 1
        if match_status == "error" or commit_outcome == "failed":
            counts["errors"] += 1
        if ignored:
            counts["ignored"] += 1
        elif selected_json or match_json:
            counts["selected"] += 1

    return counts


def job_summary_counts(preview_counts) -> dict[str, int]:
    """Contadores iniciais logo após o parse (antes do matching)."""
    return {
        "total": preview_counts.total,
        "withLogs": preview_counts.with_logs,
        "rated": preview_counts.rated,
        "reviewed": preview_counts.reviewed,
        "backlog": preview_counts.backlog,
        "matched": 0,
        "ambiguous": 0,
        "notFound": 0,
        "alreadyInLibrary": 0,
        "errors": 0,
        "ignored": 0,
        "selected": 0,
    }


def set_counts(db: Session, job: ImportJob) -> dict[str, int]:
    counts = compute_counts(db, job.id)
    job.counts_json = _dumps(counts)
    return counts


def item_counts_for_filter(db: Session, job_id: int) -> dict[str, int]:
    """Contadores usados pela barra de filtros da revisão."""
    counts = compute_counts(db, job_id)
    return {
        "all": counts["total"],
        "log": counts["withLogs"],
        "backlog": counts["backlog"],
        "validated": counts["matched"] + counts["alreadyInLibrary"],
        "attention": counts["ambiguous"],
        "errors": counts["notFound"] + counts["errors"],
        "ignored": counts["ignored"],
    }


def store_logs(logs: list[ImportLogEntry]) -> tuple[str, int]:
    return _dumps([log.model_dump(mode="json") for log in logs]), len(logs)


def store_external_refs(refs: dict[str, str]) -> str:
    return _dumps(refs or {})


__all__ = [
    "candidate_from_json",
    "candidate_to_json",
    "candidates_from_json",
    "commit_entry",
    "compute_counts",
    "external_refs_from_json",
    "item_counts_for_filter",
    "item_response",
    "job_response",
    "job_summary_counts",
    "logs_from_json",
    "match_request_item",
    "set_counts",
    "store_external_refs",
    "store_logs",
]
