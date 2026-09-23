import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.media import Media
from models.media_log import MediaLog
from schemas.media_log import (
    MediaLogCreate,
    MediaLogResponse,
    MediaLogUpdate,
    MediaLogWithMediaResponse,
    MediaSummary,
)


class MediaLogService:
    def find_by_user(
        self,
        db: Session,
        user_id: int,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MediaLogWithMediaResponse]:
        """Lista todos os logs do usuário, do mais recente para o mais antigo.

        O join com `Media` é explícito em vez de `selectinload(MediaLog.media)`
        de propósito: `Media.logs` é lazy="selectin", então carregar cada mídia
        pelo ORM traria todos os logs dela junto — uma query extra por nada.
        """
        query = (
            select(MediaLog, Media)
            .join(Media, MediaLog.media_id == Media.id)
            .where(MediaLog.user_id == user_id)
            .order_by(MediaLog.date.desc(), MediaLog.id.desc())
        )

        if start is not None:
            query = query.where(MediaLog.date >= start)
        if end is not None:
            query = query.where(MediaLog.date <= end)

        if offset:
            query = query.offset(offset)
        if limit is not None and limit > 0:
            query = query.limit(limit)

        rows = db.execute(query).all()

        return [
            MediaLogWithMediaResponse(
                id=log.id,
                user_id=log.user_id,
                media_id=log.media_id,
                date=log.date,
                status=log.status,
                rating=log.rating,
                review=log.review,
                start_date=log.start_date,
                end_date=log.end_date,
                created_at=log.created_at,
                media=MediaSummary.model_validate(media),
            )
            for log, media in rows
        ]

    def find_by_media(self, db: Session, media_id: int, user_id: int) -> list[MediaLogResponse]:
        """Lista todos os logs de uma mídia específica."""
        media = db.get(Media, media_id)
        if not media or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        query = (
            select(MediaLog)
            .where(MediaLog.media_id == media_id)
            .order_by(MediaLog.date.desc())
        )
        results = db.execute(query).scalars().all()

        return [MediaLogResponse.model_validate(log) for log in results]

    def find_by_id(self, db: Session, log_id: int, user_id: int) -> MediaLogResponse:
        """Busca um log pelo ID."""
        log = db.get(MediaLog, log_id)
        if not log or log.user_id != user_id:
            raise HTTPException(status_code=404, detail="Log não encontrado")

        return MediaLogResponse.model_validate(log)

    def create(self, db: Session, data: MediaLogCreate) -> MediaLogResponse:
        """Cria um novo log para uma mídia."""
        media = db.get(Media, data.media_id)
        if not media:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        log = MediaLog(**data.model_dump())
        db.add(log)
        db.commit()
        db.refresh(log)

        return MediaLogResponse.model_validate(log)

    def update(self, db: Session, log_id: int, data: MediaLogUpdate, user_id: int) -> MediaLogResponse:
        """Atualiza um log existente."""
        log = db.get(MediaLog, log_id)
        if not log or log.user_id != user_id:
            raise HTTPException(status_code=404, detail="Log não encontrado")

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(log, field, value)

        db.commit()
        db.refresh(log)

        return MediaLogResponse.model_validate(log)

    def delete(self, db: Session, log_id: int, user_id: int) -> None:
        """Remove um log."""
        log = db.get(MediaLog, log_id)
        if not log or log.user_id != user_id:
            raise HTTPException(status_code=404, detail="Log não encontrado")

        db.delete(log)
        db.commit()
