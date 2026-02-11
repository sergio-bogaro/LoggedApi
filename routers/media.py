from fastapi import APIRouter, Depends, Query, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from models.media import MediaStatusEnum, MediaTypeEnum
from schemas.media import MediaCreate, MediaResponse, MediaUpdate, MediaWithLogsResponse
from services.media_service import MediaService

router = APIRouter(prefix="/api/media", tags=["Media"])

service = MediaService()


@router.get("/", response_model=list[MediaResponse])
def list_media(
    media_type: MediaTypeEnum | None = Query(None, alias="type"),
    status: MediaStatusEnum | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    """Lista todas as mídias da biblioteca, com filtros opcionais."""
    return service.find_all(db, media_type=media_type, status=status, search=search)


@router.get("/{media_id}", response_model=MediaWithLogsResponse)
def get_media(media_id: int, db: Session = Depends(get_db)):
    """Busca uma mídia pelo ID, incluindo seus logs."""
    return service.find_by_id(db, media_id)


@router.get("/external/{external_id}", response_model=MediaResponse | None)
def get_by_external_id(
    external_id: str,
    media_type: MediaTypeEnum = Query(..., alias="type"),
    db: Session = Depends(get_db),
):
    """Verifica se uma mídia já está na biblioteca pelo ID externo."""
    return service.find_by_external_id(db, external_id, media_type)


@router.post("/", response_model=MediaResponse, status_code=201)
def create_media(data: MediaCreate, db: Session = Depends(get_db)):
    """Adiciona uma nova mídia à biblioteca."""
    return service.create(db, data)


@router.patch("/{media_id}", response_model=MediaResponse)
def update_media(media_id: int, data: MediaUpdate, db: Session = Depends(get_db)):
    """Atualiza informações de uma mídia."""
    return service.update(db, media_id, data)


@router.post("/{media_id}/image", response_model=MediaResponse)
async def upload_image(
    media_id: int, file: UploadFile, db: Session = Depends(get_db)
):
    """Faz upload de uma imagem customizada para a mídia."""
    return await service.upload_image(db, media_id, file)


@router.delete("/{media_id}", status_code=204)
def delete_media(media_id: int, db: Session = Depends(get_db)):
    """Remove uma mídia da biblioteca."""
    service.delete(db, media_id)
