from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, Table, Column, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.media import Media


# Tabela de associação Many-to-Many entre Media e Tag
media_tags = Table(
    "media_tags",
    Base.metadata,
    Column("media_id", Integer, ForeignKey("media.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)

    # Relacionamento Many-to-Many com Media
    media: Mapped[list["Media"]] = relationship(
        "Media", secondary=media_tags, back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<Tag(id={self.id}, name='{self.name}')>"
