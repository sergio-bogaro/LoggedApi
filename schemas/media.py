from typing import TYPE_CHECKING

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum, MediaTypeEnum

if TYPE_CHECKING:
    from schemas.media_log import MediaLogResponse


class MediaBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    type: MediaTypeEnum
    external_id: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    cover_url: str | None = None
    release_date: datetime | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )


class MediaCreate(MediaBase):
    status: MediaStatusEnum = MediaStatusEnum.BACKLOG


class MediaUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    status: MediaStatusEnum | None = None
    description: str | None = None
    cover_url: str | None = None
    image_path: str | None = None
    release_date: datetime | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None


class MediaResponse(MediaBase):
    id: int
    status: MediaStatusEnum
    image_path: str | None = None
    created_at: datetime
    updated_at: datetime
    log_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class MediaWithLogsResponse(MediaResponse):
    logs: list["MediaLogResponse"] = []

    model_config = ConfigDict(from_attributes=True)
