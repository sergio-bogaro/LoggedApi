from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


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


class UserUpdate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    username: str | None = None
    password: str | None = None
    rating_mode: str | None = None
    view_mode: str | None = None
    track_movies: bool | None = None
    track_anime: bool | None = None
    track_manga: bool | None = None
    track_games: bool | None = None
    track_books: bool | None = None


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


class LoginResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user: UserResponse
    message: str = "Login successful"
