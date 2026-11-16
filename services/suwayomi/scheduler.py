"""Agendador do sync do Suwayomi.

Roda enquanto a API estiver de pé. A cada minuto verifica quais contas estão
vencidas; o intervalo real é calculado por ``updates_per_day`` e persistido em
``next_sync_at`` para sobreviver a reinícios.
"""

import asyncio
from datetime import datetime

from sqlalchemy import or_, select

from database import SessionLocal
from models.suwayomi import SuwayomiAccount
from services.suwayomi.sync import utcnow
from services.suwayomi.sync import service as sync_service

_TICK_SECONDS = 60


class SuwayomiScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="suwayomi-scheduler")

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
                print(f"Suwayomi scheduler: {exc}")

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
                    select(SuwayomiAccount.user_id).where(
                        SuwayomiAccount.enabled.is_(True),
                        SuwayomiAccount.base_url != "",
                        or_(
                            SuwayomiAccount.next_sync_at.is_(None),
                            SuwayomiAccount.next_sync_at <= now,
                        ),
                    )
                )
                .scalars()
                .all()
            )
        finally:
            db.close()

        for user_id in rows:
            try:
                await asyncio.to_thread(sync_service.sync_user, user_id)
            except Exception as exc:  # noqa: BLE001 — uma conta não derruba as outras
                print(f"Suwayomi scheduler (user {user_id}): {exc}")


scheduler = SuwayomiScheduler()
