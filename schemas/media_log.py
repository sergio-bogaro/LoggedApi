from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class MediaLogBase(BaseModel):
    date: date
    progress: str | None = Field(None, max_length=255)
    rating: float | None = Field(None, ge=0, le=10)
    notes: str | None = None


class MediaLogCreate(MediaLogBase):
    media_id: int


class MediaLogUpdate(BaseModel):
    date: date | None = None
    progress: str | None = Field(None, max_length=255)
    rating: float | None = Field(None, ge=0, le=10)
    notes: str | None = None


class MediaLogResponse(MediaLogBase):
    id: int
    media_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
