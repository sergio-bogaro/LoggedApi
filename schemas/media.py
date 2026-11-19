from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.date_utils import parse_optional_datetime
from schemas.media_log import MediaLogResponse
from domain_errors import DomainError


class MediaBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    type: MediaTypeEnum
    external_id: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    cover_url: str | None = None
    release_date: datetime | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None
    tags: list[str] = []

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )

    @field_validator("release_date", mode="before")
    @classmethod
    def _normalize_release_date(cls, value: object) -> datetime | None:
        return parse_optional_datetime(value)


class MediaCreate(MediaBase):
    user_id: int
    status: MediaStatusEnum | None = None


class MediaUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    status: MediaStatusEnum | None = None
    description: str | None = None
    cover_url: str | None = None
    release_date: datetime | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None
    tags: list[str] | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )

    @field_validator("release_date", mode="before")
    @classmethod
    def _normalize_release_date(cls, value: object) -> datetime | None:
        return parse_optional_datetime(value)

    @model_validator(mode="before")
    @classmethod
    def _reject_null_title(cls, data: object) -> object:
        if isinstance(data, dict) and "title" in data and data["title"] is None:
            raise DomainError("O título não pode ser nulo.")
        return data


class MediaResponse(MediaBase):
    id: int
    user_id: int
    status: MediaStatusEnum | None = None
    image_path: str | None = None
    created_at: datetime
    updated_at: datetime
    log_count: int = 0
    last_log_date: date | None = None


class MediaCheckItem(BaseModel):
    external_id: str
    type: MediaTypeEnum

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )


class MediaWithLogsResponse(MediaResponse):
    logs: list[MediaLogResponse] = []

    model_config = ConfigDict(from_attributes=True)
