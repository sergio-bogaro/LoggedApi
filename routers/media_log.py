from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from schemas.media_log import MediaLogCreate, MediaLogResponse, MediaLogUpdate
from services.media_log_service import MediaLogService

router = APIRouter(prefix="/api/media-logs", tags=["Media Logs"])

service = MediaLogService()


@router.get("/media/{media_id}", response_model=list[MediaLogResponse])
def list_logs_by_media(media_id: int, user_id: int, db: Session = Depends(get_db)):
    """Lista todos os logs de uma mídia específica."""
    return service.find_by_media(db, media_id, user_id)


@router.get("/{log_id}", response_model=MediaLogResponse)
def get_log(log_id: int, user_id: int, db: Session = Depends(get_db)):
    """Busca um log pelo ID."""
    return service.find_by_id(db, log_id, user_id)


@router.post("/", response_model=MediaLogResponse, status_code=201)
def create_log(data: MediaLogCreate, db: Session = Depends(get_db)):
    """Cria um novo log para uma mídia (ex: assistiu episódio, leu capítulo)."""
    return service.create(db, data)


@router.patch("/{log_id}", response_model=MediaLogResponse)
def update_log(log_id: int, data: MediaLogUpdate, user_id: int, db: Session = Depends(get_db)):
    """Atualiza um log existente."""
    return service.update(db, log_id, data, user_id)


@router.delete("/{log_id}", status_code=204)
def delete_log(log_id: int, user_id: int, db: Session = Depends(get_db)):
    """Remove um log."""
    service.delete(db, log_id, user_id)
