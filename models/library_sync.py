"""Conexões de sincronização com serviços externos de log de mídia.

Diferente de `suwayomi_*` (que é específico do servidor de mangá), estas tabelas
são genéricas: cada provedor tem uma conexão, um mapeamento de status e links
para as mídias do Logged.
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
from models.enums import MediaStatusEnum, MediaTypeEnum

class LibraryConnection(Base):
    __tablename__ = "library_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_library_connection_user_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updates_per_day: Mapped[int] = mapped_column(Integer, default=4, nullable=False)

    # Credenciais e dados de sessão. Nunca é devolvido pela API.
    auth_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # Cursor incremental (último timestamp, GUIDs vistos etc.).
    cursor_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    baseline_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_sync_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    needs_reauth: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<LibraryConnection(id={self.id}, provider='{self.provider}')>"


class LibrarySyncLink(Base):
    __tablename__ = "library_sync_links"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", "source_id", name="uq_library_sync_link_source"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    media_id: Mapped[int | None] = mapped_column(
        ForeignKey("media.id", ondelete="SET NULL"), nullable=True, index=True
    )
    media_type: Mapped[MediaTypeEnum | None] = mapped_column(
        Enum(MediaTypeEnum), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    # Último status lido da origem e última ação aplicada no Logged.
    source_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_source_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_status: Mapped[MediaStatusEnum | None] = mapped_column(
        Enum(MediaStatusEnum), nullable=True
    )

    backlog_owned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    match_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_match_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    match_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<LibrarySyncLink(provider='{self.provider}', source_id='{self.source_id}')>"


class LibraryStatusMapping(Base):
    __tablename__ = "library_status_mappings"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", "source_status", name="uq_library_status_mapping"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_status: Mapped[str] = mapped_column(String(80), nullable=False)
    source_label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ignore | backlog | in_progress | following | on_hold | finished | dropped
    action: Mapped[str] = mapped_column(String(30), default="ignore", nullable=False)
    status: Mapped[MediaStatusEnum | None] = mapped_column(
        Enum(MediaStatusEnum), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<LibraryStatusMapping(provider='{self.provider}', "
            f"source_status='{self.source_status}', action='{self.action}')>"
        )
