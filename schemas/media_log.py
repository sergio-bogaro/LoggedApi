from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from models.enums import MediaStatusEnum


class MediaLogBase(BaseModel):
    date: date
    status: MediaStatusEnum | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )


class MediaLogCreate(MediaLogBase):
    media_id: int


class MediaLogUpdate(BaseModel):
    date: date | None = None
    status: MediaStatusEnum | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None


class MediaLogResponse(MediaLogBase):
    id: int
    media_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
