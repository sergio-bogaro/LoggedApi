import datetime
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from models.enums import MediaStatusEnum, MediaTypeEnum

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


class MediaSummary(BaseModel):
    """Resumo da mídia embutido no log.

    Deliberadamente sem `logs` nem `tags`: `Media.logs` usa lazy="selectin", então
    serializar a mídia completa arrastaria todos os logs dela para dentro de cada
    log da listagem.
    """
    id: int
    external_id: str
    title: str
    type: MediaTypeEnum
    cover_url: str | None = None
    image_path: str | None = None
    release_date: datetime.datetime | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class MediaLogWithMediaResponse(MediaLogResponse):
    """Um log com a mídia a que pertence — a unidade do registro."""
    media: MediaSummary | None = None
