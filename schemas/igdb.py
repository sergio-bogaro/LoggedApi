from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class IgdbSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(default=20, ge=1, le=50)


class IgdbSearchItem(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str
    cover_url: str
    first_release_date: str | None = None
    summary: str | None = None


class IgdbPlatform(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str
    abbreviation: str | None = None


class IgdbGenre(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str


class IgdbInvolvedCompany(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    company_id: int
    company_name: str
    developer: bool = False
    publisher: bool = False


class IgdbScreenshot(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    url: str


class IgdbArtwork(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    url: str


class IgdbVideo(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str
    video_id: str


class IgdbWebsite(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    type: int
    url: str


class IgdbGame(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    slug: str
    name: str
    summary: str | None = None
    storyline: str | None = None
    first_release_date: str | None = None
    rating: float | None = None
    total_rating_count: int | None = None
    cover_url: str = ""
    platforms: list[IgdbPlatform] = []
    genres: list[IgdbGenre] = []
    involved_companies: list[IgdbInvolvedCompany] = []
    screenshots: list[IgdbScreenshot] = []
    artworks: list[IgdbArtwork] = []
    videos: list[IgdbVideo] = []
    websites: list[IgdbWebsite] = []
