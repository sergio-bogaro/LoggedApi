from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# Ações que uma categoria pode assumir no Logged.
SuwayomiAction = Literal[
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


class SuwayomiConfigPayload(_CamelModel):
    user_id: int
    base_url: str = Field(..., min_length=1, max_length=500)
    username: str | None = None
    password: str | None = None
    clear_auth: bool = False
    enabled: bool = True
    updates_per_day: int = Field(4, ge=1, le=24)


class SuwayomiConfigResponse(_CamelModel):
    configured: bool = False
    base_url: str | None = None
    username: str | None = None
    has_password: bool = False
    enabled: bool = False
    updates_per_day: int = 4
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_summary: dict | None = None
    next_sync_at: datetime | None = None


class SuwayomiCategory(_CamelModel):
    id: int
    name: str
    order: int = 0
    default: bool = False
    manga_count: int | None = None
    action: SuwayomiAction = "ignore"
    mapped: bool = False


class SuwayomiCategoryEntry(_CamelModel):
    category_id: int
    category_name: str
    action: SuwayomiAction
    category_order: int = 0


class SuwayomiCategoriesUpdate(_CamelModel):
    user_id: int
    categories: list[SuwayomiCategoryEntry]


class SuwayomiCategoriesResponse(_CamelModel):
    categories: list[SuwayomiCategory]
    stale: bool = False
    error: str | None = None


class SuwayomiTestPayload(_CamelModel):
    user_id: int | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = None


class SuwayomiTestResponse(_CamelModel):
    ok: bool
    server_name: str | None = None
    version: str | None = None
    error: str | None = None


class SuwayomiSyncResponse(_CamelModel):
    status: str
    summary: dict


class SuwayomiLink(_CamelModel):
    id: int
    suwayomi_manga_id: int
    media_id: int | None = None
    title: str
    ignored: bool = False
    match_error: str | None = None
    match_attempts: int = 0
    last_match_attempt_at: datetime | None = None
    last_seen_at: datetime | None = None


class SuwayomiLinkUpdate(_CamelModel):
    media_id: int | None = None
    ignored: bool | None = None
