"""cleanup legacy

Revision ID: 0002_cleanup_legacy
Revises: 0001_baseline
Create Date: 2026-11-19

Limpeza idempotente para bancos antigos restaurados de backup.

- `custom_views` pertence a uma funcionalidade descontinuada.
- `media_log.progress` / `progress_total` são preservadas **de propósito**: o
  app não as usa, mas o histórico antigo não deve ser destruído por uma migração.
  SQLAlchemy ignora colunas extras, então mantê-las é inofensivo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_cleanup_legacy"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "custom_views" in inspector.get_table_names():
        op.drop_table("custom_views")


def downgrade() -> None:
    # Tabela descontinuada: não há o que recriar.
    pass
