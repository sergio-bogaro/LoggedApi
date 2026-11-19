"""Agendador global dos sincronizadores de biblioteca.

A configuração é por usuário: horário inicial + frequência diária. O loop
dorme até o próximo slot e acorda imediatamente quando a configuração muda,
em vez de verificar a cada minuto.
"""

import asyncio
import logging

from config import settings
from database import SessionLocal
from services.library_sync.orchestrator import (
    due_user_ids,
    next_due_at,
    utcnow,
)
from services.sync_runs import sync_runs

# Reavalia o relógio/banco no máximo uma vez por hora, mesmo sem mudanças.
_MAX_SLEEP_SECONDS = 3600

logger = logging.getLogger(__name__)


class LibrarySyncScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None

    async def start(self) -> None:
        if not settings.library_sync_scheduler_enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="library-sync-scheduler")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._wake is not None:
            self._wake.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def wake(self) -> None:
        """Recalcula o próximo slot depois de uma mudança de configuração."""
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self._sleep_until_next()
                if self._stop.is_set():
                    break
                await self._tick()
            except Exception:  # noqa: BLE001 — o loop não pode morrer
                logger.exception("Library sync scheduler")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass

    async def _sleep_until_next(self) -> None:
        assert self._wake is not None
        timeout = self._seconds_until_next()
        if timeout > 0:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        self._wake.clear()

    def _seconds_until_next(self) -> float:
        db = SessionLocal()
        try:
            next_at = next_due_at(db)
        finally:
            db.close()

        if next_at is None:
            return _MAX_SLEEP_SECONDS
        seconds = (next_at - utcnow()).total_seconds()
        return min(max(seconds, 0.0), _MAX_SLEEP_SECONDS)

    async def _tick(self) -> None:
        db = SessionLocal()
        try:
            user_ids = due_user_ids(db)
        finally:
            db.close()

        for user_id in user_ids:
            try:
                run, _started = sync_runs.start(user_id, "all")
                await asyncio.to_thread(sync_runs.wait, run["id"])
            except Exception:  # noqa: BLE001 — um usuário não derruba os outros
                logger.exception("Library sync scheduler (user %s)", user_id)


scheduler = LibrarySyncScheduler()
