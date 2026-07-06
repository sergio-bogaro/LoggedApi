from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.media import Media
from models.media_list_item import MediaListItem
from schemas.media_list_item import (
    MediaListItemBatchCreate,
    MediaListItemCheckResponse,
    MediaListItemCreate,
    MediaListItemMedia,
    MediaListItemResponse,
)


class MediaListService:
    VALID_LIST_TYPES = {"favorites", "backlog"}

    @staticmethod
    def _validate_list_type(list_type: str) -> None:
        if list_type not in MediaListService.VALID_LIST_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid list_type: '{list_type}'. Must be one of: {MediaListService.VALID_LIST_TYPES}",
            )

    def get_list(self, db: Session, user_id: int, list_type: str) -> list[MediaListItemResponse]:
        """Lista todos os itens de uma lista (favorites/backlog) de um usuário."""
        self._validate_list_type(list_type)
        query = (
            select(MediaListItem)
            .where(MediaListItem.user_id == user_id, MediaListItem.list_type == list_type)
            .order_by(MediaListItem.date_log.desc())
        )
        results = db.execute(query).scalars().all()
        return [self._to_response(db, item) for item in results]

    def check_in_list(
        self, db: Session, user_id: int, external_id: str, media_type: str, list_type: str
    ) -> MediaListItemCheckResponse:
        """Verifica se uma mídia está em uma lista específica."""
        self._validate_list_type(list_type)

        media = self._find_media_by_external_id(db, external_id, media_type, user_id)
        if not media:
            return MediaListItemCheckResponse(in_list=False)

        query = select(MediaListItem).where(
            MediaListItem.user_id == user_id,
            MediaListItem.media_id == media.id,
            MediaListItem.list_type == list_type,
        )
        item = db.execute(query).scalar_one_or_none()
        if item:
            return MediaListItemCheckResponse(in_list=True, item_id=item.id)
        return MediaListItemCheckResponse(in_list=False)

    def add_to_list(
        self, db: Session, data: MediaListItemCreate, list_type: str
    ) -> MediaListItemResponse:
        """Adiciona uma mídia a uma lista (favorites/backlog). Cria a mídia se não existir."""
        self._validate_list_type(list_type)

        media = self._find_media_by_external_id(
            db, data.external_id, data.media_type.value, data.user_id
        )
        if not media:
            media = self._create_media(db, data)

        existing = self._find_in_list(db, data.user_id, media.id, list_type)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Esta mídia já está nesta lista",
            )

        item = MediaListItem(
            user_id=data.user_id,
            media_type=data.media_type,
            media_id=media.id,
            list_type=list_type,
            date_log=data.date_log or datetime.now(),
        )
        db.add(item)
        db.commit()
        db.refresh(item)

        return self._to_response(db, item)

    def batch_add(
        self, db: Session, items: list[MediaListItemCreate], list_type: str
    ) -> list[MediaListItemResponse]:
        """Adiciona múltiplas mídias a uma lista (favorites/backlog)."""
        self._validate_list_type(list_type)

        added = []
        for data in items:
            media = self._find_media_by_external_id(
                db, data.external_id, data.media_type.value, data.user_id
            )
            if not media:
                media = self._create_media(db, data)

            existing = self._find_in_list(db, data.user_id, media.id, list_type)
            if existing:
                continue

            item = MediaListItem(
                user_id=data.user_id,
                media_type=data.media_type,
                media_id=media.id,
                list_type=list_type,
                date_log=data.date_log or datetime.now(),
            )
            db.add(item)
            added.append(item)

        if added:
            db.commit()
            for item in added:
                db.refresh(item)

        return [self._to_response(db, item) for item in added]

    def remove_from_list(self, db: Session, item_id: int, user_id: int) -> None:
        """Remove um item de uma lista. Não remove a mídia."""
        item = db.get(MediaListItem, item_id)
        if not item or item.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Item não encontrado na lista",
            )
        db.delete(item)
        db.commit()

    def _find_media_by_external_id(
        self, db: Session, external_id: str, media_type: str, user_id: int
    ) -> Media | None:
        """Busca uma mídia pelo external_id, tipo e usuário."""
        query = select(Media).where(
            Media.external_id == external_id,
            Media.type == media_type,
            Media.user_id == user_id,
        )
        return db.execute(query).scalar_one_or_none()

    def _create_media(self, db: Session, data: MediaListItemCreate) -> Media:
        """Cria uma nova mídia a partir dos dados de um item de lista."""
        media = Media(
            user_id=data.user_id,
            external_id=data.external_id,
            title=data.title,
            type=data.media_type,
            description=data.description,
            cover_url=data.cover_url,
            release_date=data.release_date,
        )
        db.add(media)
        db.commit()
        db.refresh(media)
        return media

    def _find_in_list(
        self, db: Session, user_id: int, media_id: int, list_type: str
    ) -> MediaListItem | None:
        """Busca um item na lista por user_id, media_id e list_type."""
        query = select(MediaListItem).where(
            MediaListItem.user_id == user_id,
            MediaListItem.media_id == media_id,
            MediaListItem.list_type == list_type,
        )
        return db.execute(query).scalar_one_or_none()

    def _to_response(self, db: Session, item: MediaListItem) -> MediaListItemResponse:
        media = db.get(Media, item.media_id)
        media_detail = MediaListItemMedia(
            id=media.id,
            external_id=media.external_id,
            title=media.title,
            description=media.description,
            cover_url=media.cover_url,
            image_path=media.image_path,
            release_date=media.release_date,
            type=media.type,
        ) if media else None

        return MediaListItemResponse(
            id=item.id,
            user_id=item.user_id,
            media_type=item.media_type,
            media_id=item.media_id,
            list_type=item.list_type,
            date_log=item.date_log,
            media=media_detail,
        )
