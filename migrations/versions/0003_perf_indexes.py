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


revision: str = "0003_perf_indexes"
down_revision: Union[str, None] = "0002_cleanup_legacy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_media_log_media_date", "media_log", ["media_id", "date"], unique=False
    )
    op.create_index(
        "ix_media_log_user_date", "media_log", ["user_id", "date"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_media_log_user_date", table_name="media_log")
    op.drop_index("ix_media_log_media_date", table_name="media_log")
