"""Orquestração do sync geral de todas as integrações de um usuário."""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database import SessionLocal
from models.library_sync import LibraryConnection, LibrarySyncSettings
from models.suwayomi import SuwayomiAccount
from services.library_sync.engine import sync_user
from services.library_sync.providers.base import LibraryProvider
from services.library_sync.registry import list_providers
from services.suwayomi.sync import service as suwayomi_sync


_START_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DEFAULT_START_TIME = "00:00"
ProgressCallback = Callable[[dict], None]


def _report(progress: ProgressCallback | None, **fields) -> None:
    if progress is None:
        return
    try:
        progress(fields)
    except Exception:  # noqa: BLE001 — progresso nunca derruba o sync
        pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clamp_updates(updates_per_day: int) -> int:
    return min(max(int(updates_per_day or 1), 1), 24)


def normalize_start_time(value: str | None) -> str:
    """Normaliza ``HH:MM``; valores inválidos caem em 00:00."""
    match = _START_TIME_RE.match((value or "").strip())
    if match is None:
        return _DEFAULT_START_TIME
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def next_slot_local(
    now_local: datetime, start_time: str, updates_per_day: int
) -> datetime:
    """Próximo slot na hora local do servidor.

    A grade é ancorada no horário inicial e repete a cada 24h. Ex.: 00:00
    com 4x/dia gera 00:00, 06:00, 12:00 e 18:00.
    """
    per_day = _clamp_updates(updates_per_day)
    hour, minute = (
        int(part) for part in normalize_start_time(start_time).split(":")
    )
    anchor = now_local.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    step = timedelta(seconds=86400 / per_day)
    if now_local < anchor:
        return anchor
    elapsed = (now_local - anchor).total_seconds()
    steps = int(elapsed // step.total_seconds()) + 1
    return anchor + step * steps


def next_sync_at(
    start_time: str,
    updates_per_day: int,
    now_local: datetime | None = None,
) -> datetime:
    """Próximo slot convertido para UTC ingênuo, como o restante do banco."""
    local = now_local or datetime.now()
    slot = next_slot_local(local, start_time, updates_per_day)
    return slot.astimezone(timezone.utc).replace(tzinfo=None)


def schedule_slots(start_time: str, updates_per_day: int) -> list[str]:
    """Horários do dia para exibição/teste, no formato HH:MM."""
    per_day = _clamp_updates(updates_per_day)
    hour, minute = (
        int(part) for part in normalize_start_time(start_time).split(":")
    )
    step = (24 * 60) / per_day
    slots: list[str] = []
    for index in range(per_day):
        total = round(hour * 60 + minute + index * step) % (24 * 60)
        slots.append(f"{total // 60:02d}:{total % 60:02d}")
    return slots


def get_settings(db: Session, user_id: int) -> LibrarySyncSettings:
    settings = db.execute(
        select(LibrarySyncSettings).where(LibrarySyncSettings.user_id == user_id)
    ).scalar_one_or_none()
    if settings is None:
        settings = LibrarySyncSettings(
            user_id=user_id,
            enabled=False,
            updates_per_day=4,
            start_time=_DEFAULT_START_TIME,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def save_settings(
    db: Session,
    user_id: int,
    enabled: bool,
    updates_per_day: int,
    start_time: str = _DEFAULT_START_TIME,
) -> LibrarySyncSettings:
    settings = get_settings(db, user_id)
    settings.enabled = bool(enabled)
    settings.updates_per_day = _clamp_updates(updates_per_day)
    settings.start_time = normalize_start_time(start_time)
    settings.next_sync_at = (
        next_sync_at(settings.start_time, settings.updates_per_day)
        if settings.enabled
        else None
    )
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
        if (
            connection is not None
            and connection.enabled
            and not connection.needs_reauth
            and _is_connected(connection)
        ):
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


def sync_all_for_user(
    user_id: int, progress: ProgressCallback | None = None
) -> dict:
    """Sincroniza todos os provedores incluídos, em sequência."""
    db = SessionLocal()
    try:
        settings = get_settings(db, user_id)
        results: list[dict] = []
        run_at = utcnow()

        generic_ids = included_provider_ids(db, user_id)
        include_suwayomi = suwayomi_included(db, user_id)
        scope_ids = list(generic_ids)
        if include_suwayomi:
            scope_ids.append("suwayomi")

        provider_states = [
            {"id": provider_id, "status": "pending", "done": 0, "total": 0, "error": None}
            for provider_id in scope_ids
        ]
        total_providers = len(scope_ids)
        _report(
            progress,
            phase="starting",
            label="Preparando sincronização…",
            provider_total=total_providers,
            provider_index=0,
            done=0,
            total=0,
            providers=[dict(item) for item in provider_states],
        )

        for provider_index, provider_id in enumerate(scope_ids, start=1):
            state = provider_states[provider_index - 1]
            state["status"] = "running"
            _report(
                progress,
                phase="fetching",
                label=provider_id,
                provider=provider_id,
                provider_index=provider_index,
                provider_total=total_providers,
                done=0,
                total=0,
                providers=[dict(item) for item in provider_states],
            )

            def provider_progress(
                event: dict,
                provider_id=provider_id,
                provider_index=provider_index,
            ) -> None:
                event = dict(event)
                event["provider"] = provider_id
                event["provider_index"] = provider_index
                event["provider_total"] = total_providers
                if event.get("done") is not None:
                    state["done"] = event["done"]
                if event.get("total") is not None:
                    state["total"] = event["total"]
                if event.get("phase") in ("fetching", "syncing"):
                    state["status"] = "running"
                event["providers"] = [dict(item) for item in provider_states]
                _report(progress, **event)

            try:
                if provider_id == "suwayomi":
                    summary = suwayomi_sync.sync_user(
                        user_id, run_at, progress=provider_progress
                    )
                else:
                    summary = sync_user(
                        user_id, provider_id, run_at, provider_progress
                    )
                results.append(
                    {
                        "provider": provider_id,
                        "status": str(summary.get("status") or "ok"),
                        "summary": _compact_summary(summary),
                    }
                )
                state["status"] = (
                    "error"
                    if summary.get("status") in ("failed", "error")
                    else "done"
                )
            except Exception as exc:  # noqa: BLE001 — um provedor não derruba os outros
                state["status"] = "error"
                state["error"] = str(exc)
                results.append(
                    {
                        "provider": provider_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
            _report(
                progress,
                phase="finishing",
                label=provider_id,
                provider=provider_id,
                provider_index=provider_index,
                provider_total=total_providers,
                done=state["done"],
                total=state["total"],
                providers=[dict(item) for item in provider_states],
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
            next_sync_at(settings.start_time, settings.updates_per_day)
            if settings.enabled
            else None
        )
        db.commit()
        _report(
            progress,
            phase="done",
            provider_total=total_providers,
            provider_index=total_providers,
            providers=[dict(item) for item in provider_states],
        )
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
    return list(
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


def next_due_at(db: Session) -> datetime | None:
    """Menor ``next_sync_at`` entre os usuários com sync automático ligado.

    ``next_sync_at`` nulo significa "vencido agora" (ex.: configuração antiga
    recém-migrada), então devolve o instante atual para o scheduler não dormir.
    """
    has_unscheduled = db.execute(
        select(LibrarySyncSettings.id)
        .where(
            LibrarySyncSettings.enabled.is_(True),
            LibrarySyncSettings.next_sync_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if has_unscheduled is not None:
        return utcnow()
    return db.execute(
        select(func.min(LibrarySyncSettings.next_sync_at)).where(
            LibrarySyncSettings.enabled.is_(True)
        )
    ).scalar_one_or_none()
