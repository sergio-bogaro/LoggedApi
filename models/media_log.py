from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class MediaLog(Base):
    __tablename__ = "media_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[int] = mapped_column(Integer, ForeignKey("media.id"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    progress: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    media: Mapped["Media"] = relationship("Media", back_populates="logs")

    def __repr__(self) -> str:
        return f"<MediaLog(id={self.id}, media_id={self.media_id}, date={self.date})>"
