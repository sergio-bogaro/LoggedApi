from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ConfigDict
from sqlalchemy import DateTime, Integer, String, func, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from database import Base
from models.enums import MediaTypeEnum
from pydantic.alias_generators import to_camel

if TYPE_CHECKING:
    from models.media import Media
    from models.user import User


class MediaListItem(Base):
    __tablename__ = "media_list_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    media_type: Mapped[MediaTypeEnum] = mapped_column(String(20), nullable=False, index=True)
    media_id: Mapped[int] = mapped_column(Integer, ForeignKey("media.id"), nullable=False, index=True)
    list_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    date_log: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    def __repr__(self) -> str:
        return (
            f"<MediaListItem(id={self.id}, user_id={self.user_id}, "
            f"media_type={self.media_type}, media_id={self.media_id}, "
            f"list_type={self.list_type})>"
        )
