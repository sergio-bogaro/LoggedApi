from datetime import date, datetime

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.media import Media
from models.tag import Tag
from models.enums import MediaStatusEnum, MediaTypeEnum
from models.media_log import MediaLog
from schemas.media import MediaCheckItem, MediaCreate, MediaResponse, MediaUpdate, MediaWithLogsResponse
from services.image_storage import ImageStorageService


class MediaService:
    def __init__(self) -> None:
        self.image_service = ImageStorageService()

    def find_all(
        self,
        db: Session,
        user_id: int,
        media_type: MediaTypeEnum | None = None,
        status: MediaStatusEnum | None = None,
        search: str | None = None,
        tags: list[str] | None = None,
        has_logs: bool | None = None,
        limit: int | None = None,
    ) -> list[MediaResponse]:
        """Lista todas as mídias, com filtros opcionais."""
        query = select(Media).where(Media.user_id == user_id)

        if media_type:
            query = query.where(Media.type == media_type)
        if status:
            query = query.where(Media.status == status)
        if search:
            query = query.where(Media.title.ilike(f"%{search}%"))
        if tags:
            # Filtrar por tags - media deve ter TODAS as tags especificadas
            for tag_name in tags:
                query = query.join(Media.tags).where(
                    func.lower(Tag.name) == tag_name.strip().lower()
                )

        query = query.order_by(Media.updated_at.desc())
        results = db.execute(query).scalars().all()

        responses = [self._to_response(db, media) for media in results]

        if has_logs is True:
            responses = [r for r in responses if r.last_log_date is not None]

        if limit is not None and limit > 0:
            responses = responses[:limit]

        return responses

    def find_by_id(self, db: Session, media_id: int, user_id: int) -> MediaWithLogsResponse:
        """Busca uma mídia pelo ID, incluindo seus logs."""
        media = db.get(Media, media_id)
        if not media or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        return self._to_response_with_logs(db, media)

    def find_by_external_id(
        self, db: Session, external_id: str, media_type: MediaTypeEnum, user_id: int
    ) -> MediaResponse | None:
        query = select(Media).where(
            Media.external_id == external_id,
            Media.type == media_type,
            Media.user_id == user_id
        )
        media = db.execute(query).scalar_one_or_none()
        return self._to_response(db, media) if media else None

    def find_by_external_id_with_logs(
        self, db: Session, external_id: str, media_type: MediaTypeEnum, user_id: int
    ) -> MediaWithLogsResponse | None:
        query = select(Media).where(
            Media.external_id == external_id,
            Media.type == media_type,
            Media.user_id == user_id
        )
        media = db.execute(query).scalar_one_or_none()
        return self._to_response_with_logs(db, media) if media else None

    def batch_check_existing(self, db: Session, items: list[MediaCheckItem], user_id: int) -> dict[str, MediaResponse]:
        if not items:
            return {}

        conditions = [
            (Media.external_id == item.external_id) & (Media.type == item.type) & (Media.user_id == user_id)
            for item in items
        ]
        
        if len(conditions) == 1:
            query = select(Media).where(conditions[0])
        else:
            from sqlalchemy import or_
            query = select(Media).where(or_(*conditions))
        
        medias = db.execute(query).scalars().all()
        
        result = {}
        for media in medias:
            key = f"{media.external_id}:{media.type.value}"
            result[key] = self._to_response(db, media)
        
        return result

    def create(self, db: Session, data: MediaCreate) -> MediaResponse:
        # Cria nova midia verificando se já existe outra com mesmo external_id e tipo
        existing = self.find_by_external_id(db, data.external_id, data.type, data.user_id)
        if existing:
            raise HTTPException(
                status_code=409, detail="Esta mídia já está na sua biblioteca"
            )

        payload = data.model_dump(exclude={'tags'})
        tag_names = data.tags or []

        media = Media(**payload)
        
        # Gerenciar tags
        if tag_names:
            media.tags = self._get_or_create_tags(db, tag_names)
        
        db.add(media)
        db.commit()
        db.refresh(media)

        return self._to_response(db, media)

    def update(self, db: Session, media_id: int, data: MediaUpdate, user_id: int) -> MediaResponse:
        """Atualiza uma mídia existente."""
        media = db.get(Media, media_id)
        if not media or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        update_data = data.model_dump(exclude_unset=True, exclude={'tags'})
        
        # Atualizar tags se fornecidas
        if data.tags is not None:
            media.tags = self._get_or_create_tags(db, data.tags)
        
        for field, value in update_data.items():
            setattr(media, field, value)

        db.commit()
        db.refresh(media)

        return self._to_response(db, media)

    async def upload_image(
        self, db: Session, media_id: int, file: UploadFile, user_id: int
    ) -> MediaResponse:
        """Faz upload de uma imagem e associa a uma mídia."""
        media = db.get(Media, media_id)
        if not media or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        # Remove imagem anterior se existir
        if media.image_path:
            self.image_service.delete(media.image_path)

        filename = await self.image_service.store(file)
        media.image_path = filename
        db.commit()
        db.refresh(media)

        return self._to_response(db, media)

    def delete(self, db: Session, media_id: int, user_id: int) -> None:
        """Remove uma mídia e sua imagem associada."""
        media = db.get(Media, media_id)
        if not media or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        if media.image_path:
            self.image_service.delete(media.image_path)

        db.delete(media)
        db.commit()

    def _get_log_count(self, db: Session, media_id: int) -> int:
        result = db.execute(
            select(func.count()).where(MediaLog.media_id == media_id)
        ).scalar()
        return result or 0

    def _get_last_log_date(self, media: Media) -> date | None:
        """Retorna a data do log mais recente de uma mídia."""
        if not media.logs:
            return None
        return max(log.date for log in media.logs if log.date is not None)

    def _to_response(self, db: Session, media: Media) -> MediaResponse:
        return MediaResponse(
            id=media.id,
            user_id=media.user_id,
            external_id=media.external_id,
            title=media.title,
            type=media.type,
            status=media.status, # type: ignore
            description=media.description,
            cover_url=media.cover_url,
            image_path=media.image_path,
            release_date=media.release_date,
            rating=media.rating,
            review=media.review,
            created_at=media.created_at,
            updated_at=media.updated_at,
            log_count=self._get_log_count(db, media.id),
            last_log_date=self._get_last_log_date(media),
            tags=[tag.name for tag in media.tags],
        )
    # TODO: Validar esses ignonore 
    def _to_response_with_logs(self, db: Session, media: Media) -> MediaWithLogsResponse:
        return MediaWithLogsResponse(
            id=media.id,
            user_id=media.user_id,
            external_id=media.external_id,
            title=media.title,
            type=media.type,
            status=media.status,
            description=media.description,
            cover_url=media.cover_url,
            image_path=media.image_path,
            release_date=media.release_date,
            rating=media.rating,
            review=media.review,
            created_at=media.created_at,
            updated_at=media.updated_at,
            log_count=self._get_log_count(db, media.id),
            last_log_date=self._get_last_log_date(media),
            logs=media.logs,
            tags=[tag.name for tag in media.tags],
        )

    def _get_or_create_tags(self, db: Session, tag_names: list[str]) -> list[Tag]:
        """Busca ou cria tags pelo nome."""
        tags = []
        for name in tag_names:
            # Normalizar: remover espaços extras e converter para lowercase
            normalized_name = name.strip().lower()
            if not normalized_name:
                continue
                
            # Buscar tag existente
            query = select(Tag).where(func.lower(Tag.name) == normalized_name)
            tag = db.execute(query).scalar_one_or_none()
            
            # Criar se não existir
            if not tag:
                tag = Tag(name=normalized_name)
                db.add(tag)
            
            tags.append(tag)
        
        return tags

    async def remove_image(self, db: Session, media_id: int, user_id: int) -> MediaResponse:
      """Remove a imagem customizada de uma mídia."""
      media = db.get(Media, media_id)
      if not media or media.user_id != user_id:
          raise HTTPException(status_code=404, detail="Mídia não encontrada")

      if media.image_path:
          self.image_service.delete(media.image_path)
          media.image_path = None
          db.commit()
          db.refresh(media)

      return self._to_response(db, media)
