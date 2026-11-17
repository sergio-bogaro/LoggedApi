"""Agendador dos sincronizadores de biblioteca.

Roda enquanto a API estiver de pé. Um sync por vez, globalmente, para respeitar
os limites das APIs de metadata.
"""

import asyncio
from datetime import datetime

from sqlalchemy import or_, select

from config import settings
from database import SessionLocal
from models.library_sync import LibraryConnection
from services.library_sync.engine import sync_user, utcnow

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
        now: datetime = utcnow()
        db = SessionLocal()
        try:
            rows = (
                db.execute(
                    select(LibraryConnection.user_id, LibraryConnection.provider).where(
                        LibraryConnection.enabled.is_(True),
                        or_(
                            LibraryConnection.next_sync_at.is_(None),
                            LibraryConnection.next_sync_at <= now,
                        ),
                    )
                )
                .all()
            )
        finally:
            db.close()

        for user_id, provider in rows:
            try:
                await asyncio.to_thread(sync_user, user_id, provider)
            except Exception as exc:  # noqa: BLE001 — uma conexão não derruba as outras
                print(f"Library sync scheduler ({provider}, user {user_id}): {exc}")


scheduler = LibrarySyncScheduler()
