"""Orquestração do sync geral de todas as integrações de um usuário."""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from database import SessionLocal
from models.library_sync import LibraryConnection, LibrarySyncSettings
from models.suwayomi import SuwayomiAccount
from services.library_sync.engine import sync_user
from services.library_sync.providers.base import LibraryProvider
from services.library_sync.registry import list_providers
from services.suwayomi.sync import service as suwayomi_sync


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def interval(updates_per_day: int) -> timedelta:
    per_day = min(max(int(updates_per_day or 1), 1), 24)
    return timedelta(hours=24 / per_day)


def get_settings(db: Session, user_id: int) -> LibrarySyncSettings:
    settings = db.execute(
        select(LibrarySyncSettings).where(LibrarySyncSettings.user_id == user_id)
    ).scalar_one_or_none()
    if settings is None:
        settings = LibrarySyncSettings(
            user_id=user_id,
            enabled=False,
            updates_per_day=4,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def save_settings(
    db: Session, user_id: int, enabled: bool, updates_per_day: int
) -> LibrarySyncSettings:
    settings = get_settings(db, user_id)
    settings.enabled = bool(enabled)
    settings.updates_per_day = min(max(int(updates_per_day or 1), 1), 24)
    settings.next_sync_at = utcnow() if settings.enabled else None
    db.commit()
    db.refresh(settings)
    return settings


def _is_connected(connection: LibraryConnection | None) -> bool:
    if connection is None:
        return False
    auth = LibraryProvider.auth(connection)
    return bool(
        str(auth.get("username") or "").strip()
        or str(auth.get("token") or "").strip()
        or str(auth.get("access_token") or "").strip()
    )


def included_provider_ids(db: Session, user_id: int) -> list[str]:
    """Provedores genéricos conectados e marcados para participar do sync geral."""
    result: list[str] = []
    for provider in list_providers():
        connection = db.execute(
            select(LibraryConnection).where(
                LibraryConnection.user_id == user_id,
                LibraryConnection.provider == provider.id,
            )
        ).scalar_one_or_none()
        if connection is not None and connection.enabled and _is_connected(connection):
            result.append(provider.id)
    return result


def suwayomi_included(db: Session, user_id: int) -> bool:
    account = db.execute(
        select(SuwayomiAccount).where(SuwayomiAccount.user_id == user_id)
    ).scalar_one_or_none()
    return bool(account and account.enabled and (account.base_url or "").strip())


def _compact_summary(summary: dict | None) -> dict:
    if not isinstance(summary, dict):
        return {}
    keys = (
        "created",
        "statusUpdated",
        "backlogAdded",
        "backlogRemoved",
        "logsCreated",
        "missing",
        "reviewStaged",
        "pendingReview",
        "pendingResolved",
    )
    return {key: summary.get(key) for key in keys if summary.get(key) is not None}


def _aggregate_status(results: list[dict]) -> str:
    if not results:
        return "ok"
    failures = sum(
        1 for item in results if item.get("status") in ("failed", "error")
    )
    if failures == len(results):
        return "failed"
    if failures or any(item.get("status") == "partial" for item in results):
        return "partial"
    return "ok"


def sync_all_for_user(user_id: int) -> dict:
    """Sincroniza todos os provedores incluídos, em sequência."""
    db = SessionLocal()
    try:
        settings = get_settings(db, user_id)
        results: list[dict] = []

        for provider_id in included_provider_ids(db, user_id):
            try:
                summary = sync_user(user_id, provider_id)
                results.append(
                    {
                        "provider": provider_id,
                        "status": str(summary.get("status") or "ok"),
                        "summary": _compact_summary(summary),
                    }
                )
            except Exception as exc:  # noqa: BLE001 — um provedor não derruba os outros
                results.append(
                    {
                        "provider": provider_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

        if suwayomi_included(db, user_id):
            try:
                summary = suwayomi_sync.sync_user(user_id)
                results.append(
                    {
                        "provider": "suwayomi",
                        "status": str(summary.get("status") or "ok"),
                        "summary": _compact_summary(summary),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "provider": "suwayomi",
                        "status": "failed",
                        "error": str(exc),
                    }
                )

        status = _aggregate_status(results)
        now = utcnow()
        settings.last_sync_at = now
        settings.last_sync_status = status
        settings.last_sync_summary = json.dumps(
            {
                "results": results,
                "ok": sum(1 for item in results if item.get("status") == "ok"),
                "failed": sum(
                    1
                    for item in results
                    if item.get("status") in ("failed", "error")
                ),
            },
            ensure_ascii=False,
        )
        settings.next_sync_at = (
            now + interval(settings.updates_per_day) if settings.enabled else None
        )
        db.commit()
        return {
            "status": status,
            "results": results,
            "ok": sum(1 for item in results if item.get("status") == "ok"),
            "failed": sum(
                1 for item in results if item.get("status") in ("failed", "error")
            ),
        }
    finally:
        db.close()


def due_user_ids(db: Session, now: datetime | None = None) -> list[int]:
    moment = now or utcnow()
    return (
        db.execute(
            select(LibrarySyncSettings.user_id).where(
                LibrarySyncSettings.enabled.is_(True),
                or_(
                    LibrarySyncSettings.next_sync_at.is_(None),
                    LibrarySyncSettings.next_sync_at <= moment,
                ),
            )
        )
        .scalars()
        .all()
    )
