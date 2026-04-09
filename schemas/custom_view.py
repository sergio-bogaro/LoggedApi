from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CustomViewBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    icon: str | None = None  # emoji ou nome do ícone
    color: str | None = None  # cor em hex (#FF5733)
    order: int = 0
    is_visible: bool = True
    is_pinned: bool = False
    filters: dict | None = None
    display_settings: dict | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class CustomViewCreate(CustomViewBase):
    user_id: int


class CustomViewUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    order: int | None = None
    is_visible: bool | None = None
    is_pinned: bool | None = None
    filters: dict | None = None
    display_settings: dict | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class CustomViewResponse(CustomViewBase):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class CustomViewReorder(BaseModel):
    """Schema para reordenar visões"""
    view_id: int
    new_order: int


# Exemplos de estruturas para filters e display_settings
"""
filters = {
    "media_types": ["anime", "manga"],  # Tipos de media
    "status": ["in_progress", "following"],  # Status das medias
    "min_rating": 8.0,  # Nota mínima
    "max_rating": 10.0,  # Nota máxima
    "tags": ["action", "comedy"],  # Tags específicas
    "year_from": 2020,  # Ano de lançamento mínimo
    "year_to": 2024,  # Ano de lançamento máximo
}

display_settings = {
    "view_mode": "grid",  # "list" ou "grid"
    "sort_by": "rating",  # "title", "rating", "created_at", "release_date"
    "sort_order": "desc",  # "asc" ou "desc"
    "show_completed": True,  # Mostrar completos
    "items_per_page": 20,  # Itens por página
}
"""
