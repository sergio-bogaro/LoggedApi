"""performance indexes

Revision ID: 0003_perf_indexes
Revises: 0002_cleanup_legacy
Create Date: 2026-11-19

Índices compostos para as ordenações mais usadas em media_log:
- histórico por mídia (`media_id`, `date`)
- histórico por usuário (`user_id`, `date`)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_perf_indexes"
down_revision: Union[str, None] = "0002_cleanup_legacy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {row["name"] for row in inspector.get_indexes("media_log")}


def upgrade() -> None:
    # Bancos antigos podem já ter os índices (criados antes do Alembic).
    existing = _index_names()
    if "ix_media_log_media_date" not in existing:
        op.create_index(
            "ix_media_log_media_date",
            "media_log",
            ["media_id", "date"],
            unique=False,
        )
    if "ix_media_log_user_date" not in existing:
        op.create_index(
            "ix_media_log_user_date",
            "media_log",
            ["user_id", "date"],
            unique=False,
        )


def downgrade() -> None:
    existing = _index_names()
    if "ix_media_log_user_date" in existing:
        op.drop_index("ix_media_log_user_date", table_name="media_log")
    if "ix_media_log_media_date" in existing:
        op.drop_index("ix_media_log_media_date", table_name="media_log")
