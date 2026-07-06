from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models.enums import MediaTypeEnum
from schemas.media_list_item import (
    MediaListItemBatchCreate,
    MediaListItemCheckResponse,
    MediaListItemCreate,
    MediaListItemResponse,
)
from services.media_list_service import MediaListService

router = APIRouter(prefix="/api/backlog", tags=["Backlog"])

service = MediaListService()
LIST_TYPE = "backlog"


@router.get("/{user_id}", response_model=list[MediaListItemResponse])
def list_backlog(user_id: int, db: Session = Depends(get_db)):
    """Lista todas as mídias do backlog de um usuário."""
    return service.get_list(db, user_id=user_id, list_type=LIST_TYPE)


@router.get("/check", response_model=MediaListItemCheckResponse)
def check_backlog(
    user_id: int,
    external_id: str = Query(..., alias="external_id"),
    media_type: MediaTypeEnum = Query(..., alias="media_type"),
    db: Session = Depends(get_db),
):
    """Verifica se uma mídia está no backlog de um usuário."""
    return service.check_in_list(db, user_id, external_id, media_type.value, LIST_TYPE)


@router.post("/", response_model=MediaListItemResponse, status_code=201)
def add_to_backlog(data: MediaListItemCreate, db: Session = Depends(get_db)):
    """Adiciona uma mídia ao backlog."""
    return service.add_to_list(db, data=data, list_type=LIST_TYPE)


@router.post("/batch", response_model=list[MediaListItemResponse])
def batch_add_to_backlog(data: MediaListItemBatchCreate, db: Session = Depends(get_db)):
    """Adiciona múltiplas mídias ao backlog."""
    return service.batch_add(db, items=data.items, list_type=LIST_TYPE)


@router.delete("/{item_id}", status_code=204)
def remove_from_backlog(item_id: int, user_id: int, db: Session = Depends(get_db)):
    """Remove uma mídia do backlog."""
    service.remove_from_list(db, item_id=item_id, user_id=user_id)
