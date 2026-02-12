from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.media import Media
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
        media_type: MediaTypeEnum | None = None,
        status: MediaStatusEnum | None = None,
        search: str | None = None,
    ) -> list[MediaResponse]:
        """Lista todas as mídias, com filtros opcionais."""
        query = select(Media)

        if media_type:
            query = query.where(Media.type == media_type)
        if status:
            query = query.where(Media.status == status)
        if search:
            query = query.where(Media.title.ilike(f"%{search}%"))

        query = query.order_by(Media.updated_at.desc())
        results = db.execute(query).scalars().all()

        return [self._to_response(db, media) for media in results]

    def find_by_id(self, db: Session, media_id: int) -> MediaWithLogsResponse:
        """Busca uma mídia pelo ID, incluindo seus logs."""
        media = db.get(Media, media_id)
        if not media:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        return self._to_response_with_logs(db, media)

    def find_by_external_id(
        self, db: Session, external_id: str, media_type: MediaTypeEnum
    ) -> MediaResponse | None:
        """Busca uma mídia pelo ID externo (API) e tipo."""
        query = select(Media).where(
            Media.external_id == external_id, Media.type == media_type
        )
        media = db.execute(query).scalar_one_or_none()
        return self._to_response(db, media) if media else None

    def batch_check_existing(self, db: Session, items: list[MediaCheckItem]) -> dict[str, MediaResponse]:
        if not items:
            return {}

        conditions = [
            (Media.external_id == item.external_id) & (Media.type == item.type)
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
        existing = self.find_by_external_id(db, data.external_id, data.type)
        if existing:
            raise HTTPException(
                status_code=409, detail="Esta mídia já está na sua biblioteca"
            )

        media = Media(**data.model_dump())
        db.add(media)
        db.commit()
        db.refresh(media)

        return self._to_response(db, media)

    def update(self, db: Session, media_id: int, data: MediaUpdate) -> MediaResponse:
        """Atualiza uma mídia existente."""
        media = db.get(Media, media_id)
        if not media:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(media, field, value)

        db.commit()
        db.refresh(media)

        return self._to_response(db, media)

    async def upload_image(
        self, db: Session, media_id: int, file: UploadFile
    ) -> MediaResponse:
        """Faz upload de uma imagem e associa a uma mídia."""
        media = db.get(Media, media_id)
        if not media:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        # Remove imagem anterior se existir
        if media.image_path:
            self.image_service.delete(media.image_path)

        filename = await self.image_service.store(file)
        media.image_path = filename
        db.commit()
        db.refresh(media)

        return self._to_response(db, media)

    def delete(self, db: Session, media_id: int) -> None:
        """Remove uma mídia e sua imagem associada."""
        media = db.get(Media, media_id)
        if not media:
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

    def _to_response(self, db: Session, media: Media) -> MediaResponse:
        return MediaResponse(
            id=media.id,
            external_id=media.external_id,
            title=media.title,
            type=media.type,
            status=media.status, # type: ignore
            description=media.description,
            cover_url=media.cover_url,
            image_path=media.image_path,
            rating=media.rating,
            created_at=media.created_at,
            updated_at=media.updated_at,
            log_count=self._get_log_count(db, media.id),
        )
    # TODO: Validar esses ignonore 
    def _to_response_with_logs(self, db: Session, media: Media) -> MediaWithLogsResponse:
        return MediaWithLogsResponse(
            id=media.id,
            external_id=media.external_id,
            title=media.title,
            type=media.type,
            status=media.status, # type: ignore
            description=media.description,
            cover_url=media.cover_url,
            image_path=media.image_path,
            rating=media.rating,
            created_at=media.created_at,
            updated_at=media.updated_at,
            log_count=self._get_log_count(db, media.id),
            logs=media.logs, # type: ignore
        )
