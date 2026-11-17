"""Fila de revisão de títulos encontrados pelos syncs de biblioteca."""

import json
from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import (
    LibraryConnection,
    LibrarySyncLink,
    LibrarySyncSuggestion,
)
from schemas.import_schemas import (
    ImportCandidate,
    ImportCommitEntry,
    ImportCommitRequest,
    ImportFailure,
    ImportLogEntry,
)
from services.importer.commit import committer
from services.library_sync.providers.base import SourceItem

_REVIEW_STATES = ("pending", "approved", "dismissed")
_ENDED_STATUSES = (MediaStatusEnum.FINISHED, MediaStatusEnum.DROPPED)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def candidate_to_json(candidate: ImportCandidate | None) -> str | None:
    if candidate is None:
        return None
    return json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False)


def candidate_from_json(value: str | None) -> ImportCandidate | None:
    data = _loads(value, None)
    if not isinstance(data, dict):
        return None
    try:
        return ImportCandidate.model_validate(data)
    except ValueError:
        return None


def suggestion_key(suggestion: LibrarySyncSuggestion) -> str:
    return f"{suggestion.provider}:{suggestion.source_id}"


def suggestion_refs(
    connection: LibraryConnection, item: SourceItem
) -> dict[str, str]:
    refs = dict(item.external_refs or {})
    if item.external_id:
        if item.media_type in (MediaTypeEnum.ANIME, MediaTypeEnum.MANGA):
            refs.setdefault("anilistId", item.external_id)
        elif item.media_type in (MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES):
            refs.setdefault("tmdbId", item.external_id)
        elif item.media_type == MediaTypeEnum.BOOK:
            refs.setdefault("openlibraryId", item.external_id)
    refs["syncProvider"] = connection.provider
    refs["syncSourceId"] = item.source_id
    refs["syncStatus"] = item.status
    if item.source_url:
        refs["sourceUrl"] = item.source_url
    return refs


def load_suggestions(
    db: Session, user_id: int, provider: str
) -> dict[str, LibrarySyncSuggestion]:
    rows = (
        db.execute(
            select(LibrarySyncSuggestion).where(
                LibrarySyncSuggestion.user_id == user_id,
                LibrarySyncSuggestion.provider == provider,
            )
        )
        .scalars()
        .all()
    )
    return {row.source_id: row for row in rows}


def stage_suggestion(
    db: Session,
    connection: LibraryConnection,
    suggestions: dict[str, LibrarySyncSuggestion],
    item: SourceItem,
    *,
    action: str,
    target_status: MediaStatusEnum | None,
    source_label: str,
    candidate: ImportCandidate | None,
    error: str | None,
) -> LibrarySyncSuggestion:
    """Cria ou atualiza uma sugestão pendente para o item."""
    suggestion = suggestions.get(item.source_id)
    if suggestion is None:
        suggestion = LibrarySyncSuggestion(
            user_id=connection.user_id,
            provider=connection.provider,
            source_id=item.source_id,
            media_type=item.media_type,
            title=item.title,
            source_status=item.status,
            review_state="pending",
        )
        db.add(suggestion)
        suggestions[item.source_id] = suggestion

    suggestion.media_type = item.media_type
    suggestion.title = item.title or suggestion.title
    suggestion.year = item.year
    suggestion.source_status = item.status
    suggestion.source_label = source_label
    suggestion.action = action
    suggestion.target_status = target_status
    suggestion.rating = item.rating
    suggestion.review = item.review
    suggestion.watched_date = item.watched_at
    suggestion.source_url = item.source_url
    suggestion.source_refs_json = json.dumps(
        suggestion_refs(connection, item), ensure_ascii=False
    )
    if candidate is not None:
        suggestion.candidate_json = candidate_to_json(candidate)
    if error is not None:
        suggestion.error = error
    db.flush()
    return suggestion


def candidate_for_suggestion(suggestion: LibrarySyncSuggestion) -> ImportCandidate:
    candidate = candidate_from_json(suggestion.candidate_json)
    if candidate is not None:
        return candidate
    refs = _loads(suggestion.source_refs_json, {})
    if not isinstance(refs, dict):
        refs = {}
    external_id = (
        refs.get("tmdbId")
        or refs.get("anilistId")
        or refs.get("openlibraryId")
        or suggestion.source_id
    )
    return ImportCandidate(
        provider=suggestion.provider,
        external_id=str(external_id),
        media_type=suggestion.media_type,
        title=suggestion.title,
        year=suggestion.year,
    )


def suggestion_logs(suggestion: LibrarySyncSuggestion) -> list[ImportLogEntry]:
    if suggestion.watched_date is None or suggestion.target_status is None:
        return []
    start = datetime.combine(suggestion.watched_date, time.min)
    ended = suggestion.target_status in _ENDED_STATUSES
    return [
        ImportLogEntry(
            date=suggestion.watched_date,
            start_date=start,
            end_date=start if ended else None,
            status=suggestion.target_status,
            rating=suggestion.rating,
            review=suggestion.review,
        )
    ]


def suggestion_entry(suggestion: LibrarySyncSuggestion) -> ImportCommitEntry:
    candidate = candidate_for_suggestion(suggestion)
    refs = _loads(suggestion.source_refs_json, {})
    if not isinstance(refs, dict):
        refs = {}
    refs = {str(key): str(value) for key, value in refs.items() if value is not None}
    refs.setdefault("syncProvider", suggestion.provider)
    refs.setdefault("syncSourceId", suggestion.source_id)
    refs.setdefault("syncStatus", suggestion.source_status)
    status = suggestion.target_status if suggestion.action != "backlog" else None
    return ImportCommitEntry(
        key=suggestion_key(suggestion),
        media_type=suggestion.media_type,
        external_id=str(candidate.external_id),
        title=candidate.title or suggestion.title,
        year=candidate.year or suggestion.year,
        cover_url=candidate.cover_url,
        overview=candidate.overview,
        release_date=candidate.release_date,
        status=status,
        rating=suggestion.rating,
        review=suggestion.review,
        logs=suggestion_logs(suggestion),
        add_to_backlog=suggestion.action == "backlog",
        source=suggestion.provider,
        external_refs=refs,
    )


def approve_suggestions(
    db: Session, user_id: int, ids: list[int]
) -> dict:
    if not ids:
        raise ValueError("Selecione ao menos um título para adicionar.")
    suggestions = (
        db.execute(
            select(LibrarySyncSuggestion).where(
                LibrarySyncSuggestion.user_id == user_id,
                LibrarySyncSuggestion.id.in_(ids),
                LibrarySyncSuggestion.review_state == "pending",
            )
        )
        .scalars()
        .all()
    )
    if not suggestions:
        raise ValueError("Nenhuma sugestão pendente foi encontrada.")

    response = committer.commit(
        db,
        ImportCommitRequest(
            user_id=user_id,
            entries=[suggestion_entry(suggestion) for suggestion in suggestions],
        ),
    )
    by_key = {item.key: item for item in response.items}

    approved = 0
    failures: list[ImportFailure] = []
    for suggestion in suggestions:
        result = by_key.get(suggestion_key(suggestion))
        if result is None or result.outcome == "failed":
            suggestion.error = (
                result.reason if result is not None else "Item não processado."
            )
            failures.append(
                ImportFailure(
                    name=suggestion.title,
                    reason=suggestion.error or "Falha ao adicionar.",
                )
            )
            continue

        suggestion.review_state = "approved"
        suggestion.error = None
        suggestion.decided_at = utcnow()
        link = db.execute(
            select(LibrarySyncLink).where(
                LibrarySyncLink.user_id == user_id,
                LibrarySyncLink.provider == suggestion.provider,
                LibrarySyncLink.source_id == suggestion.source_id,
            )
        ).scalar_one_or_none()
        if link is not None:
            suggestion.media_id = link.media_id
        approved += 1

    db.commit()
    return {
        "approved": approved,
        "failed": len(failures),
        "failures": failures,
    }


def set_suggestion_state(
    db: Session, user_id: int, suggestion_id: int, state: str
) -> LibrarySyncSuggestion:
    if state not in ("pending", "dismissed"):
        raise ValueError("Estado inválido para a sugestão.")
    suggestion = db.get(LibrarySyncSuggestion, suggestion_id)
    if suggestion is None or suggestion.user_id != user_id:
        raise ValueError("Sugestão não encontrada.")
    if suggestion.review_state == "approved":
        raise ValueError("Este título já foi adicionado à biblioteca.")

    suggestion.review_state = state
    suggestion.decided_at = utcnow() if state == "dismissed" else None
    if state == "pending":
        suggestion.error = None
    db.commit()
    db.refresh(suggestion)
    return suggestion


def pending_count(db: Session, user_id: int, provider: str | None = None) -> int:
    query = (
        select(func.count())
        .select_from(LibrarySyncSuggestion)
        .where(
            LibrarySyncSuggestion.user_id == user_id,
            LibrarySyncSuggestion.review_state == "pending",
        )
    )
    if provider:
        query = query.where(LibrarySyncSuggestion.provider == provider)
    return db.execute(query).scalar() or 0


def suggestion_counts(
    db: Session, user_id: int, provider: str | None = None
) -> dict[str, int]:
    query = (
        select(LibrarySyncSuggestion.review_state, func.count())
        .where(LibrarySyncSuggestion.user_id == user_id)
        .group_by(LibrarySyncSuggestion.review_state)
    )
    if provider:
        query = query.where(LibrarySyncSuggestion.provider == provider)
    counts = {state: 0 for state in _REVIEW_STATES}
    for state, count in db.execute(query).all():
        counts[state] = count
    return counts
