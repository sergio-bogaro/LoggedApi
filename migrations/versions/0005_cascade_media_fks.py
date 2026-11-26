"""cascade media foreign keys

Revision ID: 0005_cascade_media_fks
Revises: 0004_drop_suwayomi_schedule
Create Date: 2026-11-26

`media`, `media_log` e `media_list_items` passam a usar ON DELETE CASCADE
para `users` e `media`. Isso mantém o banco consistente mesmo em DELETE raw
(restauração/manutenção), sem depender só do cascade do ORM.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0005_cascade_media_fks"
down_revision: Union[str, None] = "0004_drop_suwayomi_schedule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cascade_media_user() -> None:
    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.drop_constraint("fk_media_user_id_users", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_media_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )


def _cascade_media_log() -> None:
    with op.batch_alter_table("media_log", schema=None) as batch_op:
        batch_op.drop_constraint("fk_media_log_user_id_users", type_="foreignkey")
        batch_op.drop_constraint("fk_media_log_media_id_media", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_media_log_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_media_log_media_id_media",
            "media",
            ["media_id"],
            ["id"],
            ondelete="CASCADE",
        )


def _cascade_media_list_items() -> None:
    with op.batch_alter_table("media_list_items", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_media_list_items_user_id_users", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_media_list_items_media_id_media", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_media_list_items_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_media_list_items_media_id_media",
            "media",
            ["media_id"],
            ["id"],
            ondelete="CASCADE",
        )


def upgrade() -> None:
    _cascade_media_user()
    _cascade_media_log()
    _cascade_media_list_items()


def _restore_media_user() -> None:
    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.drop_constraint("fk_media_user_id_users", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_media_user_id_users",
            "users",
            ["user_id"],
            ["id"],
        )


def _restore_media_log() -> None:
    with op.batch_alter_table("media_log", schema=None) as batch_op:
        batch_op.drop_constraint("fk_media_log_user_id_users", type_="foreignkey")
        batch_op.drop_constraint("fk_media_log_media_id_media", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_media_log_user_id_users",
            "users",
            ["user_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_media_log_media_id_media",
            "media",
            ["media_id"],
            ["id"],
        )


def _restore_media_list_items() -> None:
    with op.batch_alter_table("media_list_items", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_media_list_items_user_id_users", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_media_list_items_media_id_media", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_media_list_items_user_id_users",
            "users",
            ["user_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_media_list_items_media_id_media",
            "media",
            ["media_id"],
            ["id"],
        )


def downgrade() -> None:
    _restore_media_list_items()
    _restore_media_log()
    _restore_media_user()
