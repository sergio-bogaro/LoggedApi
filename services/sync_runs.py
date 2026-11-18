"""Execuções de sincronização em background com progresso consultável.

O processo é local e roda em um único worker, então o estado fica em memória.
Cada usuário tem no máximo uma execução ativa por vez.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from services.library_sync.engine import sync_user
from services.library_sync.orchestrator import sync_all_for_user
from services.library_sync.registry import get_provider
from services.suwayomi.sync import service as suwayomi_sync

_MAX_RUNS_PER_USER = 20


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _status_from_summary(summary: dict | None) -> str:
    value = str((summary or {}).get("status") or "ok")
    if value in ("ok", "success"):
        return "success"
    if value == "partial":
        return "partial"
    return "failed"


@dataclass
class SyncRun:
    id: str
    user_id: int
    scope: str
    status: str = "running"
    phase: str = "starting"
    label: str = ""
    provider: str | None = None
    provider_index: int = 0
    provider_total: int = 0
    done: int = 0
    total: int = 0
    providers: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    summary: dict | None = None
    error: str | None = None

    def percent(self) -> float | None:
        if self.status in ("success", "partial") and self.phase == "done":
            return 100.0
        if self.provider_total > 0:
            current = 0.0
            if self.total > 0 and self.provider_index > 0:
                current = min(self.done / self.total, 1.0)
            completed = max(self.provider_index - 1, 0)
            return round(
                min((completed + current) / self.provider_total * 100, 100),
                1,
            )
        if self.total > 0:
            return round(min(self.done / self.total * 100, 100), 1)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "scope": self.scope,
            "status": self.status,
            "phase": self.phase,
            "label": self.label,
            "provider": self.provider,
            "provider_index": self.provider_index,
            "provider_total": self.provider_total,
            "done": self.done,
            "total": self.total,
            "percent": self.percent(),
            "providers": [dict(item) for item in self.providers],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "error": self.error,
        }


class SyncRunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, SyncRun] = {}
        self._active_by_user: dict[int, str] = {}

    def start(self, user_id: int, scope: str) -> tuple[dict[str, Any], bool]:
        if scope not in ("all", "suwayomi"):
            get_provider(scope)  # valida o provedor

        with self._lock:
            active_id = self._active_by_user.get(user_id)
            active = self._runs.get(active_id) if active_id else None
            if active is not None and active.status == "running":
                return active.to_dict(), False
            if active_id:
                self._active_by_user.pop(user_id, None)

            run = SyncRun(
                id=uuid.uuid4().hex,
                user_id=user_id,
                scope=scope,
                label="Preparando sincronização…",
            )
            self._runs[run.id] = run
            self._active_by_user[user_id] = run.id
            snapshot = run.to_dict()

        thread = threading.Thread(
            target=self._run,
            args=(run.id, user_id, scope),
            daemon=True,
            name=f"sync-run-{run.id[:8]}",
        )
        thread.start()
        return snapshot, True

    def get(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.user_id != user_id:
                return None
            return run.to_dict()

    def active(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            run_id = self._active_by_user.get(user_id)
            run = self._runs.get(run_id) if run_id else None
            if run is None or run.status != "running":
                return None
            return run.to_dict()

    def _update(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            for key in (
                "phase",
                "label",
                "provider",
                "provider_index",
                "provider_total",
                "done",
                "total",
            ):
                if key in event and event[key] is not None:
                    setattr(run, key, event[key])
            if "providers" in event and event["providers"] is not None:
                run.providers = [dict(item) for item in event["providers"]]

    def _finish(
        self,
        run_id: str,
        *,
        status: str,
        summary: dict | None,
        error: str | None,
    ) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.status = status
            run.phase = "done"
            run.finished_at = utcnow()
            run.summary = summary
            run.error = error
            self._active_by_user.pop(run.user_id, None)
            self._prune(run.user_id)

    def _prune(self, user_id: int) -> None:
        runs = [
            run
            for run in self._runs.values()
            if run.user_id == user_id and run.status != "running"
        ]
        runs.sort(key=lambda item: item.finished_at or item.started_at)
        for run in runs[:-_MAX_RUNS_PER_USER]:
            self._runs.pop(run.id, None)

    def _run(self, run_id: str, user_id: int, scope: str) -> None:
        summary: dict | None = None
        error: str | None = None
        status = "failed"

        def progress(event: dict[str, Any]) -> None:
            self._update(run_id, event)

        try:
            if scope == "all":
                summary = sync_all_for_user(user_id, progress=progress)
            elif scope == "suwayomi":
                summary = suwayomi_sync.sync_user(
                    user_id, progress=progress
                )
            else:
                summary = sync_user(
                    user_id, scope, progress=progress
                )
            status = _status_from_summary(summary)
        except Exception as exc:  # noqa: BLE001 — reportado no dialog
            error = str(exc)
            status = "failed"
        finally:
            self._finish(
                run_id, status=status, summary=summary, error=error
            )


sync_runs = SyncRunManager()
