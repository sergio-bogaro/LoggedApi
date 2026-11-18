from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import ImportCandidate, ImportFailure, ImportLogEntry

ReviewSource = Literal["manual", "sync"]
ReviewState = Literal["pending", "approved", "dismissed", "failed"]
ReviewMatch = Literal[
    "ready", "needs_validation", "no_match", "already_in_library"
]


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ReviewItem(_CamelModel):
    key: str
    source: ReviewSource
    provider: str
    media_type: MediaTypeEnum
    title: str
    year: int | None = None
    overview: str | None = None
    source_url: str | None = None
    cover_url: str | None = None
    external_refs: dict[str, str] = Field(default_factory=dict)

    action: str = "ignore"
    target_status: MediaStatusEnum | None = None
    rating: float | None = None
    review: str | None = None
    watched_date: date | None = None
    logs: list[ImportLogEntry] = Field(default_factory=list)
    in_backlog: bool = False
    backlog_date: date | None = None

    candidate: ImportCandidate | None = None
    candidates: list[ImportCandidate] = Field(default_factory=list)
    selected: ImportCandidate | None = None
    match: ReviewMatch = "no_match"
    error: str | None = None

    state: ReviewState = "pending"
    ignored: bool = False
    job_id: int | None = None
    job_status: str | None = None
    sync_run_at: datetime | None = None
    origin: str = "review"

    commit_outcome: str | None = None
    commit_reason: str | None = None
    logs_created: int = 0
    backlog_added: bool = False

    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReviewCounts(_CamelModel):
    pending: int = 0
    approved: int = 0
    dismissed: int = 0
    failed: int = 0
    ready: int = 0
    needs_validation: int = 0
    no_match: int = 0
    already_in_library: int = 0
    manual_jobs: int = 0
    sync_items: int = 0
    pending_tasks: int = 0


class ReviewItemsResponse(_CamelModel):
    items: list[ReviewItem]
    total: int
    counts: ReviewCounts
    offset: int = 0
    limit: int = 50


class ReviewPatch(_CamelModel):
    user_id: int
    selected: ImportCandidate | None = None
    ignored: bool | None = None


class ReviewStateRequest(_CamelModel):
    user_id: int
    keys: list[str]
    state: Literal["pending", "dismissed"]


class ReviewCommitRequest(_CamelModel):
    user_id: int
    keys: list[str]
    add_to_backlog: bool = True


class ReviewCommitResult(_CamelModel):
    key: str
    source: ReviewSource
    outcome: str
    reason: str | None = None
    logs_created: int = 0
    backlog_added: bool = False


class ReviewCommitResponse(_CamelModel):
    imported: int = 0
    merged: int = 0
    skipped: int = 0
    failed: int = 0
    logs_created: int = 0
    backlog_added: int = 0
    items: list[ReviewCommitResult] = Field(default_factory=list)
    failures: list[ImportFailure] = Field(default_factory=list)
