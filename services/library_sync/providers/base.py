"""Contrato dos provedores de sincronização de biblioteca.

Cada provedor busca a biblioteca remota e devolve itens normalizados. O motor
cuida do vínculo com o `Media`, do mapeamento de status, do backlog e das
pendências — assim como o sync do Suwayomi.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import LibraryConnection

Action = str


@dataclass
class SourceStatus:
    key: str
    label: str
    default_action: Action = "ignore"
    default_status: MediaStatusEnum | None = None


@dataclass
class SourceItem:
    source_id: str
    media_type: MediaTypeEnum
    title: str
    status: str
    year: int | None = None
    rating: float | None = None
    review: str | None = None
    overview: str | None = None
    cover_url: str | None = None
    release_date: date | None = None
    external_id: str | None = None
    external_refs: dict[str, str] = field(default_factory=dict)
    source_url: str | None = None
    updated_at: datetime | None = None
    # Data em que o item foi consumido na origem (diário do Letterboxd, por exemplo).
    watched_at: date | None = None


@dataclass
class SourceSnapshot:
    items: list[SourceItem] = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)
    removed_source_ids: set[str] = field(default_factory=set)
    cursor: dict[str, Any] = field(default_factory=dict)
    # Um snapshot parcial (delta) não permite concluir que links ausentes foram
    # removidos da origem.
    full: bool = True
    skipped: bool = False


class LibraryProvider:
    id: str = ""
    label: str = ""
    media_types: list[MediaTypeEnum] = []
    # username | token | oauth
    auth_kind: str = "username"
    tracks_removals: bool = True
    # Quando True, a fila de revisão também vale no primeiro sync (baseline).
    # Fora isso, o baseline continua apenas vinculando o que já existe.
    review_baseline: bool = False
    # Quando True, o sync cria um MediaLog com a data de consumo da origem.
    creates_logs: bool = False

    def statuses(self) -> list[SourceStatus]:
        return []

    async def test(self, connection: LibraryConnection) -> str:
        """Valida as credenciais e devolve um rótulo da conta."""
        raise NotImplementedError

    async def fetch(
        self, connection: LibraryConnection, cursor: dict[str, Any]
    ) -> SourceSnapshot:
        raise NotImplementedError

    # ── helpers de credenciais ──

    @staticmethod
    def auth(connection: LibraryConnection) -> dict[str, Any]:
        try:
            data = json.loads(connection.auth_json or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def set_auth(connection: LibraryConnection, data: dict[str, Any]) -> None:
        connection.auth_json = json.dumps(data, ensure_ascii=False)

    @staticmethod
    def cursor(connection: LibraryConnection) -> dict[str, Any]:
        try:
            data = json.loads(connection.cursor_json or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def set_cursor(connection: LibraryConnection, data: dict[str, Any]) -> None:
        connection.cursor_json = json.dumps(data, ensure_ascii=False)
