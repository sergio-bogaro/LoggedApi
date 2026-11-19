"""Clientes e locks HTTP escopados por event loop.

Os serviços de metadata são singletons de módulo e são usados em dois contextos:
o loop principal do FastAPI (API/worker de importação) e loops criados por
`asyncio.run` em threads (sync em background). Um `httpx.AsyncClient` e um
`asyncio.Lock` pertencem ao loop em que foram criados; reutilizá-los em outro
loop falha com "Event loop is closed" / "TCPTransport ... handler is closed".

Aqui o cache é indexado pelo loop (`WeakKeyDictionary`): cada loop ganha seus
próprios objetos, mantendo keep-alive dentro de uma mesma operação e liberando
as entradas quando o loop é descartado.
"""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary

import httpx

_clients: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]] = (
    WeakKeyDictionary()
)
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)


def get_async_client(
    name: str,
    *,
    timeout: float,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    """Cliente nomeado do loop atual (criado sob demanda)."""
    loop = asyncio.get_running_loop()
    per_loop = _clients.get(loop)
    if per_loop is None:
        per_loop = {}
        _clients[loop] = per_loop

    client = per_loop.get(name)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects
        )
        per_loop[name] = client
    return client


def get_loop_lock(name: str) -> asyncio.Lock:
    """Lock nomeado do loop atual (para throttle/token refresh)."""
    loop = asyncio.get_running_loop()
    per_loop = _locks.get(loop)
    if per_loop is None:
        per_loop = {}
        _locks[loop] = per_loop

    lock = per_loop.get(name)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[name] = lock
    return lock


async def close_loop_clients() -> None:
    """Fecha os clientes do loop atual (usar antes de encerrar um loop próprio)."""
    loop = asyncio.get_running_loop()
    per_loop = _clients.pop(loop, None)
    _locks.pop(loop, None)
    if not per_loop:
        return
    for client in per_loop.values():
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — fechamento é melhor esforço
            pass
