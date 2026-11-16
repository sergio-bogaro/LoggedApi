"""Integração com o Suwayomi-Server.

Três tabelas:
- `suwayomi_accounts`: conexão e agendamento por usuário.
- `suwayomi_category_mappings`: o que cada categoria do Suwayomi vira no Logged.
- `suwayomi_manga_links`: vínculo entre um mangá do Suwayomi e uma `Media`, com o
  último estado aplicado para que o sync só aja quando a categoria mudar.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from database import Base
from models.enums import MediaStatusEnum


class SuwayomiAccount(Base):
    __tablename__ = "suwayomi_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)

    # Credenciais opcionais (write-only; nunca expostas nas respostas). O
    # Suwayomi usa HTTP Basic quando authMode = BASIC_AUTH.
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updates_per_day: Mapped[int] = mapped_column(Integer, default=4, nullable=False)

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # never | ok | partial | error
    last_sync_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_sync_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<SuwayomiAccount(id={self.id}, user_id={self.user_id})>"


class SuwayomiCategoryMapping(Base):
    __tablename__ = "suwayomi_category_mappings"
    __table_args__ = (
        UniqueConstraint("user_id", "category_id", name="uq_suwayomi_category_user_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[int] = mapped_column(Integer, nullable=False)
    category_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ignore | backlog | status
    action: Mapped[str] = mapped_column(String(20), default="ignore", nullable=False)
    status: Mapped[MediaStatusEnum | None] = mapped_column(
        Enum(MediaStatusEnum), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<SuwayomiCategoryMapping(id={self.id}, category_id={self.category_id}, "
            f"action='{self.action}')>"
        )


class SuwayomiMangaLink(Base):
    __tablename__ = "suwayomi_manga_links"
    __table_args__ = (
        UniqueConstraint("user_id", "suwayomi_manga_id", name="uq_suwayomi_link_user_manga"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    suwayomi_manga_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    media_id: Mapped[int | None] = mapped_column(
        ForeignKey("media.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)

    # Última ação aplicada pelo sync (ignore | backlog | status) e o status
    # correspondente. Serve para aplicar somente quando a categoria mudar.
    last_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_status: Mapped[MediaStatusEnum | None] = mapped_column(
        Enum(MediaStatusEnum), nullable=True
    )
    # True quando o item do backlog foi criado pelo sync (e pode ser removido
    # por ele ao sair da categoria).
    backlog_owned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    match_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<SuwayomiMangaLink(id={self.id}, suwayomi_manga_id={self.suwayomi_manga_id}, "
            f"media_id={self.media_id})>"
        )
