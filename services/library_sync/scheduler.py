"""Agendador global dos sincronizadores de biblioteca.

Roda enquanto a API estiver de pé. A configuração é por usuário; quando vence,
sincroniza todos os provedores incluídos, em sequência, para respeitar os
limites das APIs de metadata.
"""

import asyncio

from config import settings
from database import SessionLocal
from services.library_sync.orchestrator import due_user_ids, sync_all_for_user

_TICK_SECONDS = 60


class LibrarySyncScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        if not settings.library_sync_scheduler_enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="library-sync-scheduler")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 — o loop não pode morrer
                print(f"Library sync scheduler: {exc}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        db = SessionLocal()
        try:
            user_ids = due_user_ids(db)
        finally:
            db.close()

        for user_id in user_ids:
            try:
                await asyncio.to_thread(sync_all_for_user, user_id)
            except Exception as exc:  # noqa: BLE001 — um usuário não derruba os outros
                print(f"Library sync scheduler (user {user_id}): {exc}")


scheduler = LibrarySyncScheduler()
