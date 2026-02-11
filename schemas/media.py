from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from models.media import MediaStatusEnum, MediaTypeEnum


class MediaBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    type: MediaTypeEnum
    external_id: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    cover_url: str | None = None
    year: str | None = None
    rating: float | None = Field(None, ge=0, le=10)
    notes: str | None = None
    extra_data: dict | None = None


class MediaCreate(MediaBase):
    status: MediaStatusEnum = MediaStatusEnum.BACKLOG


class MediaUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    status: MediaStatusEnum | None = None
    description: str | None = None
    cover_url: str | None = None
    image_path: str | None = None
    year: str | None = None
    rating: float | None = Field(None, ge=0, le=10)
    notes: str | None = None
    extra_data: dict | None = None


class MediaResponse(MediaBase):
    id: int
    status: MediaStatusEnum
    image_path: str | None = None
    created_at: datetime
    updated_at: datetime
    log_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class MediaWithLogsResponse(MediaResponse):
    from schemas.media_log import MediaLogResponse

    logs: list[MediaLogResponse] = []

    model_config = ConfigDict(from_attributes=True)
