"""Importações executadas em background.

O job guarda a entrada (arquivo/URL/username), o progresso e o resultado. Cada
item guarda a entrada normalizada, o match e a decisão de revisão do usuário.
"""

from datetime import date, datetime
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
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

class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    media_type: Mapped[MediaTypeEnum | None] = mapped_column(
        Enum(MediaTypeEnum), nullable=True
    )
    # file | username | url
    input_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # queued | parsing | matching | ready | done | failed | cancelled
    status: Mapped[str] = mapped_column(
        String(20), default="queued", nullable=False, index=True
    )
    progress_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(500), nullable=True)

    counts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<ImportJob(id={self.id}, provider='{self.provider}', status='{self.status}')>"


class ImportJobItem(Base):
    __tablename__ = "import_job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "key", name="uq_import_job_item_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[MediaTypeEnum] = mapped_column(
        Enum(MediaTypeEnum), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    external_refs_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    status: Mapped[MediaStatusEnum | None] = mapped_column(
        Enum(MediaStatusEnum), nullable=True
    )
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    logs_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    in_backlog: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    backlog_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # matched | ambiguous | not_found | already_in_library | error
    match_status: Mapped[str | None] = mapped_column(
        String(30), nullable=True, index=True
    )
    match_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    existing_media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    selected_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    commit_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    commit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    backlog_added: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ImportJobItem(id={self.id}, job_id={self.job_id}, title='{self.title}')>"
        )
