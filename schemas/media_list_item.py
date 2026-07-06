from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from models.enums import MediaTypeEnum


class MediaListItemMedia(BaseModel):
    """Media details embedded in a list item response."""
    id: int
    external_id: str
    title: str
    description: str | None = None
    cover_url: str | None = None
    image_path: str | None = None
    release_date: datetime | None = None
    type: MediaTypeEnum

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class MediaListItemCreate(BaseModel):
    user_id: int
    media_type: MediaTypeEnum
    external_id: str = Field(..., min_length=1, max_length=255)
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    cover_url: str | None = None
    release_date: datetime | None = None
    date_log: datetime | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class MediaListItemResponse(BaseModel):
    id: int
    user_id: int
    media_type: MediaTypeEnum
    media_id: int
    list_type: str
    date_log: datetime
    media: MediaListItemMedia | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class MediaListItemBatchCreate(BaseModel):
    items: list[MediaListItemCreate]

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class MediaListItemCheckResponse(BaseModel):
    in_list: bool
    item_id: int | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
