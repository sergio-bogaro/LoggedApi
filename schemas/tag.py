from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TagBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )


class TagCreate(TagBase):
    pass


class TagResponse(TagBase):
    id: int

    model_config = ConfigDict(
        from_attributes=True,
        alias_generator=to_camel,
        populate_by_name=True
    )
