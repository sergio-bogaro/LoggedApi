import datetime
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from models.enums import MediaStatusEnum

class MediaLogBase(BaseModel):
    date: datetime.date
    status: MediaStatusEnum | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None
    start_date: datetime.datetime | None = None
    end_date: datetime.datetime | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )


class MediaLogCreate(MediaLogBase):
    user_id: int
    media_id: int
    date: datetime.date = Field(default_factory=datetime.date.today)


class MediaLogUpdate(BaseModel):
    date: datetime.date | None = None
    status: MediaStatusEnum | None = None
    rating: float | None = Field(None, ge=0, le=10)
    review: str | None = None
    start_date: datetime.datetime | None = None
    end_date: datetime.datetime | None = None


class MediaLogResponse(MediaLogBase):
    id: int
    user_id: int
    media_id: int
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)
