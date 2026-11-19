"""Fila de revisão de títulos encontrados pelos syncs de biblioteca."""

import json
from datetime import date, datetime, time, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import (
    LibraryConnection,
    LibrarySyncLink,
    LibrarySyncSuggestion,
)
from models.suwayomi import SuwayomiMangaLink
from schemas.import_schemas import (
    ImportCandidate,
    ImportCommitEntry,
    ImportCommitRequest,
    ImportFailure,
    ImportLogEntry,
)
from services.importer.commit import committer
from services.library_sync.providers.base import SourceItem
from domain_errors import DomainError

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


def candidates_to_json(candidates: list[ImportCandidate] | None) -> str:
    return json.dumps(
        [candidate.model_dump(mode="json") for candidate in candidates or []],
        ensure_ascii=False,
    )


def candidates_from_json(value: str | None) -> list[ImportCandidate]:
    data = _loads(value, [])
    if not isinstance(data, list):
        return []
    result: list[ImportCandidate] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            result.append(ImportCandidate.model_validate(entry))
        except ValueError:
            continue
    return result


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


def stage_suggestion_values(
    db: Session,
    suggestions: dict[str, LibrarySyncSuggestion],
    *,
    user_id: int,
    provider: str,
    source_id: str,
    media_type: MediaTypeEnum,
    title: str,
    source_status: str,
    source_label: str,
    action: str,
    target_status: MediaStatusEnum | None,
    candidate: ImportCandidate | None,
    error: str | None,
    year: int | None = None,
    rating: float | None = None,
    review: str | None = None,
    watched_date: date | None = None,
    source_url: str | None = None,
    refs: dict[str, str] | None = None,
    candidates: list[ImportCandidate] | None = None,
    sync_run_at: datetime | None = None,
) -> LibrarySyncSuggestion:
    """Cria ou atualiza uma sugestão pendente de qualquer provedor."""
    suggestion = suggestions.get(source_id)
    if suggestion is None:
        suggestion = LibrarySyncSuggestion(
            user_id=user_id,
            provider=provider,
            source_id=source_id,
            media_type=media_type,
            title=title,
            source_status=source_status,
            review_state="pending",
        )
        db.add(suggestion)
        suggestions[source_id] = suggestion

    # Uma decisão já tomada pelo usuário não é reaberta pelo sync.
    if suggestion.review_state != "pending":
        return suggestion

    suggestion.media_type = media_type
    suggestion.title = title or suggestion.title
    suggestion.year = year
    suggestion.source_status = source_status
    suggestion.source_label = source_label
    suggestion.action = action
    suggestion.target_status = target_status
    suggestion.rating = rating
    suggestion.review = review
    suggestion.watched_date = watched_date
    suggestion.source_url = source_url
    suggestion.source_refs_json = json.dumps(refs or {}, ensure_ascii=False)
    if candidates is not None:
        suggestion.candidates_json = candidates_to_json(candidates)
    if sync_run_at is not None:
        suggestion.sync_run_at = sync_run_at
    if candidate is not None:
        suggestion.candidate_json = candidate_to_json(candidate)
        suggestion.error = error
    elif suggestion.candidate_json is None:
        suggestion.error = error
    db.flush()
    return suggestion


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
    candidates: list[ImportCandidate] | None = None,
    sync_run_at: datetime | None = None,
) -> LibrarySyncSuggestion:
    """Cria ou atualiza uma sugestão pendente para o item genérico."""
    return stage_suggestion_values(
        db,
        suggestions,
        user_id=connection.user_id,
        provider=connection.provider,
        source_id=item.source_id,
        media_type=item.media_type,
        title=item.title,
        source_status=item.status,
        source_label=source_label,
        action=action,
        target_status=target_status,
        candidate=candidate,
        error=error,
        year=item.year,
        rating=item.rating,
        review=item.review,
        watched_date=item.watched_at,
        source_url=item.source_url,
        refs=suggestion_refs(connection, item),
        candidates=candidates,
        sync_run_at=sync_run_at,
    )


def record_applied_suggestion(
    db: Session,
    suggestions: dict[str, LibrarySyncSuggestion],
    *,
    user_id: int,
    provider: str,
    source_id: str,
    media_id: int,
    media_type: MediaTypeEnum,
    title: str,
    source_status: str,
    source_label: str,
    action: str,
    target_status: MediaStatusEnum | None,
    candidate: ImportCandidate | None,
    candidates: list[ImportCandidate] | None = None,
    origin: str = "auto",
    year: int | None = None,
    rating: float | None = None,
    review: str | None = None,
    watched_date: date | None = None,
    source_url: str | None = None,
    refs: dict[str, str] | None = None,
    sync_run_at: datetime | None = None,
) -> LibrarySyncSuggestion:
    """Registra no histórico um item que já entrou na biblioteca."""
    suggestion = suggestions.get(source_id)
    if suggestion is None:
        suggestion = LibrarySyncSuggestion(
            user_id=user_id,
            provider=provider,
            source_id=source_id,
            media_type=media_type,
            title=title,
            source_status=source_status,
            review_state="approved",
        )
        db.add(suggestion)
        suggestions[source_id] = suggestion

    suggestion.media_type = media_type
    suggestion.title = title or suggestion.title
    suggestion.year = year
    suggestion.source_status = source_status
    suggestion.source_label = source_label
    suggestion.action = action
    suggestion.target_status = target_status
    suggestion.rating = rating
    suggestion.review = review
    suggestion.watched_date = watched_date
    suggestion.source_url = source_url
    suggestion.source_refs_json = json.dumps(refs or {}, ensure_ascii=False)
    suggestion.candidates_json = candidates_to_json(candidates)
    if candidate is not None:
        suggestion.candidate_json = candidate_to_json(candidate)
    suggestion.review_state = "approved"
    suggestion.origin = origin
    suggestion.sync_run_at = sync_run_at
    suggestion.media_id = media_id
    suggestion.error = None
    suggestion.decided_at = utcnow()
    db.flush()
    return suggestion


def candidate_for_suggestion(
    suggestion: LibrarySyncSuggestion,
) -> ImportCandidate | None:
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
    )
    if not external_id:
        return None
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


def suggestion_entry(
    suggestion: LibrarySyncSuggestion,
) -> ImportCommitEntry | None:
    candidate = candidate_for_suggestion(suggestion)
    if candidate is None:
        return None
    refs = _loads(suggestion.source_refs_json, {})
    if not isinstance(refs, dict):
        refs = {}
    refs = {str(key): str(value) for key, value in refs.items() if value is not None}
    if suggestion.provider != "suwayomi":
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
        raise DomainError("Selecione ao menos um título para adicionar.")
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
        raise DomainError("Nenhuma sugestão pendente foi encontrada.")

    entries: list[tuple[LibrarySyncSuggestion, ImportCommitEntry]] = []
    failures: list[ImportFailure] = []
    for suggestion in suggestions:
        entry = suggestion_entry(suggestion)
        if entry is None:
            suggestion.error = "Escolha um match antes de adicionar."
            failures.append(
                ImportFailure(
                    name=suggestion.title,
                    reason=suggestion.error,
                )
            )
            continue
        entries.append((suggestion, entry))

    by_key = {}
    if entries:
        response = committer.commit(
            db,
            ImportCommitRequest(
                user_id=user_id,
                entries=[entry for _suggestion, entry in entries],
            ),
        )
        by_key = {item.key: item for item in response.items}

    approved = 0
    for suggestion, _entry in entries:
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
        suggestion.origin = "review"
        suggestion.error = None
        suggestion.decided_at = utcnow()
        if suggestion.provider == "suwayomi":
            try:
                suwayomi_id = int(suggestion.source_id)
            except (TypeError, ValueError):
                suwayomi_id = None
            link = None
            if suwayomi_id is not None:
                link = db.execute(
                    select(SuwayomiMangaLink).where(
                        SuwayomiMangaLink.user_id == user_id,
                        SuwayomiMangaLink.suwayomi_manga_id == suwayomi_id,
                    )
                ).scalar_one_or_none()
        else:
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
        raise DomainError("Estado inválido para a sugestão.")
    suggestion = db.get(LibrarySyncSuggestion, suggestion_id)
    if suggestion is None or suggestion.user_id != user_id:
        raise DomainError("Sugestão não encontrada.")
    if suggestion.review_state == "approved":
        raise DomainError("Este título já foi adicionado à biblioteca.")

    suggestion.review_state = state
    suggestion.decided_at = utcnow() if state == "dismissed" else None
    if state == "pending":
        suggestion.error = None

    if suggestion.provider == "suwayomi":
        try:
            suwayomi_id = int(suggestion.source_id)
        except (TypeError, ValueError):
            suwayomi_id = None
        link = None
        if suwayomi_id is not None:
            link = db.execute(
                select(SuwayomiMangaLink).where(
                    SuwayomiMangaLink.user_id == user_id,
                    SuwayomiMangaLink.suwayomi_manga_id == suwayomi_id,
                )
            ).scalar_one_or_none()
        if link is not None and link.media_id is None:
            link.ignored = state == "dismissed"
            link.match_error = None
            link.last_match_attempt_at = None
            link.match_attempts = 0

    db.commit()
    db.refresh(suggestion)
    return suggestion


def set_suggestion_candidate(
    db: Session,
    user_id: int,
    suggestion_id: int,
    candidate: ImportCandidate,
) -> LibrarySyncSuggestion:
    suggestion = db.get(LibrarySyncSuggestion, suggestion_id)
    if suggestion is None or suggestion.user_id != user_id:
        raise DomainError("Sugestão não encontrada.")
    if suggestion.review_state == "approved":
        raise DomainError("Este título já foi adicionado à biblioteca.")
    if candidate.media_type != suggestion.media_type:
        raise DomainError("O match escolhido é de outro tipo de mídia.")

    suggestion.candidate_json = candidate_to_json(candidate)
    existing = candidates_from_json(suggestion.candidates_json)
    if not any(
        item.external_id == candidate.external_id
        and item.provider == candidate.provider
        for item in existing
    ):
        existing.insert(0, candidate)
    suggestion.candidates_json = candidates_to_json(existing)
    suggestion.error = None
    suggestion.decided_at = None
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

    base = [LibrarySyncSuggestion.user_id == user_id]
    if provider:
        base.append(LibrarySyncSuggestion.provider == provider)

    ready = db.execute(
        select(func.count())
        .select_from(LibrarySyncSuggestion)
        .where(
            *base,
            LibrarySyncSuggestion.review_state == "pending",
            LibrarySyncSuggestion.candidate_json.is_not(None),
            or_(
                LibrarySyncSuggestion.error.is_(None),
                LibrarySyncSuggestion.error == "",
            ),
        )
    ).scalar() or 0
    attention = db.execute(
        select(func.count())
        .select_from(LibrarySyncSuggestion)
        .where(
            *base,
            LibrarySyncSuggestion.review_state == "pending",
            LibrarySyncSuggestion.candidate_json.is_not(None),
            LibrarySyncSuggestion.error.is_not(None),
            LibrarySyncSuggestion.error != "",
        )
    ).scalar() or 0
    no_match = db.execute(
        select(func.count())
        .select_from(LibrarySyncSuggestion)
        .where(
            *base,
            LibrarySyncSuggestion.review_state == "pending",
            LibrarySyncSuggestion.candidate_json.is_(None),
        )
    ).scalar() or 0
    last_run = db.execute(
        select(func.max(LibrarySyncSuggestion.sync_run_at)).where(*base)
    ).scalar_one_or_none()
    last_sync = 0
    if last_run is not None:
        last_sync = (
            db.execute(
                select(func.count())
                .select_from(LibrarySyncSuggestion)
                .where(
                    *base,
                    LibrarySyncSuggestion.review_state == "approved",
                    LibrarySyncSuggestion.sync_run_at == last_run,
                )
            ).scalar()
            or 0
        )

    counts["ready"] = ready
    counts["attention"] = attention
    counts["no_match"] = no_match
    counts["last_sync"] = last_sync
    return counts
