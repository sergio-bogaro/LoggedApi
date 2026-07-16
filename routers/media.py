from pydantic import BaseModel, HttpUrl

from fastapi import APIRouter, Depends, Query, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from models.enums import MediaStatusEnum, MediaTypeEnum
from schemas.media import MediaCheckItem, MediaCreate, MediaResponse, MediaUpdate, MediaWithLogsResponse
from services.media_service import MediaService

router = APIRouter(prefix="/api/media", tags=["Media"])

service = MediaService()


class ImageUrlPayload(BaseModel):
    url: str


@router.get("/", response_model=list[MediaResponse])
def list_media(
    user_id: int,
    media_type: MediaTypeEnum | None = Query(None, alias="type"),
    status: MediaStatusEnum | None = None,
    search: str | None = None,
    tags: list[str] | None = Query(None, description="Filtrar por tags (AND)"),
    has_logs: bool | None = Query(None, description="Apenas mídias com logs"),
    limit: int | None = Query(None, ge=1, description="Limitar quantidade de resultados"),
    offset: int = Query(0, ge=0, description="Número de itens a pular (paginação)"),
    db: Session = Depends(get_db),
):
    """Lista todas as mídias da biblioteca, com filtros opcionais."""
    return service.find_all(db, user_id=user_id, media_type=media_type, status=status, search=search, tags=tags, has_logs=has_logs, limit=limit, offset=offset)


@router.get("/{media_id}", response_model=MediaWithLogsResponse)
def get_media(media_id: int, user_id: int, db: Session = Depends(get_db)):
    """Busca uma mídia pelo ID, incluindo seus logs."""
    return service.find_by_id(db, media_id, user_id)


@router.get("/external/{external_id}", response_model=MediaResponse | None)
def get_by_external_id(
    external_id: str,
    user_id: int,
    media_type: MediaTypeEnum = Query(..., alias="type"),
    db: Session = Depends(get_db),
):
    """Verifica se uma mídia já está na biblioteca pelo ID externo."""
    return service.find_by_external_id(db, external_id, media_type, user_id)


@router.get("/external/{external_id}/with-logs", response_model=MediaWithLogsResponse | None)
def get_by_external_id_with_logs(
    external_id: str,
    user_id: int,
    media_type: MediaTypeEnum = Query(..., alias="type"),
    db: Session = Depends(get_db),
):
    """Busca uma mídia pelo ID externo incluindo seus logs."""
    return service.find_by_external_id_with_logs(db, external_id, media_type, user_id)


@router.post("/batch-check", response_model=dict[str, MediaResponse])
def batch_check_existing(items: list[MediaCheckItem], user_id: int, db: Session = Depends(get_db)):
    return service.batch_check_existing(db, items, user_id)


@router.post("/", response_model=MediaResponse, status_code=201)
def create_media(data: MediaCreate, db: Session = Depends(get_db)):
    """Adiciona uma nova mídia à biblioteca."""
    return service.create(db, data)


@router.patch("/{media_id}", response_model=MediaResponse)
def update_media(media_id: int, data: MediaUpdate, user_id: int, db: Session = Depends(get_db)):
    """Atualiza informações de uma mídia."""
    return service.update(db, media_id, data, user_id)


@router.post("/{media_id}/image", response_model=MediaResponse)
async def upload_image(
    media_id: int, user_id: int, file: UploadFile, db: Session = Depends(get_db)
):
    """Faz upload de uma imagem customizada para a mídia."""
    return await service.upload_image(db, media_id, file, user_id)


@router.post("/{media_id}/image-url", response_model=MediaResponse)
async def upload_image_from_url(
    media_id: int, user_id: int, payload: ImageUrlPayload, db: Session = Depends(get_db)
):
    """Baixa uma imagem de uma URL externa e associa a uma mídia."""
    return await service.upload_image_from_url(db, media_id, payload.url, user_id)


@router.delete("/{media_id}", status_code=204)
def delete_media(media_id: int, user_id: int, db: Session = Depends(get_db)):
    """Remove uma mídia da biblioteca."""
    service.delete(db, media_id, user_id)


@router.delete("/{media_id}/image", response_model=MediaResponse)
async def remove_image(
    media_id: int, user_id: int, db: Session = Depends(get_db)
):
    """Remove a imagem customizada de uma mídia."""
    return await service.remove_image(db, media_id, user_id)
