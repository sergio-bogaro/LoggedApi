from datetime import date, datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum, MediaTypeEnum


class _CamelModel(BaseModel):
    """Base dos schemas de importação: JSON em camelCase, Python em snake_case."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


# ──────────────────────────────────────────────
# Provedores
# ──────────────────────────────────────────────


class ImportProviderInfo(_CamelModel):
    id: str
    label: str
    media_types: list[MediaTypeEnum]
    # "file" (upload) ou "username" (busca por perfil público)
    input_type: str
    accepts: str | None = None


# ──────────────────────────────────────────────
# Prévia (parse do export, sem tocar no provedor de metadata)
# ──────────────────────────────────────────────


class ImportLogEntry(_CamelModel):
    """Uma sessão/registro — vira um `MediaLog` no commit."""

    date: date
    start_date: datetime | None = None
    end_date: datetime | None = None
    status: MediaStatusEnum | None = None
    rating: float | None = None
    review: str | None = None


class ImportEntry(_CamelModel):
    """Um item consolidado, ainda sem o `external_id` do app (resolvido no match)."""

    key: str
    media_type: MediaTypeEnum
    title: str
    year: int | None = None
    source: str
    external_refs: dict[str, str] = {}
    status: MediaStatusEnum | None = None
    rating: float | None = None
    review: str | None = None
    logs: list[ImportLogEntry] = []
    in_backlog: bool = False
    backlog_date: date | None = None


class ImportPreviewCounts(_CamelModel):
    total: int = 0
    with_logs: int = 0
    rated: int = 0
    reviewed: int = 0
    backlog: int = 0


class ImportPreviewResponse(_CamelModel):
    media_type: MediaTypeEnum
    entries: list[ImportEntry]
    counts: ImportPreviewCounts


# ──────────────────────────────────────────────
# Match
# ──────────────────────────────────────────────


class ImportMatchRequestItem(_CamelModel):
    key: str
    title: str
    year: int | None = None
    media_type: MediaTypeEnum
    external_refs: dict[str, str] = {}


class ImportMatchRequest(_CamelModel):
    user_id: int
    items: list[ImportMatchRequestItem]


class ImportCandidate(_CamelModel):
    provider: str
    external_id: str
    media_type: MediaTypeEnum
    title: str
    year: int | None = None
    cover_url: str | None = None
    overview: str | None = None
    release_date: date | None = None
    score: float = 0.0


class ImportMatchResult(_CamelModel):
    key: str
    # matched | ambiguous | not_found | already_in_library
    status: str
    match: ImportCandidate | None = None
    candidates: list[ImportCandidate] = []
    existing_media_id: int | None = None


class ImportMatchResponse(_CamelModel):
    results: list[ImportMatchResult]


class ImportSearchRequest(_CamelModel):
    query: str
    media_type: MediaTypeEnum
    user_id: int | None = None


# ──────────────────────────────────────────────
# Commit
# ──────────────────────────────────────────────


class ImportCommitEntry(_CamelModel):
    key: str = ""
    media_type: MediaTypeEnum
    external_id: str
    title: str
    year: int | None = None
    cover_url: str | None = None
    overview: str | None = None
    release_date: date | None = None
    status: MediaStatusEnum | None = None
    rating: float | None = None
    review: str | None = None
    logs: list[ImportLogEntry] = []
    add_to_backlog: bool = False
    backlog_date: date | None = None


class ImportCommitRequest(_CamelModel):
    user_id: int
    entries: list[ImportCommitEntry]


class ImportFailure(_CamelModel):
    name: str
    reason: str


class ImportCommitItem(_CamelModel):
    """Desfecho do commit de um item — alimenta o relatório por item."""

    key: str = ""
    external_id: str
    media_type: MediaTypeEnum
    title: str
    # imported | merged | skipped | failed
    outcome: str
    logs_created: int = 0
    backlog_added: bool = False
    reason: str | None = None


class ImportCommitResponse(_CamelModel):
    imported: int = 0
    merged: int = 0
    skipped: int = 0
    logs_created: int = 0
    backlog_added: int = 0
    failures: list[ImportFailure] = []
    items: list[ImportCommitItem] = []
