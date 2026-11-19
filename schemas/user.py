from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from domain_errors import DomainError

_NON_NULLABLE_UPDATE_FIELDS = (
    "username",
    "password",
    "rating_mode",
    "view_mode",
    "track_movies",
    "track_anime",
    "track_manga",
    "track_games",
    "track_books",
    "track_music",
    "track_series",
)


class UserBase(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    username: str = Field(..., min_length=3, max_length=100)


class UserCreate(UserBase):
    password: str = Field(..., min_length=3, max_length=255)


class UserLogin(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    username: str
    password: str


class UserSettings(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    rating_mode: str = "stars5"  # "numeric", "stars5", "stars10"
    view_mode: str = "list"  # "list", "grid"
    track_movies: bool = True
    track_anime: bool = True
    track_manga: bool = True
    track_games: bool = True
    track_books: bool = True
    track_music: bool = True
    track_series: bool = True


class UserUpdate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    username: str | None = Field(None, min_length=3, max_length=100)
    password: str | None = Field(None, min_length=3, max_length=255)
    rating_mode: Literal["numeric", "stars5", "stars10"] | None = None
    view_mode: Literal["list", "grid"] | None = None
    track_movies: bool | None = None
    track_anime: bool | None = None
    track_manga: bool | None = None
    track_games: bool | None = None
    track_books: bool | None = None
    track_music: bool | None = None
    track_series: bool | None = None
    # Chaves de integração por usuário (write-only; campos enviados vazios limpam a chave)
    tmdb_api_key: str | None = None
    igdb_client_id: str | None = None
    igdb_client_secret: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_null_non_nullable_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        for field in _NON_NULLABLE_UPDATE_FIELDS:
            for key in (field, to_camel(field)):
                if key in data and data[key] is None:
                    raise DomainError(f"O campo '{key}' não pode ser nulo.")
        return data


class UserResponse(UserBase):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    id: int
    created_at: datetime
    updated_at: datetime
    rating_mode: str
    view_mode: str
    track_movies: bool
    track_anime: bool
    track_manga: bool
    track_games: bool
    track_books: bool
    track_music: bool
    track_series: bool


class LoginResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user: UserResponse
    message: str = "Login successful"
