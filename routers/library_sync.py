import asyncio
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from database import get_db
from models.library_sync import (
    LibraryConnection,
    LibraryStatusMapping,
    LibrarySyncLink,
)
from schemas.library_sync import (
    OAuthPollPayload,
    OAuthPollResponse,
    OAuthStartResponse,
    SyncConfigPayload,
    SyncProviderInfo,
    SyncResponse,
    SyncStatusesResponse,
    SyncStatusesUpdate,
    SyncStatusOption,
    SyncTestPayload,
    SyncTestResponse,
)
from services.library_sync.engine import engine as sync_engine, sync_user, utcnow
from services.library_sync.providers.base import LibraryProvider
from services.library_sync.registry import get_provider, list_providers

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
    provider: LibraryProvider, connection: LibraryConnection | None
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


@router.get("", response_model=list[SyncProviderInfo])
def list_integrations(user_id: int, db: Session = Depends(get_db)):
    return [
        _info(provider, _get_connection(db, user_id, provider.id))
        for provider in list_providers()
    ]


@router.get("/{provider_id}", response_model=SyncProviderInfo)
def get_integration(
    provider_id: str, user_id: int, db: Session = Depends(get_db)
):
    provider = get_provider(provider_id)
    return _info(provider, _get_connection(db, user_id, provider_id))


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
    return _info(provider, connection)


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
