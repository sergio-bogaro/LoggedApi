from datetime import date, datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import ImportCandidate, ImportLogEntry


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ImportJobResponse(_CamelModel):
    id: int
    provider: str
    media_type: MediaTypeEnum | None = None
    input_type: str
    status: str
    progress_done: int = 0
    progress_total: int = 0
    counts: dict = {}
    summary: dict | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ImportJobItemResponse(_CamelModel):
    id: int
    key: str
    media_type: MediaTypeEnum
    title: str
    year: int | None = None
    overview: str | None = None
    source: str
    external_refs: dict[str, str] = {}
    status: MediaStatusEnum | None = None
    rating: float | None = None
    review: str | None = None
    logs: list[ImportLogEntry] = []
    in_backlog: bool = False
    backlog_date: date | None = None

    match_status: str | None = None
    match: ImportCandidate | None = None
    candidates: list[ImportCandidate] = []
    existing_media_id: int | None = None
    match_error: str | None = None

    # Seleção efetiva: o candidato escolhido pelo usuário ou o melhor match.
    selected: ImportCandidate | None = None
    ignored: bool = False

    commit_outcome: str | None = None
    commit_reason: str | None = None
    logs_created: int = 0
    backlog_added: bool = False


class ImportJobItemsResponse(_CamelModel):
    items: list[ImportJobItemResponse]
    total: int
    offset: int
    limit: int
    counts: dict[str, int] = {}


class ImportJobPatch(_CamelModel):
    selected: ImportCandidate | None = None
    ignored: bool | None = None


class ImportJobCommitRequest(_CamelModel):
    add_to_backlog: bool = True
