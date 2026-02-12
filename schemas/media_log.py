from typing import TYPE_CHECKING

import datetime
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from models.enums import MediaStatusEnum

if TYPE_CHECKING:
    from schemas.media_log import MediaLogResponse


class MediaLogBase(BaseModel):
    date: datetime.date
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
    date: datetime.date | None = None
    status: MediaStatusEnum | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None


class MediaLogResponse(MediaLogBase):
    id: int
    media_id: int
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)
