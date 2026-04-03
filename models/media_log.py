from typing import TYPE_CHECKING

import datetime
from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base
from models.enums import MediaStatusEnum

if TYPE_CHECKING:
    from models.media import Media
    from models.user import User


class MediaLog(Base):
    __tablename__ = "media_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    media_id: Mapped[int] = mapped_column(Integer, ForeignKey("media.id"), nullable=False, index=True)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False, default=datetime.date.today)

    status: Mapped[MediaStatusEnum | None] = mapped_column(Enum(MediaStatusEnum), nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    start_date: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="logs")
    media: Mapped["Media"] = relationship("Media", back_populates="logs")

    def __repr__(self) -> str:
        return f"<MediaLog(id={self.id}, media_id={self.media_id}, date={self.date})>"
