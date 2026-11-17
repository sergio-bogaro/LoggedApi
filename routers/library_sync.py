import asyncio
import json
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from database import get_db
from models.library_sync import (
    LibraryConnection,
    LibraryStatusMapping,
    LibrarySyncLink,
    LibrarySyncSuggestion,
)
from schemas.library_sync import (
    GlobalSyncConfigPayload,
    GlobalSyncPayload,
    GlobalSyncResponse,
    GlobalSyncResultItem,
    GlobalSyncSettings,
    OAuthPollPayload,
    OAuthPollResponse,
    OAuthStartResponse,
    SyncConfigPayload,
    SyncProviderInfo,
    SyncResponse,
    SyncStatusesResponse,
    SyncStatusesUpdate,
    SyncStatusOption,
    SyncSuggestion,
    SyncSuggestionCountResponse,
    SyncSuggestionCounts,
    SyncSuggestionsApprove,
    SyncSuggestionsApproveResponse,
    SyncSuggestionsResponse,
    SyncSuggestionStateUpdate,
    SyncTestPayload,
    SyncTestResponse,
)
from services.library_sync.engine import engine as sync_engine, sync_user, utcnow
from services.library_sync.orchestrator import (
    get_settings as get_global_settings,
    save_settings as save_global_settings,
    sync_all_for_user,
)
from services.library_sync.providers.base import LibraryProvider
from services.library_sync.registry import get_provider, list_providers
from services.library_sync.suggestions import (
    approve_suggestions,
    candidate_from_json,
    pending_count,
    set_suggestion_state,
    suggestion_counts,
)

router = APIRouter(prefix="/api/integrations", tags=["Integrações"])

_DEVICE_FLOWS: dict[tuple[int, str], dict] = {}


def _decode_summary(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _get_connection(
    db: Session, user_id: int, provider_id: str
) -> LibraryConnection | None:
    return db.execute(
        select(LibraryConnection).where(
            LibraryConnection.user_id == user_id,
            LibraryConnection.provider == provider_id,
        )
    ).scalar_one_or_none()


def _is_connected(connection: LibraryConnection | None) -> bool:
    if connection is None:
        return False
    auth = LibraryProvider.auth(connection)
    return bool(
        str(auth.get("username") or "").strip()
        or str(auth.get("token") or "").strip()
        or str(auth.get("access_token") or "").strip()
    )


def _account(connection: LibraryConnection | None) -> str | None:
    if connection is None:
        return None
    auth = LibraryProvider.auth(connection)
    return str(auth.get("account") or auth.get("username") or "").strip() or None


def _info(
    provider: LibraryProvider,
    connection: LibraryConnection | None,
    pending_review: int = 0,
) -> SyncProviderInfo:
    auth = LibraryProvider.auth(connection) if connection else {}
    return SyncProviderInfo(
        id=provider.id,
        label=provider.label,
        media_types=provider.media_types,
        auth_kind=provider.auth_kind,
        connected=_is_connected(connection),
        account=_account(connection),
        enabled=bool(connection.enabled) if connection else False,
        updates_per_day=connection.updates_per_day if connection else 4,
        baseline_completed_at=connection.baseline_completed_at if connection else None,
        last_sync_at=connection.last_sync_at if connection else None,
        last_sync_status=connection.last_sync_status if connection else None,
        last_sync_summary=_decode_summary(connection.last_sync_summary)
        if connection
        else None,
        next_sync_at=connection.next_sync_at if connection else None,
        needs_reauth=bool(connection.needs_reauth) if connection else False,
        username=str(auth.get("username") or "").strip() or None,
        client_id=str(auth.get("client_id") or "").strip() or None,
        has_token=bool(
            str(auth.get("token") or "").strip()
            or str(auth.get("access_token") or "").strip()
        ),
        pending_review=pending_review,
    )


def _ensure_connection(
    db: Session, user_id: int, provider_id: str
) -> LibraryConnection:
    connection = _get_connection(db, user_id, provider_id)
    if connection is not None:
        return connection
    connection = LibraryConnection(
        user_id=user_id,
        provider=provider_id,
        enabled=False,
        updates_per_day=4,
        auth_json="{}",
        cursor_json="{}",
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def _global_settings_response(settings) -> GlobalSyncSettings:
    return GlobalSyncSettings(
        enabled=settings.enabled,
        updates_per_day=settings.updates_per_day,
        last_sync_at=settings.last_sync_at,
        last_sync_status=settings.last_sync_status,
        last_sync_summary=_decode_summary(settings.last_sync_summary),
        next_sync_at=settings.next_sync_at,
    )


@router.get("/global", response_model=GlobalSyncSettings)
def get_global_config(user_id: int, db: Session = Depends(get_db)):
    """Configuração geral de sincronização do usuário."""
    return _global_settings_response(get_global_settings(db, user_id))


@router.put("/global", response_model=GlobalSyncSettings)
def save_global_config(
    data: GlobalSyncConfigPayload, db: Session = Depends(get_db)
):
    settings = save_global_settings(
        db, data.user_id, data.enabled, data.updates_per_day
    )
    return _global_settings_response(settings)


@router.post("/sync-all", response_model=GlobalSyncResponse)
async def sync_all_now(data: GlobalSyncPayload):
    """Sincroniza todos os provedores incluídos no sync geral."""
    result = await asyncio.to_thread(sync_all_for_user, data.user_id)
    return GlobalSyncResponse(
        status=result["status"],
        results=[GlobalSyncResultItem(**item) for item in result["results"]],
        ok=result["ok"],
        failed=result["failed"],
    )


def _suggestion_response(suggestion: LibrarySyncSuggestion) -> SyncSuggestion:
    return SyncSuggestion(
        id=suggestion.id,
        provider=suggestion.provider,
        source_id=suggestion.source_id,
        media_type=suggestion.media_type,
        title=suggestion.title,
        year=suggestion.year,
        source_status=suggestion.source_status,
        source_label=suggestion.source_label,
        action=suggestion.action,
        target_status=suggestion.target_status,
        rating=suggestion.rating,
        review=suggestion.review,
        watched_date=suggestion.watched_date,
        candidate=candidate_from_json(suggestion.candidate_json),
        source_url=suggestion.source_url,
        review_state=suggestion.review_state,
        media_id=suggestion.media_id,
        error=suggestion.error,
        created_at=suggestion.created_at,
        updated_at=suggestion.updated_at,
    )


@router.get("", response_model=list[SyncProviderInfo])
def list_integrations(user_id: int, db: Session = Depends(get_db)):
    counts = dict(
        db.execute(
            select(LibrarySyncSuggestion.provider, func.count())
            .where(
                LibrarySyncSuggestion.user_id == user_id,
                LibrarySyncSuggestion.review_state == "pending",
            )
            .group_by(LibrarySyncSuggestion.provider)
        ).all()
    )
    return [
        _info(
            provider,
            _get_connection(db, user_id, provider.id),
            counts.get(provider.id, 0),
        )
        for provider in list_providers()
    ]


@router.get("/suggestions", response_model=SyncSuggestionsResponse)
def list_suggestions(
    user_id: int,
    state: Literal["pending", "approved", "dismissed"] = "pending",
    provider: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """Títulos encontrados pelo sync que aguardam revisão."""
    conditions = [
        LibrarySyncSuggestion.user_id == user_id,
        LibrarySyncSuggestion.review_state == state,
    ]
    if provider:
        conditions.append(LibrarySyncSuggestion.provider == provider)

    total = (
        db.execute(
            select(func.count())
            .select_from(LibrarySyncSuggestion)
            .where(*conditions)
        ).scalar()
        or 0
    )
    page = min(max(int(limit), 1), 200)
    items = (
        db.execute(
            select(LibrarySyncSuggestion)
            .where(*conditions)
            .order_by(
                LibrarySyncSuggestion.provider,
                LibrarySyncSuggestion.title,
                LibrarySyncSuggestion.id,
            )
            .offset(max(int(offset), 0))
            .limit(page)
        )
        .scalars()
        .all()
    )
    counts = suggestion_counts(db, user_id, provider)
    return SyncSuggestionsResponse(
        items=[_suggestion_response(item) for item in items],
        total=total,
        counts=SyncSuggestionCounts(**counts),
    )


@router.get("/suggestions/count", response_model=SyncSuggestionCountResponse)
def count_suggestions(
    user_id: int,
    provider: str | None = None,
    db: Session = Depends(get_db),
):
    return SyncSuggestionCountResponse(
        pending=pending_count(db, user_id, provider)
    )


@router.post(
    "/suggestions/approve", response_model=SyncSuggestionsApproveResponse
)
def approve_sync_suggestions(
    data: SyncSuggestionsApprove, db: Session = Depends(get_db)
):
    """Cria as mídias selecionadas e remove as sugestões da fila."""
    result = approve_suggestions(db, data.user_id, data.ids)
    return SyncSuggestionsApproveResponse(**result)


@router.patch("/suggestions/{suggestion_id}", response_model=SyncSuggestion)
def update_sync_suggestion(
    suggestion_id: int,
    data: SyncSuggestionStateUpdate,
    db: Session = Depends(get_db),
):
    suggestion = set_suggestion_state(
        db, data.user_id, suggestion_id, data.state
    )
    return _suggestion_response(suggestion)


@router.get("/{provider_id}", response_model=SyncProviderInfo)
def get_integration(
    provider_id: str, user_id: int, db: Session = Depends(get_db)
):
    provider = get_provider(provider_id)
    return _info(
        provider,
        _get_connection(db, user_id, provider_id),
        pending_count(db, user_id, provider_id),
    )


@router.put("/{provider_id}/config", response_model=SyncProviderInfo)
def save_config(
    provider_id: str,
    data: SyncConfigPayload,
    db: Session = Depends(get_db),
):
    provider = get_provider(provider_id)
    connection = _ensure_connection(db, data.user_id, provider_id)
    connection.enabled = data.enabled
    connection.updates_per_day = data.updates_per_day

    auth = LibraryProvider.auth(connection)
    if data.clear_auth:
        auth = {}
        connection.needs_reauth = False
    if data.username is not None:
        auth["username"] = data.username.strip()
        auth["account"] = data.username.strip()
    if data.token is not None:
        auth["token"] = data.token.strip()
        auth["account"] = auth.get("account") or provider.label
    if data.client_id is not None:
        auth["client_id"] = data.client_id.strip()

    LibraryProvider.set_auth(connection, auth)
    if connection.next_sync_at is None and connection.enabled:
        connection.next_sync_at = utcnow()
    db.commit()
    db.refresh(connection)

    sync_engine._ensure_mappings(db, connection, provider)
    db.commit()
    return _info(
        provider,
        connection,
        pending_count(db, data.user_id, provider_id),
    )


@router.get("/{provider_id}/statuses", response_model=SyncStatusesResponse)
def get_statuses(
    provider_id: str, user_id: int, db: Session = Depends(get_db)
):
    provider = get_provider(provider_id)
    connection = _get_connection(db, user_id, provider_id)
    if connection is None:
        return SyncStatusesResponse(
            statuses=[
                SyncStatusOption(
                    source_status=status.key,
                    source_label=status.label,
                    action=status.default_action,
                    status=status.default_status,
                    position=index,
                )
                for index, status in enumerate(provider.statuses())
            ]
        )

    mappings = sync_engine._ensure_mappings(db, connection, provider)
    db.commit()
    return SyncStatusesResponse(
        statuses=[
            SyncStatusOption(
                source_status=row.source_status,
                source_label=row.source_label,
                action=row.action,
                status=row.status,
                position=row.position,
            )
            for row in sorted(mappings.values(), key=lambda item: item.position)
        ]
    )


@router.put("/{provider_id}/statuses", response_model=SyncStatusesResponse)
def save_statuses(
    provider_id: str,
    data: SyncStatusesUpdate,
    db: Session = Depends(get_db),
):
    provider = get_provider(provider_id)
    connection = _ensure_connection(db, data.user_id, provider_id)
    existing = sync_engine._ensure_mappings(db, connection, provider)

    for index, entry in enumerate(data.statuses):
        row = existing.get(entry.source_status)
        if row is None:
            row = LibraryStatusMapping(
                user_id=data.user_id,
                provider=provider_id,
                source_status=entry.source_status,
                source_label=entry.source_label or entry.source_status,
                position=index,
            )
            db.add(row)
            existing[entry.source_status] = row
        row.action = entry.action
        row.status = action_status(entry.action)
        row.source_label = entry.source_label or row.source_label
        row.position = entry.position or index

    db.commit()
    return get_statuses(provider_id, data.user_id, db)


def action_status(action: str):
    from models.enums import MediaStatusEnum

    try:
        return MediaStatusEnum(action)
    except ValueError:
        return None


@router.post("/{provider_id}/test", response_model=SyncTestResponse)
async def test_integration(
    provider_id: str,
    data: SyncTestPayload,
    db: Session = Depends(get_db),
):
    provider = get_provider(provider_id)
    connection = _get_connection(db, data.user_id, provider_id)
    if connection is None or not _is_connected(connection):
        return SyncTestResponse(ok=False, error="Conexão não configurada.")
    try:
        account = await provider.test(connection)
        auth = LibraryProvider.auth(connection)
        if account:
            auth["account"] = account
            LibraryProvider.set_auth(connection, auth)
        connection.needs_reauth = False
        db.commit()
        return SyncTestResponse(ok=True, account=account)
    except Exception as exc:  # noqa: BLE001 — reportado na UI
        db.rollback()
        message = str(exc).lower()
        if any(
            token in message
            for token in ("reconecte", "expirada", "inválido", "invalid", "token")
        ):
            connection = _get_connection(db, data.user_id, provider_id)
            if connection is not None:
                connection.needs_reauth = True
                db.commit()
        return SyncTestResponse(ok=False, error=str(exc))


@router.post("/{provider_id}/sync", response_model=SyncResponse)
async def sync_now(
    provider_id: str,
    data: SyncTestPayload,
    db: Session = Depends(get_db),
):
    provider = get_provider(provider_id)
    connection = _get_connection(db, data.user_id, provider_id)
    if connection is None or not _is_connected(connection):
        raise HTTPException(status_code=400, detail="Conexão não configurada.")

    await asyncio.to_thread(sync_user, data.user_id, provider_id)
    db.expire_all()
    connection = _get_connection(db, data.user_id, provider_id)
    summary = _decode_summary(connection.last_sync_summary) or {}
    return SyncResponse(
        status=connection.last_sync_status or "ok",
        summary=summary,
    )


@router.post("/{provider_id}/oauth/start", response_model=OAuthStartResponse)
async def oauth_start(
    provider_id: str,
    data: SyncTestPayload,
    db: Session = Depends(get_db),
):
    provider = get_provider(provider_id)
    if not hasattr(provider, "start_device"):
        raise HTTPException(status_code=400, detail="Provedor não usa OAuth device.")
    result = await provider.start_device()  # type: ignore[attr-defined]
    device_code = result.pop("device_code", None)
    if not device_code:
        raise HTTPException(status_code=502, detail="O provedor não retornou device code.")
    _DEVICE_FLOWS[(data.user_id, provider_id)] = {
        "device_code": device_code,
        "expires_at": utcnow(),
        **result,
    }
    return OAuthStartResponse(**result)


@router.post("/{provider_id}/oauth/poll", response_model=OAuthPollResponse)
async def oauth_poll(
    provider_id: str,
    data: OAuthPollPayload,
    db: Session = Depends(get_db),
):
    provider = get_provider(provider_id)
    flow = _DEVICE_FLOWS.get((data.user_id, provider_id))
    if flow is None:
        return OAuthPollResponse(status="expired")
    if not hasattr(provider, "poll_device"):
        raise HTTPException(status_code=400, detail="Provedor não usa OAuth device.")
    result = await provider.poll_device(flow["device_code"])  # type: ignore[attr-defined]
    status = result.get("status")
    if status != "connected":
        if status == "expired":
            _DEVICE_FLOWS.pop((data.user_id, provider_id), None)
        return OAuthPollResponse(status=status or "error", error=result.get("error"))

    connection = _ensure_connection(db, data.user_id, provider_id)
    auth = LibraryProvider.auth(connection)
    auth.update(result.get("tokens") or {})
    auth["expires_at"] = (
        utcnow()
        + timedelta(
            seconds=int((result.get("tokens") or {}).get("expires_in") or 604800)
        )
    ).isoformat()
    LibraryProvider.set_auth(connection, auth)
    connection.enabled = True
    connection.needs_reauth = False
    connection.next_sync_at = utcnow()
    db.commit()
    db.refresh(connection)

    account = None
    try:
        account = await provider.test(connection)
        auth = LibraryProvider.auth(connection)
        auth["account"] = account
        LibraryProvider.set_auth(connection, auth)
        db.commit()
    except Exception:  # noqa: BLE001 — conexão já foi salva
        db.rollback()

    _DEVICE_FLOWS.pop((data.user_id, provider_id), None)
    return OAuthPollResponse(status="connected", account=account)


@router.delete("/{provider_id}", status_code=204)
def delete_integration(
    provider_id: str, user_id: int, db: Session = Depends(get_db)
):
    get_provider(provider_id)
    _DEVICE_FLOWS.pop((user_id, provider_id), None)
    db.execute(
        delete(LibraryStatusMapping).where(
            LibraryStatusMapping.user_id == user_id,
            LibraryStatusMapping.provider == provider_id,
        )
    )
    db.execute(
        delete(LibrarySyncSuggestion).where(
            LibrarySyncSuggestion.user_id == user_id,
            LibrarySyncSuggestion.provider == provider_id,
        )
    )
    db.execute(
        delete(LibrarySyncLink).where(
            LibrarySyncLink.user_id == user_id,
            LibrarySyncLink.provider == provider_id,
        )
    )
    db.execute(
        delete(LibraryConnection).where(
            LibraryConnection.user_id == user_id,
            LibraryConnection.provider == provider_id,
        )
    )
    db.commit()
