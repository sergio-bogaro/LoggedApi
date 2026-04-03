from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base
from models.enums import MediaTypeEnum

if TYPE_CHECKING:
    from models.media import Media
    from models.media_log import MediaLog
    from models.custom_view import CustomView


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Configurações do usuário
    rating_mode: Mapped[str] = mapped_column(String(50), default="stars5", nullable=False)  # "numeric", "stars5", "stars10"
    view_mode: Mapped[str] = mapped_column(String(50), default="list", nullable=False)  # "list", "grid"
    
    # Medias que o usuário quer logar (armazenado como flags booleanas)
    track_movies: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    track_anime: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    track_manga: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    track_games: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    track_books: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relacionamentos
    media: Mapped[list["Media"]] = relationship(
        "Media", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    
    logs: Mapped[list["MediaLog"]] = relationship(
        "MediaLog", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    
    custom_views: Mapped[list["CustomView"]] = relationship(
        "CustomView", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username={self.username}, email={self.email})>"
