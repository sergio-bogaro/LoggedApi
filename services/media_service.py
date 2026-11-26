from datetime import date, datetime

from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased, lazyload

from models.media import Media
from models.tag import Tag, media_tags
from models.enums import MediaStatusEnum, MediaTypeEnum
from models.media_log import MediaLog
from models.media_list_item import MediaListItem
from schemas.media import MediaCheckItem, MediaCreate, MediaResponse, MediaUpdate, MediaWithLogsResponse
from services.image_storage import ImageStorageService


class MediaService:
    def __init__(self) -> None:
        self.image_service = ImageStorageService()

    def find_all(
        self,
        db: Session,
        user_id: int,
        media_types: list[MediaTypeEnum] | None = None,
        status: MediaStatusEnum | None = None,
        search: str | None = None,
        tags: list[str] | None = None,
        has_logs: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MediaResponse]:
        """Lista as mídias do usuário com filtros, paginação e agregações no SQL."""
        log_count_sq = (
            select(func.count(MediaLog.id))
            .where(MediaLog.media_id == Media.id)
            .correlate(Media)
            .scalar_subquery()
        )
        last_log_date_sq = (
            select(func.max(MediaLog.date))
            .where(MediaLog.media_id == Media.id)
            .correlate(Media)
            .scalar_subquery()
        )
        latest_status_sq = (
            select(MediaLog.status)
            .where(MediaLog.media_id == Media.id)
            .order_by(MediaLog.date.desc(), MediaLog.id.desc())
            .limit(1)
            .correlate(Media)
            .scalar_subquery()
        )

        query = select(Media, log_count_sq, last_log_date_sq, latest_status_sq).where(
            Media.user_id == user_id
        )

        if media_types:
            query = query.where(Media.type.in_(media_types))
        if search:
            query = query.where(Media.title.ilike(f"%{search}%"))
        if tags:
            # Uma tag por alias: reutilizar o mesmo join faria a segunda condição
            # ser aplicada à mesma linha e o filtro AND nunca casaria.
            for tag_name in tags:
                tag_alias = aliased(Tag)
                query = query.join(Media.tags.of_type(tag_alias)).where(
                    func.lower(tag_alias.name) == tag_name.strip().lower()
                )
        if has_logs is True:
            query = query.where(last_log_date_sq.is_not(None))
        elif has_logs is False:
            query = query.where(last_log_date_sq.is_(None))
        if status is not None:
            query = query.where(
                or_(
                    Media.status == status,
                    and_(Media.status.is_(None), latest_status_sq == status),
                )
            )

        if has_logs is True:
            query = query.order_by(last_log_date_sq.desc(), Media.id.desc())
        else:
            query = query.order_by(Media.updated_at.desc(), Media.id.desc())

        if offset:
            query = query.offset(offset)
        if limit is not None and limit > 0:
            query = query.limit(min(limit, 500))

        # A listagem não precisa dos logs completos; as agregações acima já
        # trazem contagem, última data e status derivado.
        query = query.options(lazyload(Media.logs))

        rows = db.execute(query).all()
        return [
            self._build_response(
                media=media,
                status=media.status if media.status is not None else latest_status,
                log_count=log_count or 0,
                last_log_date=last_log_date,
            )
            for media, log_count, last_log_date, latest_status in rows
        ]

    def list_tags(
        self, db: Session, user_id: int, media_type: MediaTypeEnum | None = None
    ) -> list[str]:
        """Lista as tags distintas usadas nas mídias do usuário, opcionalmente por tipo."""
        query = (
            select(Tag.name)
            .join(media_tags, media_tags.c.tag_id == Tag.id)
            .join(Media, Media.id == media_tags.c.media_id)
            .where(Media.user_id == user_id)
        )
        if media_type:
            query = query.where(Media.type == media_type)
        query = query.distinct().order_by(Tag.name)
        return [row[0] for row in db.execute(query).all()]

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

        old_path = media.image_path
        # Grava a nova imagem primeiro; a antiga só é removida após o commit,
        # para que uma falha de validação não deixe a mídia sem capa.
        filename = await self.image_service.store(file)
        media.image_path = filename
        db.commit()
        db.refresh(media)
        if old_path and old_path != filename:
            self.image_service.delete(old_path)

        return self._to_response(db, media)

    async def upload_image_from_url(
        self, db: Session, media_id: int, image_url: str, user_id: int
    ) -> MediaResponse:
        """Baixa uma imagem de uma URL externa e associa a uma mídia."""
        media = db.get(Media, media_id)
        if not media or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        old_path = media.image_path
        filename = await self.image_service.store_from_url(image_url)
        media.image_path = filename
        db.commit()
        db.refresh(media)
        if old_path and old_path != filename:
            self.image_service.delete(old_path)

        return self._to_response(db, media)

    def delete(self, db: Session, media_id: int, user_id: int) -> None:
        """Remove uma mídia e sua imagem associada."""
        media = db.get(Media, media_id)
        if not media or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada")

        image_path = media.image_path

        # Remove entradas de favoritos/backlog antes da mídia (não há relationship
        # em MediaListItem), evitando itens órfãos. O vínculo com o Suwayomi
        # também é removido; o sync recria se o título voltar.
        db.query(MediaListItem).filter(MediaListItem.media_id == media_id).delete()
        from models.suwayomi import SuwayomiMangaLink

        db.query(SuwayomiMangaLink).filter(SuwayomiMangaLink.media_id == media_id).delete()

        db.delete(media)
        db.commit()
        if image_path:
            self.image_service.delete(image_path)

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

    def _resolve_status(self, media: Media) -> MediaStatusEnum | None:
        """Status atual da mídia.

        Mídias criadas antes de `MediaCreate` aceitar `status` — ou criadas pelo
        fluxo de troca de imagem, que não envia status — têm a coluna nula. Nesse
        caso o status do log mais recente é a melhor resposta, em vez de expor um
        vazio que a listagem não consegue explicar.
        """
        if media.status is not None:
            return media.status

        logs = [log for log in media.logs if log.date is not None]
        if not logs:
            return None

        latest = max(logs, key=lambda log: (log.date, log.id))
        return latest.status

    def _build_response(
        self,
        media: Media,
        status: MediaStatusEnum | None,
        log_count: int,
        last_log_date: date | None,
    ) -> MediaResponse:
        return MediaResponse(
            id=media.id,
            user_id=media.user_id,
            external_id=media.external_id,
            title=media.title,
            type=media.type,
            status=status,
            description=media.description,
            cover_url=media.cover_url,
            image_path=media.image_path,
            release_date=media.release_date,
            rating=media.rating,
            review=media.review,
            created_at=media.created_at,
            updated_at=media.updated_at,
            log_count=log_count,
            last_log_date=last_log_date,
            tags=[tag.name for tag in media.tags],
        )

    def _to_response(self, db: Session, media: Media) -> MediaResponse:
        return self._build_response(
            media=media,
            status=self._resolve_status(media),
            log_count=self._get_log_count(db, media.id),
            last_log_date=self._get_last_log_date(media),
        )
    # TODO: Validar esses ignonore 
    def _to_response_with_logs(self, db: Session, media: Media) -> MediaWithLogsResponse:
        return MediaWithLogsResponse(
            id=media.id,
            user_id=media.user_id,
            external_id=media.external_id,
            title=media.title,
            type=media.type,
            status=self._resolve_status(media),
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
            tag = db.execute(query).scalars().first()
            
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

        image_path = media.image_path
        if image_path:
            media.image_path = None
            db.commit()
            db.refresh(media)
            self.image_service.delete(image_path)

        return self._to_response(db, media)
