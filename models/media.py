from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ConfigDict
from sqlalchemy import DateTime, Enum, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base
from models.enums import MediaStatusEnum, MediaTypeEnum
from pydantic.alias_generators import to_camel

if TYPE_CHECKING:
    from models.media_log import MediaLog


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[MediaTypeEnum] = mapped_column(Enum(MediaTypeEnum), nullable=False, index=True)
    cover_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    release_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # TODO: Pensar melhor na estrutura desses campos, talvez adicionar na tabela de logs ou criar uma tabela de status/rating/notes separados
    status: Mapped[MediaStatusEnum | None] = mapped_column(Enum(MediaStatusEnum), nullable=False, default=MediaStatusEnum.BACKLOG)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review: Mapped[str | None] = mapped_column(Text, nullable=True)

    logs: Mapped[list["MediaLog"]] = relationship(
        "MediaLog", back_populates="media", cascade="all, delete-orphan", lazy="selectin"
    )

    model_config = ConfigDict (
        alias_generator=to_camel,  # Converte automaticamente
        populate_by_name=True
    )
    

    def __repr__(self) -> str:
        return f"<Media(id={self.id}, title='{self.title}', type={self.type})>"
