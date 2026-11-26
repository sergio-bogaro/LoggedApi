"""drop Suwayomi per-account schedule

Revision ID: 0004_drop_suwayomi_schedule
Revises: 0003_perf_indexes
Create Date: 2026-11-26

O Suwayomi passou a usar exclusivamente o agendamento do sync global de
biblioteca. As colunas de frequência própria não têm mais efeito e foram
removidas para evitar uma segunda agenda concorrente.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_drop_suwayomi_schedule"
down_revision: Union[str, None] = "0003_perf_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {row["name"] for row in inspector.get_columns("suwayomi_accounts")}


def upgrade() -> None:
    existing = _columns()
    with op.batch_alter_table("suwayomi_accounts", schema=None) as batch_op:
        if "next_sync_at" in existing:
            batch_op.drop_column("next_sync_at")
        if "updates_per_day" in existing:
            batch_op.drop_column("updates_per_day")


def downgrade() -> None:
    existing = _columns()
    with op.batch_alter_table("suwayomi_accounts", schema=None) as batch_op:
        if "updates_per_day" not in existing:
            batch_op.add_column(
                sa.Column(
                    "updates_per_day",
                    sa.Integer(),
                    nullable=False,
                    server_default="4",
                )
            )
        if "next_sync_at" not in existing:
            batch_op.add_column(
                sa.Column("next_sync_at", sa.DateTime(), nullable=True)
            )
