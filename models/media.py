import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class MediaTypeEnum(str, enum.Enum):
    MOVIES = "movies"
    MANGA = "manga"
    ANIME = "anime"
    GAME = "game"
    BOOK = "book"


class MediaStatusEnum(str, enum.Enum):
    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DROPPED = "dropped"
    ON_HOLD = "on_hold"


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[MediaTypeEnum] = mapped_column(Enum(MediaTypeEnum), nullable=False, index=True)
    status: Mapped[MediaStatusEnum] = mapped_column(
        Enum(MediaStatusEnum), nullable=False, default=MediaStatusEnum.BACKLOG
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    year: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    logs: Mapped[list["MediaLog"]] = relationship(
        "MediaLog", back_populates="media", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Media(id={self.id}, title='{self.title}', type={self.type})>"
