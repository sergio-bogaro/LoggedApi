from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum, MediaTypeEnum

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
