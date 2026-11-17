from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.import_schemas import ImportCandidate, ImportFailure

SyncAction = Literal[
    "ignore",
    "backlog",
    "in_progress",
    "following",
    "on_hold",
    "finished",
    "dropped",
]


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SyncProviderInfo(_CamelModel):
    id: str
    label: str
    media_types: list[MediaTypeEnum]
    auth_kind: str
    connected: bool = False
    account: str | None = None
    enabled: bool = False
    updates_per_day: int = 4
    baseline_completed_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_summary: dict | None = None
    next_sync_at: datetime | None = None
    needs_reauth: bool = False
    username: str | None = None
    client_id: str | None = None
    has_token: bool = False
    pending_review: int = 0


class SyncConfigPayload(_CamelModel):
    user_id: int
    enabled: bool = True
    updates_per_day: int = Field(4, ge=1, le=24)
    username: str | None = None
    token: str | None = None
    client_id: str | None = None
    clear_auth: bool = False


class SyncStatusOption(_CamelModel):
    source_status: str
    source_label: str
    action: SyncAction = "ignore"
    status: MediaStatusEnum | None = None
    position: int = 0


class SyncStatusesResponse(_CamelModel):
    statuses: list[SyncStatusOption]


class SyncStatusEntry(_CamelModel):
    source_status: str
    action: SyncAction
    source_label: str | None = None
    position: int = 0


class SyncStatusesUpdate(_CamelModel):
    user_id: int
    statuses: list[SyncStatusEntry]


class SyncTestPayload(_CamelModel):
    user_id: int


class SyncTestResponse(_CamelModel):
    ok: bool
    account: str | None = None
    error: str | None = None


class SyncResponse(_CamelModel):
    status: str
    summary: dict


class OAuthStartResponse(_CamelModel):
    user_code: str | None = None
    verification_url: str | None = None
    expires_in: int = 600
    interval: int = 5


class OAuthPollPayload(_CamelModel):
    user_id: int


class OAuthPollResponse(_CamelModel):
    status: str
    account: str | None = None
    error: str | None = None


SyncSuggestionState = Literal["pending", "approved", "dismissed"]


class SyncSuggestion(_CamelModel):
    id: int
    provider: str
    source_id: str
    media_type: MediaTypeEnum
    title: str
    year: int | None = None
    source_status: str
    source_label: str = ""
    action: SyncAction = "ignore"
    target_status: MediaStatusEnum | None = None
    rating: float | None = None
    review: str | None = None
    watched_date: date | None = None
    candidate: ImportCandidate | None = None
    candidates: list[ImportCandidate] = []
    source_url: str | None = None
    review_state: SyncSuggestionState = "pending"
    origin: str = "review"
    sync_run_at: datetime | None = None
    media_id: int | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SyncSuggestionCounts(_CamelModel):
    pending: int = 0
    approved: int = 0
    dismissed: int = 0
    attention: int = 0
    no_match: int = 0
    last_sync: int = 0


class SyncSuggestionsResponse(_CamelModel):
    items: list[SyncSuggestion]
    total: int
    counts: SyncSuggestionCounts
    last_sync_at: datetime | None = None


class SyncSuggestionsApprove(_CamelModel):
    user_id: int
    ids: list[int]


class SyncSuggestionsApproveResponse(_CamelModel):
    approved: int
    failed: int
    failures: list[ImportFailure] = []


class SyncSuggestionStateUpdate(_CamelModel):
    user_id: int
    state: Literal["pending", "dismissed"]


class SyncSuggestionCandidateUpdate(_CamelModel):
    user_id: int
    candidate: ImportCandidate


class SyncSuggestionCountResponse(_CamelModel):
    pending: int


class GlobalSyncSettings(_CamelModel):
    enabled: bool = False
    updates_per_day: int = 4
    start_time: str = "00:00"
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_summary: dict | None = None
    next_sync_at: datetime | None = None


class GlobalSyncConfigPayload(_CamelModel):
    user_id: int
    enabled: bool = False
    updates_per_day: int = Field(4, ge=1, le=24)
    start_time: str = Field("00:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class GlobalSyncPayload(_CamelModel):
    user_id: int


class GlobalSyncResultItem(_CamelModel):
    provider: str
    status: str
    summary: dict | None = None
    error: str | None = None


class GlobalSyncResponse(_CamelModel):
    status: str
    results: list[GlobalSyncResultItem] = []
    ok: int = 0
    failed: int = 0
